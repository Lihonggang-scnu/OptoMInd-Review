"""Dominant-review original-source materialization adapter.

Consumes ``ACQUISITION_REQUESTS.json`` from the dominant-review reference
unpacking side channel and materializes kept original papers through the
existing S2 structured-body -> public-OA-fulltext -> true-abstract route,
building a task-local material-card/vector increment and a NEW merged
long-term cache snapshot.  This is an independent, non-quota side channel:
it never overwrites the existing cache and never alters first-round
retrieval or supplementary task policies.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from optomind_research.metadata_index import title_match_score
from optomind_research.runtime.artifact_store import atomic_write_json
from optomind_research.runtime.material_cache_merge import (
    MaterialCacheIncrement,
    merge_material_cache,
)
from optomind_research.runtime.material_proposition_extractor import (
    run_material_proposition_extraction,
)
from optomind_research.runtime.material_unit_store import (
    material_unit_from_text_chunk,
)
from optomind_research.runtime.supplementary_retrieval_pipeline import (
    finalize_task_material_cache,
)


ROUTE_PRIORITY = ("s2_structured_body", "public_oa_fulltext", "abstract_claim")
SCHEMA_VERSION = "dominant_review_materialization.v1"
DEFAULT_CARD_MODEL_TIER = "b_plus_model"
DEFAULT_CARD_WORKERS = 3
MAX_CARD_WORKERS = 8
CARD_WORKERS_ENV = "OPTOMIND_CARD_EXTRACTION_WORKERS"


def resolve_card_extraction_workers(value: int | None = None) -> int:
    """Resolve the bounded per-paper card extraction worker count.

    An explicit parameter wins over the environment, and the environment
    wins over the conservative default of 3.  The result is clamped to
    [1, 8]; embedding workers are separate and remain unchanged.
    """

    configured = value
    if configured is None:
        configured = os.environ.get(
            CARD_WORKERS_ENV, str(DEFAULT_CARD_WORKERS)
        )
    try:
        return max(1, min(int(configured), MAX_CARD_WORKERS))
    except (TypeError, ValueError):
        return DEFAULT_CARD_WORKERS


def _card_worker_audit(
    value: Optional[int], resolved: int
) -> dict[str, Any]:
    if value is not None:
        source = "explicit"
    elif CARD_WORKERS_ENV in os.environ:
        source = "environment"
    else:
        source = "default"
    return {
        "workers": resolved,
        "default": DEFAULT_CARD_WORKERS,
        "max": MAX_CARD_WORKERS,
        "source": source,
    }


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_doi(value: Any) -> str:
    text = _text(value)
    lowered = text.casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
    return lowered.strip().rstrip(".,;")


def _normalize_arxiv(value: Any) -> str:
    text = _text(value).casefold()
    for prefix in ("arxiv:", "abs/", "https://arxiv.org/abs/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().rstrip(".,;")


def _identity_key(request: Mapping[str, Any]) -> str:
    identity = request.get("identity") or {}
    doi = _normalize_doi(identity.get("doi"))
    arxiv = _normalize_arxiv(identity.get("arxiv_id"))
    s2 = _text(identity.get("s2_paper_id"))
    if doi:
        return "doi:" + doi
    if arxiv:
        return "arxiv:" + arxiv
    if s2:
        return "s2:" + s2
    return "title:" + _text(identity.get("title")).casefold()


def _existing_cache_identity_keys(base_units: Sequence[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for unit in base_units:
        identity = unit.get("identity") or {}
        doi = _normalize_doi(identity.get("doi"))
        paper_id = _text(identity.get("paper_id"))
        arxiv = _normalize_arxiv(
            (identity.get("locator") or {}).get("arxiv_id")
        )
        title = _text(identity.get("title")).casefold()
        if doi:
            keys.add("doi:" + doi)
        if paper_id:
            keys.add("s2:" + paper_id)
        if arxiv:
            keys.add("arxiv:" + arxiv)
        if title:
            keys.add("title:" + title)
    return keys


def _dedupe_requests(
    requests: Sequence[Mapping[str, Any]],
    existing_keys: set[str],
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    seen: set[str] = set()
    kept: list[Mapping[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        key = _identity_key(request)
        reference_number = str(
            request.get("reference_number")
            or request.get("identity", {}).get("title")
            or key
        )
        if key in existing_keys:
            audit.append({
                "reference_number": reference_number,
                "status": "deduped_existing_cache",
                "identity_key": key,
            })
            continue
        if key in seen:
            audit.append({
                "reference_number": reference_number,
                "status": "deduped_within_requests",
                "identity_key": key,
            })
            continue
        seen.add(key)
        kept.append(request)
    return kept, audit


def _load_units(path: Path) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    units = payload.get("units") if isinstance(payload, Mapping) else None
    return [dict(unit) for unit in units or [] if isinstance(unit, Mapping)]


def _stable_id_from_request(request: Mapping[str, Any]) -> list[str]:
    identity = request.get("identity") or {}
    ids: list[str] = []
    doi = _normalize_doi(identity.get("doi"))
    if doi:
        ids.append("DOI:" + doi)
    arxiv = _normalize_arxiv(identity.get("arxiv_id"))
    if arxiv:
        ids.append("ARXIV:" + arxiv)
    s2 = _text(identity.get("s2_paper_id"))
    if s2:
        ids.append(s2)
    return ids


def _batch_records_by_identity(records: Sequence[Any]) -> dict[str, Any]:
    by_key: dict[str, Any] = {}
    for record in records or []:
        external = dict(getattr(record, "external_ids", None) or {})
        doi = _normalize_doi(getattr(record, "doi", ""))
        arxiv = _normalize_arxiv(external.get("ArXiv") or "")
        s2 = _text(getattr(record, "paper_id", ""))
        for key in (
            ("doi:" + doi) if doi else None,
            ("arxiv:" + arxiv) if arxiv else None,
            ("s2:" + s2) if s2 else None,
        ):
            if key and key not in by_key:
                by_key[key] = record
    return by_key


def _request_enriched(request: Mapping[str, Any]) -> dict[str, Any]:
    enriched = request.get("enriched")
    return dict(enriched) if isinstance(enriched, Mapping) else {}


def _request_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    identity = request.get("identity")
    return dict(identity) if isinstance(identity, Mapping) else {}


def _request_open_access_url(request: Mapping[str, Any]) -> str:
    identity = _request_identity(request)
    enriched = _request_enriched(request)
    for source in (request, enriched, identity):
        for key in (
            "open_access_url",
            "openAccessPdf",
            "pdf_url",
            "oa_url",
            "s2_open_access_candidate_url",
        ):
            value = _text(source.get(key))
            if value:
                return value
    return ""


def _request_true_abstract(request: Mapping[str, Any]) -> str:
    identity = _request_identity(request)
    enriched = _request_enriched(request)
    for source in (enriched, identity, request):
        abstract = _text(source.get("abstract"))
        if abstract:
            return abstract
    return ""


def _request_verified_identifiers(
    request: Mapping[str, Any],
) -> dict[str, str]:
    identity = _request_identity(request)
    enriched = _request_enriched(request)
    return {
        "doi": _normalize_doi(
            identity.get("doi") or enriched.get("doi") or request.get("doi")
        ),
        "arxiv_id": _normalize_arxiv(
            identity.get("arxiv_id")
            or enriched.get("arxiv_id")
            or request.get("arxiv_id")
        ),
        "s2_paper_id": _text(
            identity.get("s2_paper_id")
            or enriched.get("s2_paper_id")
            or request.get("s2_paper_id")
        ),
        "openalex_id": _text(
            identity.get("openalex_id")
            or enriched.get("openalex_id")
            or request.get("openalex_id")
        ),
    }


def _normalized_axis_label(value: Any) -> str:
    text = _text(value)
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def _build_question_seed_axes(
    explicit_seed_axes: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge explicit axes with acquisition-request useful_axes (no Qwen)."""
    seen: set[str] = set()
    catalog: list[dict[str, Any]] = []
    for row in explicit_seed_axes:
        if not isinstance(row, Mapping):
            continue
        label = _text(row.get("description") or row.get("label"))
        key = _normalized_axis_label(label)
        if not key or key in seen:
            continue
        seen.add(key)
        catalog.append(dict(row))
    for request in requests:
        for axis in request.get("useful_axes") or []:
            label = _text(axis)
            key = _normalized_axis_label(label)
            if not key or key in seen:
                continue
            seen.add(key)
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            catalog.append({
                "axis_id": "axis:" + digest,
                "description": label,
                "label": label,
                "origin": "acquisition_request_useful_axes",
                "status": "seed",
            })
    return catalog


def _source_guidance_for_units(
    units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    useful_axes: set[str] = set()
    useful_sections: set[str] = set()
    roles: set[str] = set()
    reference_numbers: set[str] = set()
    acquisition_priority = ""
    for unit in units:
        provenance = (unit.get("audit") or {}).get("source_provenance") or {}
        unb = provenance.get("dominant_review_unbundling") or {}
        useful_axes.update(
            _text(value)
            for value in (unb.get("useful_axes") or [])
            if _text(value)
        )
        useful_sections.update(
            _text(value)
            for value in (unb.get("useful_sections") or [])
            if _text(value)
        )
        roles.update(
            _text(value)
            for value in (unb.get("likely_evidence_roles") or [])
            if _text(value)
        )
        reference_numbers.update(
            str(value)
            for value in (
                unb.get("reference_number"),
                unb.get("reference_ordinal"),
            )
            if str(value or "").strip()
        )
        if not acquisition_priority:
            acquisition_priority = _text(unb.get("acquisition_priority"))
    return {
        "useful_axes": sorted(useful_axes),
        "useful_sections": sorted(useful_sections),
        "likely_evidence_roles": sorted(roles),
        "reference_numbers": sorted(reference_numbers),
        "acquisition_priority": acquisition_priority,
    }


def _screening_cards_by_reference(
    screening_batches: Any,
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Index screening-batch cards by reference number, first occurrence wins."""
    if isinstance(screening_batches, Mapping):
        batches = screening_batches.get("batches") or []
    else:
        batches = list(screening_batches or [])
    cards: dict[int, dict[str, Any]] = {}
    duplicates: list[int] = []
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        for card in batch.get("cards") or []:
            if not isinstance(card, Mapping):
                continue
            try:
                number = int(card.get("reference_number"))
            except (TypeError, ValueError):
                continue
            if number in cards:
                duplicates.append(number)
                continue
            cards[number] = dict(card)
    return cards, sorted(set(duplicates))


def _card_open_access_url(card: Mapping[str, Any]) -> str:
    enriched = (
        card.get("enriched")
        if isinstance(card.get("enriched"), Mapping)
        else {}
    )
    identity = (
        card.get("identity")
        if isinstance(card.get("identity"), Mapping)
        else {}
    )
    for source in (card, enriched, identity):
        for key in (
            "open_access_url",
            "openAccessPdf",
            "pdf_url",
            "oa_url",
            "s2_open_access_candidate_url",
        ):
            value = _text(source.get(key))
            if value:
                return value
    return ""


def _upgrade_request_with_card(
    request: Mapping[str, Any],
    card: Mapping[str, Any] | None,
) -> dict[str, Any]:
    upgraded = dict(request)
    if card is None:
        return upgraded
    identity = _request_identity(upgraded)
    enriched = _request_enriched(upgraded)
    card_identity = (
        card.get("identity")
        if isinstance(card.get("identity"), Mapping)
        else {}
    )
    card_enriched = (
        card.get("enriched")
        if isinstance(card.get("enriched"), Mapping)
        else {}
    )
    screen = card.get("screen") if isinstance(card.get("screen"), Mapping) else {}
    decision = (
        screen.get("decision")
        if isinstance(screen.get("decision"), Mapping)
        else {}
    )

    for key in (
        "doi",
        "arxiv_id",
        "title",
        "authors",
        "year",
        "batch_lookup_ids",
        "s2_paper_id",
        "openalex_id",
    ):
        if not identity.get(key) and card_identity.get(key) not in (None, ""):
            identity[key] = card_identity[key]
    for key, value in card_enriched.items():
        if not enriched.get(key) and value not in (None, ""):
            enriched[key] = value

    candidate = dict(upgraded)
    candidate["identity"] = identity
    candidate["enriched"] = enriched
    oa_url = _request_open_access_url(candidate)
    if not oa_url:
        oa_url = _card_open_access_url(card)

    verified_identifiers = dict(upgraded.get("verified_identifiers") or {})
    for key in ("doi", "arxiv_id", "s2_paper_id", "openalex_id"):
        if not verified_identifiers.get(key) and identity.get(key):
            verified_identifiers[key] = identity[key]
    if verified_identifiers:
        upgraded["verified_identifiers"] = verified_identifiers
    if enriched:
        upgraded["enriched"] = enriched
    if oa_url:
        upgraded["open_access_url"] = oa_url
    if not upgraded.get("query_text") and card.get("candidate_text"):
        upgraded["query_text"] = str(card.get("candidate_text") or "")
    for request_key, decision_key in (
        ("acquisition_priority", "acquisition_priority"),
        ("useful_axes", "useful_axes"),
        ("useful_sections", "useful_sections"),
        ("likely_evidence_roles", "likely_evidence_roles"),
        ("reason", "reason"),
        ("relevance_score", "relevance_score"),
    ):
        if not upgraded.get(request_key) and decision.get(decision_key) not in (
            None,
            "",
        ):
            upgraded[request_key] = decision[decision_key]
    return upgraded


def upgrade_requests_from_screening_batches(
    requests: Sequence[Mapping[str, Any]],
    screening_batches: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deterministically join old requests with screening-batch enrichment.

    No model call is made.  Existing request fields, screen decisions and
    provenance are preserved; missing enriched metadata, verified identifiers,
    OA URL and decision fields are filled from the batch card for the same
    reference number.  Unmatched requests are kept byte-for-byte unchanged.
    """
    cards, duplicate_numbers = _screening_cards_by_reference(screening_batches)
    upgraded_requests: list[dict[str, Any]] = []
    audit: dict[str, Any] = {
        "mode": "deterministic_screening_batch_join",
        "model_calls": 0,
        "request_count": len(requests),
        "matched_card_count": 0,
        "unmatched_reference_numbers": [],
        "enriched_gained_count": 0,
        "enriched_already_present_count": 0,
        "screen_decision_filled_count": 0,
        "unchanged_count": 0,
        "duplicate_card_reference_numbers": duplicate_numbers,
    }
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        try:
            number = int(request.get("reference_number"))
        except (TypeError, ValueError):
            number = None
        card = cards.get(number)
        before_enriched = bool(_request_enriched(request))
        before_decision = bool(
            request.get("useful_axes")
            or request.get("likely_evidence_roles")
            or request.get("acquisition_priority")
        )
        upgraded = _upgrade_request_with_card(request, card)
        after_enriched = bool(_request_enriched(upgraded))
        after_decision = bool(
            upgraded.get("useful_axes")
            or upgraded.get("likely_evidence_roles")
            or upgraded.get("acquisition_priority")
        )
        if card is None:
            audit["unmatched_reference_numbers"].append(number)
        else:
            audit["matched_card_count"] += 1
        if after_enriched and not before_enriched:
            audit["enriched_gained_count"] += 1
        elif before_enriched:
            audit["enriched_already_present_count"] += 1
        if after_decision and not before_decision:
            audit["screen_decision_filled_count"] += 1
        if upgraded == dict(request):
            audit["unchanged_count"] += 1
        upgraded_requests.append(upgraded)
    return upgraded_requests, audit


def _has_enriched_identity(request: Mapping[str, Any]) -> bool:
    enriched = _request_enriched(request)
    identity = _request_identity(request)
    if enriched and any(
        _text(enriched.get(key))
        for key in (
            "abstract",
            "s2_paper_id",
            "openalex_id",
            "doi",
            "arxiv_id",
            "open_access_url",
            "openAccessPdf",
        )
    ):
        return True
    return bool(
        _text(identity.get("s2_paper_id"))
        or _text(identity.get("openalex_id"))
        or _text(identity.get("verified"))
        or _text(request.get("verified"))
    )


def _trustworthy_bibliographic_identity(request: Mapping[str, Any]) -> bool:
    identity = _request_identity(request)
    enriched = _request_enriched(request)
    verified = _request_verified_identifiers(request)
    if verified["doi"] or verified["arxiv_id"] or verified["s2_paper_id"]:
        return True
    title = _text(identity.get("title") or enriched.get("title"))
    authors = _text(identity.get("authors") or enriched.get("authors"))
    year = _text(identity.get("year") or enriched.get("year"))
    return bool(title and authors and year)


def _fallback_paper_id(mapping: Mapping[str, Any]) -> str:
    key = "|".join([
        _normalize_doi(mapping.get("doi")),
        _normalize_arxiv(mapping.get("arxiv_id")),
        _text(mapping.get("s2_paper_id")),
        _text(mapping.get("title")).casefold(),
    ])
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"identity-fallback:{digest}"


def _mapping_from_request_metadata(
    request: Mapping[str, Any],
    *,
    verified: bool,
) -> dict[str, Any]:
    identity = _request_identity(request)
    enriched = _request_enriched(request)
    verified_ids = _request_verified_identifiers(request)
    title = _text(
        identity.get("title")
        or enriched.get("title")
        or request.get("title")
    )
    abstract = _request_true_abstract(request)
    authors = (
        identity.get("authors")
        if identity.get("authors") not in (None, "")
        else enriched.get("authors")
    )
    year = (
        identity.get("year")
        if identity.get("year") not in (None, "")
        else enriched.get("year")
    )
    external_ids = dict(enriched.get("external_ids") or {})
    return {
        "s2_paper_id": verified_ids["s2_paper_id"],
        "doi": verified_ids["doi"],
        "arxiv_id": verified_ids["arxiv_id"],
        "openalex_id": verified_ids["openalex_id"],
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "year": year,
        "venue": _text(enriched.get("venue")),
        "open_access_url": _request_open_access_url(request),
        "external_ids": external_ids,
        "use_permission": "discovery_only",
        "s2_record": None,
        "verified_identifiers": verified_ids,
        "identity_evidence": False,
        "evidence_status": "metadata_only",
        "metadata_verified": bool(verified),
    }


def _paper_record_from_mapping(mapping: Mapping[str, Any]):
    """Build a lightweight S2PaperRecord for OA escalation from metadata."""
    title = _text(mapping.get("title"))
    doi = _normalize_doi(mapping.get("doi"))
    oa_url = _text(mapping.get("open_access_url"))
    s2_id = _text(mapping.get("s2_paper_id"))
    if not (title or doi or oa_url or s2_id):
        return None
    from optomind_research.s2_schemas import S2PaperRecord

    year = mapping.get("year")
    try:
        year_value = int(year) if str(year or "").strip().isdigit() else None
    except (TypeError, ValueError):
        year_value = None
    return S2PaperRecord(
        paper_id=s2_id or _fallback_paper_id(mapping),
        doi=doi,
        title=title,
        abstract=_text(mapping.get("abstract")),
        year=year_value,
        venue=_text(mapping.get("venue")),
        external_ids=dict(mapping.get("external_ids") or {}),
        s2_open_access_candidate_url=oa_url,
        is_oa=bool(oa_url) or None,
        use_permission=_text(mapping.get("use_permission"))
        or "discovery_only",
        discovery_route="dominant_review_unbundling_identity_fallback",
    )


def _merge_audit_counts(existing: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in (new or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = int(merged.get(key, 0) or 0) + int(value)
        elif key not in merged:
            merged[key] = value
    return merged


def _resolve_requests_prefetch(
    requests: Sequence[Mapping[str, Any]],
    *,
    batch_fn: Callable[[list[str]], Any],
    search_fn: Callable[[str], Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve requests using enriched identity, then S2, then fallback.

    Requests carrying already-known enriched/verified identity are resolved
    offline first.  Remaining requests go through S2 batch/title lookup.
    If S2 is unavailable OR has no exact match and the request still has
    trustworthy bibliographic identity (DOI/arXiv/S2 or title+authors+year),
    a metadata-only record is constructed for OA/abstract acquisition; it is
    never labeled scientific evidence.  Service-unavailable is tracked per
    request while the aggregate audit records whether any call failed.
    """
    results: list[dict[str, Any]] = []
    pending: list[tuple[int, Mapping[str, Any]]] = []
    for index, request in enumerate(requests):
        if _has_enriched_identity(request):
            results.append({
                "resolved": _mapping_from_request_metadata(
                    request, verified=True
                ),
                "resolution_mode": "enriched_identity",
                "service_unavailable": False,
            })
        else:
            results.append(None)
            pending.append((index, request))

    stable_ids: list[str] = []
    for _, request in pending:
        for lookup_id in _stable_id_from_request(request):
            if lookup_id not in stable_ids:
                stable_ids.append(lookup_id)
    resolved_by_key: dict[str, dict[str, Any]] = {}
    batch_call_count = 0
    batch_failed_call_count = 0
    failed_lookup_ids: set[str] = set()
    service_unavailable_any = False
    if stable_ids:
        for offset in range(0, len(stable_ids), 500):
            chunk = stable_ids[offset:offset + 500]
            try:
                payload = batch_fn(chunk)
            except Exception:
                batch_failed_call_count += 1
                service_unavailable_any = True
                failed_lookup_ids.update(str(item) for item in chunk)
                continue
            if isinstance(payload, tuple):
                records = payload[0] if payload else []
            elif isinstance(payload, list):
                records = payload
            else:
                records = payload
            batch_call_count += 1
            for key, record in _batch_records_by_identity(records).items():
                mapping = _record_to_mapping(record)
                mapping["s2_record"] = record
                mapping["identity_evidence"] = True
                mapping["evidence_status"] = "s2_verified"
                resolved_by_key.setdefault(key, mapping)
    title_call_count = 0
    for index, request in pending:
        mapping: dict[str, Any] = {}
        mode = "unresolved"
        request_service_unavailable = any(
            lookup_id in failed_lookup_ids
            for lookup_id in _stable_id_from_request(request)
        )
        for lookup_id in _stable_id_from_request(request):
            key = lookup_id.lower()
            if key.startswith("doi:"):
                key = "doi:" + _normalize_doi(lookup_id[4:])
            elif key.startswith("arxiv:"):
                key = "arxiv:" + _normalize_arxiv(lookup_id[6:])
            else:
                key = "s2:" + _text(lookup_id)
            mapping = resolved_by_key.get(key) or mapping
        if mapping:
            mode = "exact_identity"
        else:
            title = _text((request.get("identity") or {}).get("title"))
            if title:
                title_call_count += 1
                try:
                    raw = search_fn(title) or []
                except Exception:
                    request_service_unavailable = True
                    service_unavailable_any = True
                    mode = "s2_service_unavailable"
                    raw = []
                records = (
                    [raw]
                    if not isinstance(raw, list) and raw is not None
                    else (raw or [])
                )
                if mode != "s2_service_unavailable":
                    best = None
                    best_score = 0.0
                    for record in records:
                        score = title_match_score(
                            title, _text(getattr(record, "title", ""))
                        )
                        if score > best_score:
                            best_score = score
                            best = record
                    if best is not None and best_score >= 0.9:
                        mapping = _record_to_mapping(best)
                        mapping["s2_record"] = best
                        mapping["identity_evidence"] = True
                        mapping["evidence_status"] = "s2_verified"
                        mode = "exact_title_fallback"
                    else:
                        mode = "title_no_exact_match"
        if not mapping and mode != "exact_identity":
            if _trustworthy_bibliographic_identity(request):
                mapping = _mapping_from_request_metadata(
                    request, verified=False
                )
                mode = (
                    "identity_fallback"
                    if request_service_unavailable
                    else "no_s2_match_identity_fallback"
                )
        results[index] = {
            "resolved": mapping or None,
            "resolution_mode": mode,
            "service_unavailable": request_service_unavailable,
        }
    audit = {
        "batch_call_count": batch_call_count,
        "batch_failed_call_count": batch_failed_call_count,
        "stable_id_count": len(stable_ids),
        "title_call_count": title_call_count,
        "resolved_count": sum(
            1 for row in results if row and row["resolved"]
        ),
        "unresolved_count": sum(
            1 for row in results if row and not row["resolved"]
        ),
        "enriched_resolved_count": sum(
            1 for row in results
            if row and row["resolution_mode"] == "enriched_identity"
        ),
        "identity_fallback_count": sum(
            1 for row in results
            if row and row["resolution_mode"] == "identity_fallback"
        ),
        "no_s2_match_identity_fallback_count": sum(
            1 for row in results
            if row
            and row["resolution_mode"] == "no_s2_match_identity_fallback"
        ),
        "title_resolved_count": sum(
            1 for row in results
            if row and row["resolution_mode"] == "exact_title_fallback"
        ),
        "s2_service_unavailable": service_unavailable_any,
    }
    return results, audit


def resolve_identity_default(
    request: Mapping[str, Any],
    *,
    batch_fn: Callable[[list[str]], Any],
    search_fn: Callable[[str], Any],
) -> tuple[Optional[Mapping[str, Any]], str]:
    """Resolve DOI/arXiv/S2 ids exactly, then exact-title fallback only."""
    stable_ids = _stable_id_from_request(request)
    if stable_ids:
        records, _ = batch_fn(stable_ids)
        for record in records or []:
            if getattr(record, "paper_id", None):
                return _record_to_mapping(record), "exact_identity"
    title = _text((request.get("identity") or {}).get("title"))
    if not title:
        return None, "no_stable_identity_no_title"
    records = search_fn(title) or []
    if not isinstance(records, list):
        return None, "title_search_empty"
    best = None
    best_score = 0.0
    for record in records:
        score = title_match_score(title, _text(getattr(record, "title", "")))
        if score > best_score:
            best_score = score
            best = record
    if best is None or best_score < 0.9:
        return None, "title_no_exact_match"
    return _record_to_mapping(best), "exact_title_fallback"


def _record_to_mapping(record: Any) -> dict[str, Any]:
    external = dict(getattr(record, "external_ids", None) or {})
    return {
        "s2_paper_id": _text(getattr(record, "paper_id", "")),
        "doi": _text(getattr(record, "doi", "")),
        "arxiv_id": _text(external.get("ArXiv") or ""),
        "title": _text(getattr(record, "title", "")),
        "abstract": _text(getattr(record, "abstract", "")),
        "year": getattr(record, "year", None),
        "venue": _text(getattr(record, "venue", "")),
        "use_permission": _text(getattr(record, "use_permission", ""))
        or "factual_support",
        "open_access_url": _text(
            getattr(record, "s2_open_access_candidate_url", "")
        ),
        "external_ids": dict(external),
    }


def s2_body_provider_default(
    request: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    snippet_fn: Callable[[str, list[str]], list[dict[str, Any]]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    s2_id = _text(resolved.get("s2_paper_id"))
    if not s2_id:
        return []
    items = snippet_fn(
        _text((request.get("identity") or {}).get("title") or resolved.get("title", "")),
        [s2_id],
    )
    chunks: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        snippet = item.get("snippet") or {}
        text = _text(snippet.get("text") if isinstance(snippet, Mapping) else "")
        if not text:
            continue
        chunks.append({
            "chunk_id": f"s2-body:{s2_id}:{len(chunks):04d}",
            "paper_id": s2_id,
            "doi": _text(resolved.get("doi")),
            "title": _text(resolved.get("title")),
            "text": text,
            "section_path": _text(
                snippet.get("section") if isinstance(snippet, Mapping) else ""
            ),
            "source_kind": "s2_body",
            "content_depth": "fulltext",
            "use_permission": "factual_support",
            "provenance": {"route": "s2_structured_body"},
        })
    return chunks


def abstract_provider_default(
    request: Mapping[str, Any],
    resolved: Mapping[str, Any],
) -> list[dict[str, Any]]:
    abstract = _text(resolved.get("abstract"))
    if not abstract:
        return []
    s2_id = _text(resolved.get("s2_paper_id")) or "abstract-only"
    return [{
        "chunk_id": f"abstract:{s2_id}:0000",
        "paper_id": s2_id,
        "doi": _text(resolved.get("doi")),
        "title": _text(resolved.get("title")),
        "text": abstract,
        "section_path": "abstract",
        "source_kind": "true_abstract",
        "content_depth": "abstract_claim",
        "use_permission": "contextual_or_qualified_support",
        "provenance": {"route": "abstract_claim"},
    }]


def _default_create_empty_kb(path: Path) -> Path:
    from optomind_research.runtime.topic_scoped_kb_stage import (
        create_empty_review_kb,
    )

    return create_empty_review_kb(path)


def _default_acquirer_factory(kb_sqlite: Path, download_dir: Path) -> Any:
    from optomind_research.s2_fulltext_acquisition import S2FulltextAcquirer

    return S2FulltextAcquirer(
        kb_sqlite=kb_sqlite,
        download_dir=download_dir,
    )


def _read_oa_text_chunks(kb_sqlite: Path, paper_id: str) -> list[dict[str, Any]]:
    """Read OA text chunks written into the runtime KB by S2FulltextAcquirer."""
    uri = f"file:{Path(kb_sqlite).resolve().as_posix()}?mode=ro"
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT chunk_id, paper_id, doi, title, ordinal, section_path, "
            "text, content_depth, use_permission, source_kind, "
            "context_complete, allowed_claim_kinds_json, provenance_json, "
            "route_provenance_json FROM text_chunks "
            "WHERE paper_id = ? ORDER BY ordinal, chunk_id",
            (paper_id,),
        ):
            item = dict(row)
            allowed = item.get("allowed_claim_kinds_json")
            try:
                allowed_kinds = json.loads(allowed or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                allowed_kinds = []
            rows.append({
                "chunk_id": _text(item.get("chunk_id")),
                "paper_id": _text(item.get("paper_id")),
                "doi": _text(item.get("doi")),
                "title": _text(item.get("title")),
                "text": str(item.get("text") or ""),
                "section_path": _text(item.get("section_path")) or "fulltext",
                "source_kind": "oa_fulltext",
                "content_depth": _text(item.get("content_depth")) or "fulltext",
                "use_permission": _text(item.get("use_permission"))
                or "factual_support",
                "context_complete": bool(item.get("context_complete", True)),
                "allowed_claim_kinds": (
                    allowed_kinds if isinstance(allowed_kinds, list) else []
                ),
                "provenance": {
                    "route": "public_oa_fulltext",
                    "oa_source": (
                        item.get("provenance_json")
                        or item.get("route_provenance_json")
                        or {}
                    ),
                },
            })
    return rows


def build_claim_centered_packets(
    *,
    units: Sequence[Mapping[str, Any]],
    question: str,
    seed_axes: Sequence[Mapping[str, Any]],
    review_identity: Mapping[str, Any],
    question_seed_axes: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the fixed local packet contract for the existing card extractor."""
    units_by_work: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        work_id = _text(unit.get("work_id"))
        if work_id:
            units_by_work.setdefault(work_id, []).append(unit)
    packets: list[dict[str, Any]] = []
    for work_id in sorted(units_by_work):
        work_units = units_by_work[work_id]
        first = work_units[0]
        identity = first.get("identity") or {}
        selected_evidence = []
        all_chunk_ids = []
        for unit in work_units:
            durable = unit.get("durable_content") or {}
            quality = (
                (unit.get("durable_content_card") or {}).get("content_quality")
                or {}
            )
            chunk_id = _text((unit.get("identity") or {}).get("chunk_id"))
            all_chunk_ids.append(chunk_id)
            selected_evidence.append({
                "chunk_id": chunk_id,
                "text": str(durable.get("raw_text") or ""),
                "section_path": _text(durable.get("section_path")),
                "paper_id": _text(identity.get("paper_id")),
                "title": _text(identity.get("title")),
                "source_kind": _text(quality.get("source_kind")),
                "content_depth": _text(durable.get("content_depth")),
                "evidence_ceiling": _text(quality.get("evidence_ceiling"))
                or "contextual_or_qualified_support",
                "scope_fit": "unreviewed",
            })
        material_classes = sorted({
            _text(unit.get("material_class") or "")
            for unit in work_units
        } - {""})
        source_guidance = _source_guidance_for_units(work_units)
        packets.append({
            "schema_version": "optomind.claim_centered_material_packet.v1",
            "canonical_work_id": work_id,
            "canonical_identity": {
                "paper_id": _text(identity.get("paper_id")),
                "doi": _text(identity.get("doi")),
                "title": _text(identity.get("title")),
                "year": identity.get("year"),
                "venue": _text(identity.get("venue")),
            },
            "member_paper_ids": [_text(identity.get("paper_id"))],
            "material_classes": material_classes or ["s2_body"],
            "strongest_material_class": (
                material_classes[0] if material_classes else "s2_body"
            ),
            "question": question,
            "seed_axis_catalog": [
                dict(row)
                for row in (
                    question_seed_axes
                    if question_seed_axes is not None
                    else seed_axes
                )
            ],
            "supplementary_context": {
                "dominant_review_unbundling": {
                    "review_paper_id": _text(
                        review_identity.get("paper_id")
                    ),
                    "review_title": _text(review_identity.get("title")),
                },
                "source_guidance": source_guidance,
            },
            "selected_evidence": selected_evidence,
            "all_available_chunk_ids": all_chunk_ids,
            "selection_audit": {
                "selected_count": len(selected_evidence),
                "max_chunks_per_work": None,
            },
            "downstream_rule": (
                "Extract propositions only from selected_evidence and cite "
                "exact chunk_ids."
            ),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "canonical_work_count": len(packets),
        "packets": packets,
    }


def _build_cost_summary(
    qwen_usage: Sequence[Mapping[str, Any]],
    final_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Provider-token cost summary; embedding price is never invented."""
    from optomind_research.runtime.cost_ledger import (
        estimate_call_cost_cny,
        load_model_pricing,
    )

    per_model: dict[str, dict[str, Any]] = {}
    qwen_input_tokens = 0
    qwen_output_tokens = 0
    qwen_model_call_count = 0
    qwen_total = 0.0
    for row in qwen_usage:
        model = _text(row.get("model_name")) or "unknown"
        input_tokens = int(row.get("input_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        model_call_count = max(
            1, int(row.get("model_call_count") or 1)
        )
        qwen_input_tokens += input_tokens
        qwen_output_tokens += output_tokens
        qwen_model_call_count += model_call_count
        attempts = row.get("per_attempt_usage") or []
        if isinstance(attempts, list) and attempts:
            cost = sum(
                estimate_call_cost_cny(
                    _text(attempt.get("model_name")) or model,
                    int(attempt.get("input_tokens") or 0),
                    int(attempt.get("output_tokens") or 0),
                )
                for attempt in attempts
                if isinstance(attempt, Mapping)
            )
        else:
            cost = estimate_call_cost_cny(
                model, input_tokens, output_tokens
            )
        qwen_total += cost
        entry = per_model.setdefault(model, {
            "model_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_cny": 0.0,
        })
        entry["model_call_count"] += model_call_count
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["estimated_cost_cny"] = round(
            float(entry["estimated_cost_cny"]) + cost, 6
        )

    embedding_usage = (
        final_report.get("embedding_usage")
        if isinstance(final_report.get("embedding_usage"), Mapping)
        else {}
    )
    embedding_input_tokens = int(
        embedding_usage.get("input_tokens") or 0
    )
    embedding_request_count = int(
        embedding_usage.get("request_count") or 0
    )
    pricing = load_model_pricing()
    embedding_rate = (
        (pricing.get("embedding") or {}).get("input_cny_per_million")
        or (pricing.get("embeddings") or {}).get("input_cny_per_million")
    )
    priced_components = ["qwen_calls"]
    embedding_cost: Optional[float] = None
    embedding_omitted_reason = ""
    if embedding_rate is None:
        embedding_omitted_reason = (
            "embedding price not configured in config/model_pricing.json; "
            "embedding cost omitted rather than invented"
        )
    else:
        embedding_cost = (
            embedding_input_tokens / 1_000_000 * float(embedding_rate)
        )
        priced_components.append("embeddings")
    total = qwen_total + (embedding_cost or 0.0)
    note = (
        "Total estimated cost includes only priced components: "
        + ", ".join(priced_components)
    )
    if embedding_omitted_reason:
        note += "; " + embedding_omitted_reason
    return {
        "currency": "CNY",
        "qwen": {
            "model_call_count": qwen_model_call_count,
            "input_tokens": qwen_input_tokens,
            "output_tokens": qwen_output_tokens,
            "estimated_cost_cny": round(qwen_total, 6),
            "per_model": per_model,
        },
        "embedding": {
            "input_tokens": embedding_input_tokens,
            "request_count": embedding_request_count,
            "estimated_cost_cny": embedding_cost,
            "cost_omitted_reason": embedding_omitted_reason or None,
        },
        "total_estimated_cost_cny": round(total, 6),
        "priced_components": priced_components,
        "note": note,
    }


def run_dominant_review_materialization(
    *,
    acquisition_requests_path: Path,
    base_units_path: Path,
    base_vectors_path: Path,
    output_dir: Path,
    question: str,
    review_identity: Mapping[str, Any],
    seed_axes: Sequence[Mapping[str, Any]] = (),
    resolve_identity_fn: Optional[Callable[..., Any]] = None,
    route_providers: Optional[Mapping[str, Callable[..., list[dict[str, Any]]]]] = None,
    batch_fn: Optional[Callable[[list[str]], Any]] = None,
    search_fn: Optional[Callable[[str], Any]] = None,
    snippet_fn: Optional[Callable[[str, list[str]], list[dict[str, Any]]]] = None,
    oa_acquirer_factory: Optional[Callable[[Path, Path], Any]] = None,
    oa_create_kb_fn: Optional[Callable[[Path], Path]] = None,
    oa_read_chunks_fn: Optional[Callable[[Path, str], list[dict[str, Any]]]] = None,
    extract_cards_fn: Optional[Callable[..., Any]] = None,
    finalize_fn: Optional[Callable[..., Any]] = None,
    merge_fn: Optional[Callable[..., Any]] = None,
    embedder: Optional[Callable[..., Any]] = None,
    cards_model_tier: str = DEFAULT_CARD_MODEL_TIER,
    card_workers: Optional[int] = None,
    embedding_batch_size: int = 10,
    embedding_workers: int = 4,
    resume: bool = False,
    screening_batches_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Materialize kept original papers and produce increment + merged cache."""
    effective_card_workers = resolve_card_extraction_workers(card_workers)
    output_dir = Path(output_dir)
    checkpoint_path = output_dir / "materialization_checkpoint.json"
    report_path = output_dir / "DOMINANT_REVIEW_MATERIALIZATION_REPORT.json"
    if output_dir.exists():
        if not resume:
            raise FileExistsError(
                "Refusing to overwrite existing output directory: "
                + str(output_dir)
            )
        if not checkpoint_path.is_file():
            raise FileExistsError(
                "Output directory exists but has no materialization "
                "checkpoint; refusing to resume without one: "
                + str(output_dir)
            )
    elif resume:
        raise FileNotFoundError(
            "Resume requested but output directory does not exist: "
            + str(output_dir)
        )
    requests_path = Path(acquisition_requests_path)
    upgraded_artifact_path = output_dir / "ACQUISITION_REQUESTS_UPGRADED.json"
    upgrade_audit: dict[str, Any] = {
        "mode": "no_upgrade",
        "model_calls": 0,
        "request_count": 0,
    }
    if (
        resume
        and screening_batches_path is None
        and upgraded_artifact_path.is_file()
    ):
        requests_path = upgraded_artifact_path
        try:
            upgrade_audit = json.loads(
                upgraded_artifact_path.read_text(encoding="utf-8")
            ).get("upgrade_audit") or upgrade_audit
        except (OSError, ValueError, json.JSONDecodeError):
            upgrade_audit = {"mode": "upgraded_artifact_unreadable"}
    requests = json.loads(requests_path.read_text("utf-8"))
    if isinstance(requests, Mapping):
        requests = requests.get("requests") or []
    if not isinstance(requests, list):
        raise ValueError("ACQUISITION_REQUESTS.json must contain a request list")
    if screening_batches_path is not None:
        screening_payload = json.loads(
            Path(screening_batches_path).read_text("utf-8")
        )
        requests, upgrade_audit = upgrade_requests_from_screening_batches(
            requests, screening_payload
        )
    base_units = _load_units(base_units_path)
    existing_keys = _existing_cache_identity_keys(base_units)
    kept_requests, dedupe_audit = _dedupe_requests(requests, existing_keys)

    completed_items: list[dict[str, Any]] = []
    if resume:
        checkpoint = json.loads(
            checkpoint_path.read_text(encoding="utf-8")
        )
        completed_items = list(checkpoint.get("completed_items") or [])
        completed_keys = {
            row.get("identity_key")
            for row in completed_items
            if row.get("identity_key")
        }
        kept_requests = [
            request
            for request in kept_requests
            if _identity_key(request) not in completed_keys
        ]
        dedupe_audit = list(checkpoint.get("dedupe_audit") or dedupe_audit)
    else:
        checkpoint = None

    total_kept_count = (
        len(completed_items) + len(kept_requests)
        if checkpoint is not None
        else len(kept_requests)
    )
    question_seed_axes = _build_question_seed_axes(seed_axes, requests)

    output_dir.mkdir(parents=True, exist_ok=True)
    increment_dir = output_dir / "task_local_increment"
    increment_dir.mkdir(parents=True, exist_ok=True)
    if screening_batches_path is not None:
        atomic_write_json(upgraded_artifact_path, {
            "schema_version": SCHEMA_VERSION,
            "source_requests": str(requests_path),
            "screening_batches": str(screening_batches_path),
            "requests": requests,
            "upgrade_audit": upgrade_audit,
        })

    units: list[dict[str, Any]] = []
    for item in completed_items:
        route = item.get("route") or ""
        for chunk in item.get("materialized_chunks") or []:
            unit = material_unit_from_text_chunk(chunk, chunk)
            unit["material_class"] = (
                "s2_body"
                if route == "s2_structured_body"
                else "oa_fulltext"
                if route == "public_oa_fulltext"
                else "abstract_claim"
            )
            units.append(unit)

    route_counts: dict[str, int] = dict(
        checkpoint.get("route_counts") or {}
    ) if checkpoint else {}
    per_request_audit: list[dict[str, Any]] = list(
        checkpoint.get("per_request_audit") or []
    ) if checkpoint else []
    unresolved: list[dict[str, Any]] = list(
        checkpoint.get("unresolved_requests") or []
    ) if checkpoint else []
    oa_audits: list[dict[str, Any]] = list(
        checkpoint.get("oa_audits") or []
    ) if checkpoint else []
    resolution_audit: dict[str, Any] = dict(
        checkpoint.get("resolution_audit") or {}
    ) if checkpoint else {"mode": "injected_resolver"}
    stages: dict[str, Any] = dict(
        checkpoint.get("stages") or {}
    ) if checkpoint else {"requests_complete": False}

    processed_in_this_session = 0

    def save_checkpoint() -> None:
        atomic_write_json(checkpoint_path, {
            "schema_version": SCHEMA_VERSION,
            "output_dir": str(output_dir),
            "requests_total": len(requests),
            "kept_request_count": total_kept_count,
            "completed_items": completed_items,
            "remaining_count": max(
                0, len(kept_requests) - processed_in_this_session
            ),
            "route_counts": route_counts,
            "per_request_audit": per_request_audit,
            "unresolved_requests": unresolved,
            "oa_audits": oa_audits,
            "dedupe_audit": dedupe_audit,
            "resolution_audit": resolution_audit,
            "upgrade_audit": upgrade_audit,
            "stages": stages,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    if resolve_identity_fn is None:
        from optomind_research.s2_intelligence_gateway import (
            S2IntelligenceGateway,
        )

        gateway = S2IntelligenceGateway()
        batch_fn = batch_fn or gateway.batch_papers
        search_fn = search_fn or (lambda query: gateway.title_match(query)[0])
        snippet_fn = snippet_fn or (
            lambda query, paper_ids: gateway.search_snippets(
                query, paper_ids=paper_ids, limit=20
            )[0]
        )
        resolution_results, new_resolution_audit = _resolve_requests_prefetch(
            kept_requests,
            batch_fn=batch_fn,
            search_fn=search_fn,
        )
        resolution_audit = _merge_audit_counts(
            resolution_audit, new_resolution_audit
        )
        prefetch_index = 0

        def default_resolve(request: Mapping[str, Any]):
            nonlocal prefetch_index
            row = resolution_results[prefetch_index]
            prefetch_index += 1
            return (
                row["resolved"],
                row["resolution_mode"],
                row.get("service_unavailable", False),
            )

        resolve_identity_fn = default_resolve
    route_providers = route_providers or {}
    if "s2_structured_body" not in route_providers:
        route_providers = dict(route_providers)
        route_providers["s2_structured_body"] = (
            lambda request, resolved: s2_body_provider_default(
                request, resolved, snippet_fn=snippet_fn
            )
        )
    if "abstract_claim" not in route_providers:
        route_providers = dict(route_providers)
        route_providers["abstract_claim"] = abstract_provider_default

    if "public_oa_fulltext" not in route_providers:
        oa_runtime_dir = increment_dir / "oa_runtime"
        oa_runtime_dir.mkdir(parents=True, exist_ok=True)
        oa_kb_path = oa_runtime_dir / "runtime_kb.sqlite"
        oa_download_dir = increment_dir / "oa_downloads"
        oa_download_dir.mkdir(parents=True, exist_ok=True)
        create_kb = oa_create_kb_fn or _default_create_empty_kb
        if not oa_kb_path.exists():
            create_kb(oa_kb_path)
        acquirer_factory = oa_acquirer_factory or _default_acquirer_factory
        read_oa = oa_read_chunks_fn or _read_oa_text_chunks

        def oa_fulltext_provider(
            request: Mapping[str, Any], resolved: Mapping[str, Any]
        ) -> list[dict[str, Any]]:
            paper = resolved.get("s2_record")
            if paper is None:
                paper = _paper_record_from_mapping(resolved)
            if paper is None:
                return []
            from optomind_research.s2_fulltext_acquisition import (
                decide_fulltext_escalation,
            )

            decision = decide_fulltext_escalation(
                paper, need_complete_context=True
            )
            acquirer = acquirer_factory(oa_kb_path, oa_download_dir)
            result = acquirer.acquire(
                [(paper, decision)],
                max_successes=1,
                source_task_id="dominant_review_materialization",
            )
            rows = read_oa(oa_kb_path, paper.paper_id)
            oa_audits.append({
                "reference_number": str(
                    request.get("reference_number")
                    or (request.get("identity") or {}).get("title")
                    or paper.paper_id
                ),
                "paper_id": paper.paper_id,
                "decision": asdict(decision),
                "acquisition": (
                    result.to_dict()
                    if hasattr(result, "to_dict")
                    else vars(result)
                ),
                "chunk_count": len(rows),
            })
            return [dict(row) for row in rows]

        route_providers = dict(route_providers)
        route_providers["public_oa_fulltext"] = oa_fulltext_provider

    save_checkpoint()
    for request in kept_requests:
        identity_key = _identity_key(request)
        reference_number = str(
            request.get("reference_number")
            or (request.get("identity") or {}).get("title")
            or "?"
        )
        materialized_route = ""
        materialized_chunks: list[dict[str, Any]] = []
        service_unavailable = False
        resolution_mode = "unknown"
        try:
            payload = resolve_identity_fn(request)
            if isinstance(payload, (tuple, list)):
                resolved = payload[0] if payload else None
                resolution_mode = (
                    str(payload[1]) if len(payload) > 1 else "unknown"
                )
                service_unavailable = (
                    bool(payload[2]) if len(payload) > 2 else False
                )
            else:
                resolved = payload
        except Exception as exc:
            unresolved.append({
                "identity_key": identity_key,
                "reference_number": reference_number,
                "status": "resolution_failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            completed_items.append({
                "identity_key": identity_key,
                "reference_number": reference_number,
                "status": "resolution_failed",
                "route": "",
                "resolution_mode": "resolution_failed",
                "service_unavailable": False,
                "materialized_chunks": [],
                "unit_count": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            processed_in_this_session += 1
            save_checkpoint()
            continue
        if not resolved:
            unresolved.append({
                "identity_key": identity_key,
                "reference_number": reference_number,
                "status": "unresolved",
                "resolution_mode": resolution_mode,
                "service_unavailable": service_unavailable,
            })
            completed_items.append({
                "identity_key": identity_key,
                "reference_number": reference_number,
                "status": "unresolved",
                "route": "",
                "resolution_mode": resolution_mode,
                "service_unavailable": service_unavailable,
                "materialized_chunks": [],
                "unit_count": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            processed_in_this_session += 1
            save_checkpoint()
            continue
        for route in ROUTE_PRIORITY:
            provider = route_providers.get(route)
            if provider is None:
                continue
            try:
                chunks = provider(request, resolved) or []
            except Exception as exc:
                per_request_audit.append({
                    "reference_number": reference_number,
                    "status": "route_failed",
                    "route": route,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            if not chunks:
                continue
            materialized_route = route
            route_counts[route] = route_counts.get(route, 0) + 1
            for index, chunk in enumerate(chunks):
                chunk = dict(chunk)
                chunk["provenance"] = {
                    **(chunk.get("provenance") or {}),
                    "dominant_review_unbundling": {
                        "review_paper_id": _text(
                            review_identity.get("paper_id")
                        ),
                        "review_title": _text(review_identity.get("title")),
                        "reference_number": reference_number,
                        "reference_ordinal": reference_number,
                        "route": route,
                        "useful_axes": [
                            str(value)
                            for value in (request.get("useful_axes") or [])
                        ],
                        "useful_sections": [
                            str(value)
                            for value in (request.get("useful_sections") or [])
                        ],
                        "likely_evidence_roles": [
                            str(value)
                            for value in (
                                request.get("likely_evidence_roles") or []
                            )
                        ],
                        "acquisition_priority": _text(
                            request.get("acquisition_priority")
                        ),
                        "resolution_mode": resolution_mode,
                        "service_unavailable": service_unavailable,
                        "evidence_status": (
                            resolved.get("evidence_status")
                            or (
                                "s2_verified"
                                if resolved.get("s2_record")
                                else "metadata_only"
                            )
                        ),
                        "identity_evidence": (
                            bool(resolved.get("identity_evidence"))
                            if "identity_evidence" in resolved
                            else bool(resolved.get("s2_record"))
                        ),
                        "verified_identifiers": dict(
                            resolved.get("verified_identifiers")
                            or _request_verified_identifiers(request)
                        ),
                        "true_abstract": _text(
                            resolved.get("abstract")
                            or _request_true_abstract(request)
                        ),
                        "open_access_url": _text(
                            resolved.get("open_access_url")
                            or _request_open_access_url(request)
                        ),
                        "enriched_metadata": {
                            "title": _text(resolved.get("title")),
                            "abstract": _text(resolved.get("abstract")),
                            "s2_paper_id": _text(
                                resolved.get("s2_paper_id")
                            ),
                            "openalex_id": _text(
                                resolved.get("openalex_id")
                            ),
                            "doi": _text(resolved.get("doi")),
                            "arxiv_id": _text(resolved.get("arxiv_id")),
                            "year": resolved.get("year"),
                            "venue": _text(resolved.get("venue")),
                            "open_access_url": _text(
                                resolved.get("open_access_url")
                            ),
                        },
                    },
                    "acquisition_priority": _text(
                        request.get("acquisition_priority")
                    ),
                }
                unit = material_unit_from_text_chunk(chunk, chunk)
                unit["material_class"] = (
                    "s2_body"
                    if route == "s2_structured_body"
                    else "oa_fulltext"
                    if route == "public_oa_fulltext"
                    else "abstract_claim"
                )
                units.append(unit)
                materialized_chunks.append(chunk)
            break
        request_audit_row = {
            "reference_number": reference_number,
            "status": (
                "materialized" if materialized_route else "no_route"
            ),
            "route": materialized_route,
            "resolution_mode": resolution_mode,
            "service_unavailable": service_unavailable,
            "identity_evidence": (
                bool(resolved.get("identity_evidence"))
                if "identity_evidence" in resolved
                else bool(resolved.get("s2_record"))
            ),
            "evidence_status": (
                resolved.get("evidence_status")
                or ("s2_verified" if resolved.get("s2_record") else "metadata_only")
            ),
        }
        if materialized_route == "public_oa_fulltext" and oa_audits:
            request_audit_row["oa_audit"] = oa_audits[-1]
        per_request_audit.append(request_audit_row)
        if not materialized_route:
            unresolved.append({
                "identity_key": identity_key,
                "reference_number": reference_number,
                "status": "no_route",
                "resolution_mode": resolution_mode,
                "service_unavailable": service_unavailable,
            })
        completed_items.append({
            "identity_key": identity_key,
            "reference_number": reference_number,
            "status": "materialized" if materialized_route else "no_route",
            "route": materialized_route,
            "resolution_mode": resolution_mode,
            "service_unavailable": service_unavailable,
            "materialized_chunks": materialized_chunks,
            "unit_count": len(materialized_chunks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        processed_in_this_session += 1
        save_checkpoint()
    stages["requests_complete"] = True
    save_checkpoint()

    modes = [
        row.get("resolution_mode") for row in per_request_audit
    ]
    resolution_summary = {
        "total": len(modes),
        "enriched_identity": modes.count("enriched_identity"),
        "exact_identity": modes.count("exact_identity"),
        "exact_title_fallback": modes.count("exact_title_fallback"),
        "identity_fallback": modes.count("identity_fallback"),
        "no_s2_match_identity_fallback": modes.count(
            "no_s2_match_identity_fallback"
        ),
        "resolution_failed": sum(
            1 for row in unresolved
            if row.get("status") == "resolution_failed"
        ),
        "service_unavailable": sum(
            1 for row in per_request_audit
            if row.get("service_unavailable")
        ),
        "unresolved_or_no_route": len(unresolved),
    }

    if not units:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "no_progress",
            "no_progress": True,
            "output_dir": str(output_dir),
            "request_total": len(requests),
            "kept_request_count": total_kept_count,
            "dedupe_audit": dedupe_audit,
            "per_request_audit": per_request_audit,
            "unresolved_requests": unresolved,
            "route_counts": route_counts,
            "resolution_audit": resolution_audit,
            "resolution_summary": resolution_summary,
            "upgrade_audit": upgrade_audit,
            "question_seed_axes": question_seed_axes,
            "cost_summary": _build_cost_summary([], {}),
            "oa_audits": oa_audits,
            "unit_count": 0,
            "merged_snapshot_path": None,
            "cards_model_tier": cards_model_tier,
            "card_worker_audit": _card_worker_audit(
                card_workers, effective_card_workers
            ),
            "no_admission_cap": True,
            "quota_class": "dominant_source_unbundling_non_quota",
            "checkpoint_path": str(checkpoint_path),
        }
        atomic_write_json(report_path, report)
        stages["no_progress"] = True
        save_checkpoint()
        return report

    if (
        checkpoint
        and processed_in_this_session == 0
        and stages.get("finalize_merge_complete")
        and report_path.is_file()
    ):
        return json.loads(report_path.read_text(encoding="utf-8"))

    packets = build_claim_centered_packets(
        units=units,
        question=question,
        seed_axes=seed_axes,
        review_identity=review_identity,
        question_seed_axes=question_seed_axes,
    )
    packets_path = increment_dir / "MATERIAL_CARD_PACKETS.json"
    atomic_write_json(packets_path, packets)
    extract_cards_fn = extract_cards_fn or run_material_proposition_extraction
    cards_summary = extract_cards_fn(
        packet_path=packets_path,
        output_dir=increment_dir / "material_cards",
        model_tier=cards_model_tier,
        workers=effective_card_workers,
        skip_existing=True,
    )
    cards: list[dict[str, Any]] = []
    cards_root = increment_dir / "material_cards" / "cards"
    if cards_root.is_dir():
        for path in sorted(cards_root.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            card = value.get("card") if isinstance(value, dict) else None
            if isinstance(card, dict):
                cards.append(card)

    qwen_usage = [
        dict(row["llm_usage"])
        for row in (cards_summary.get("rows") or [])
        if isinstance(row, Mapping)
        and isinstance(row.get("llm_usage"), Mapping)
        and row["llm_usage"]
    ]
    expected_packet_count = int(
        packets.get("canonical_work_count")
        or len(packets.get("packets") or [])
    )
    parsed_card_count = len(cards)
    if parsed_card_count < expected_packet_count:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "incomplete",
            "no_progress": False,
            "completed": False,
            "output_dir": str(output_dir),
            "incomplete_reason": (
                f"expected packet count {expected_packet_count} exceeds "
                f"parsed card count {parsed_card_count}"
            ),
            "expected_packet_count": expected_packet_count,
            "parsed_card_count": parsed_card_count,
            "request_total": len(requests),
            "kept_request_count": total_kept_count,
            "dedupe_audit": dedupe_audit,
            "per_request_audit": per_request_audit,
            "unresolved_requests": unresolved,
            "route_counts": route_counts,
            "resolution_audit": resolution_audit,
            "resolution_summary": resolution_summary,
            "upgrade_audit": upgrade_audit,
            "question_seed_axes": question_seed_axes,
            "cost_summary": _build_cost_summary(qwen_usage, {}),
            "oa_audits": oa_audits,
            "unit_count": len(units),
            "card_count": parsed_card_count,
            "qwen_usage": qwen_usage,
            "merged_snapshot_path": None,
            "cards_model_tier": cards_model_tier,
            "card_worker_audit": _card_worker_audit(
                card_workers, effective_card_workers
            ),
            "no_admission_cap": True,
            "quota_class": "dominant_source_unbundling_non_quota",
            "checkpoint_path": str(checkpoint_path),
        }
        atomic_write_json(report_path, report)
        stages["cards_complete"] = False
        save_checkpoint()
        return report
    stages["cards_complete"] = True

    from optomind_research.runtime.material_semantic_cache import (
        dashscope_embedder,
    )

    finalize_fn = finalize_fn or finalize_task_material_cache
    final_report = finalize_fn(
        units=units,
        cards=cards,
        question=question,
        output_dir=increment_dir,
        embedder=embedder or dashscope_embedder,
        batch_size=embedding_batch_size,
        workers=embedding_workers,
    )
    merged_root = output_dir / "merged_cache_snapshot"
    merge_fn = merge_fn or merge_material_cache
    merge_report = merge_fn(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=[MaterialCacheIncrement(
            units_path=increment_dir / "MATERIAL_UNITS_FINAL.json",
            vectors_path=increment_dir / "material_vectors.sqlite",
        )],
        output_root=merged_root,
    )
    cost_summary = _build_cost_summary(qwen_usage, final_report)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "no_progress": False,
        "completed": True,
        "output_dir": str(output_dir),
        "expected_packet_count": expected_packet_count,
        "parsed_card_count": parsed_card_count,
        "request_total": len(requests),
        "kept_request_count": total_kept_count,
        "dedupe_audit": dedupe_audit,
        "per_request_audit": per_request_audit,
        "unresolved_requests": unresolved,
        "route_counts": route_counts,
        "resolution_audit": resolution_audit,
        "resolution_summary": resolution_summary,
        "upgrade_audit": upgrade_audit,
        "question_seed_axes": question_seed_axes,
        "cost_summary": cost_summary,
        "oa_audits": oa_audits,
        "unit_count": len(units),
        "card_count": len(cards),
        "qwen_usage": qwen_usage,
        "finalization": final_report,
        "merge": merge_report,
        "increment_paths": {
            "units_final": str(
                increment_dir / "MATERIAL_UNITS_FINAL.json"
            ),
            "vectors": str(increment_dir / "material_vectors.sqlite"),
        },
        "merged_snapshot_path": str(merged_root),
        "cards_model_tier": cards_model_tier,
        "card_worker_audit": _card_worker_audit(
            card_workers, effective_card_workers
        ),
        "no_admission_cap": True,
        "quota_class": "dominant_source_unbundling_non_quota",
        "checkpoint_path": str(checkpoint_path),
    }
    atomic_write_json(report_path, report)
    stages["finalize_merge_complete"] = True
    save_checkpoint()
    return report
