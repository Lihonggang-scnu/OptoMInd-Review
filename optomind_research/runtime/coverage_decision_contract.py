"""Deterministic contracts shared by section-coverage runtime components.

The coverage worker has several durable ledgers and more than one execution
path (normal ReAct, resume, short path, and bounded materialisation).  This
module deliberately contains no I/O, network, or model code.  It centralises
the small decisions that must have exactly one meaning on every path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


COVERAGE_ROLES = (
    "foundation",
    "mechanism",
    "method",
    "frontier",
    "controversy",
    "application",
)

ROLE_QUERY_TERMS = {
    # These are deliberately short retrieval facets.  Workflow prose such as
    # "conceptual basis" and "state of the art" consumed query budget without
    # improving provider recall, and made a topic anchor look like a chapter
    # title rather than a scientific search.
    "foundation": "principles history",
    "mechanism": "causal mechanism",
    "method": "fabrication measurement",
    "frontier": "recent advances",
    "controversy": "conflicting evidence",
    "application": "deployment application",
}

_SCOPE_VALUES = {"direct", "adjacent", "contextual", "out_of_scope", "unreviewed"}
_DECISION_VALUES = {"approved", "rejected", "deferred"}
_ACTION_VALUES = {"materialize_now", "discovery_lead", "reject"}

# Structured audit outcomes are deliberately code-based.  The model may add a
# human-readable message, but executable routing never searches that message
# for English words.  This keeps the boundary contract stable across domains
# and across translated/model-generated audit text.
_HARD_SCOPE_VIOLATION_CODES = frozenset({
    "explicit_hard_boundary",
    "forbidden_regime",
    "modality_mismatch",
    "spectral_mismatch",
    "application_boundary_violation",
    "retrieved_paper_scope_boundary",
})
_SOFT_SCOPE_VIOLATION_CODES = frozenset({
    "chapter_stage_mismatch",
    "not_section_role",
    "adjacent_only",
    "scope_not_direct",
})
_SCOPE_VIOLATION_SEVERITIES = frozenset({"hard", "soft"})
_STOP_WORDS = {
    "about", "after", "again", "also", "between", "chapter", "from",
    "have", "into", "more", "only", "section", "such", "that", "their",
    "these", "this", "using", "with", "within", "without", "which",
}
_QUERY_BOILERPLATE = {
    "basis", "chapter", "conceptual", "coverage", "foundations", "gap",
    "gaps", "history", "literature", "principle", "principles",
    "query", "queries", "retrieval", "role", "section", "target", "targets",
    "workflow",
}
_GENERIC_TOPIC_OBJECT_TERMS = {
    "application", "beam", "control", "design", "device", "field", "image",
    "imaging", "mechanism", "method", "optics", "phase", "physical",
    "physics", "process", "system", "wavefront",
}

# Generic modality/spectral regimes used only when a section explicitly
# narrows its scope.  These are deliberately domain-neutral; the guard does
# not assume that the scientific object is a metasurface or even an optical
# device.
_EXPLICIT_REGIME_TERMS = {
    "optical_near_ir": (
        # Keep broad field nouns such as ``nonlinear optics`` and
        # ``integrated photonics`` out of the regime detector.  They describe
        # a scientific area, not necessarily the explicit spectral/modality
        # boundary that this guard is allowed to enforce.  ``optical`` and
        # the spectral terms remain available for phrases such as
        # ``optical/near-IR only``.
        "optical", "visible", "near-ir", "near ir", "near-infrared",
        "near infrared", "infrared", "nanophotonic", "nanophotonics",
    ),
    "microwave_rf": (
        "microwave", "microwaves", "radio-frequency", "radio frequency",
        "radiofrequency", "rf", "ghz",
    ),
    "acoustic": (
        "acoustic", "ultrasound", "sound wave", "sound-wave", "sonic",
    ),
    "terahertz": ("terahertz", "thz"),
    "x_ray": ("x-ray", "x ray", "xray"),
    "electron_beam": ("electron beam", "electron-beam", "electron microscopy"),
}
_NEGATIVE_SCOPE_RE = re.compile(
    r"\b(?:exclude|excluding|avoid|without|outside|beyond|not|do not|don't|no|"
    r"rather\s+than|as\s+opposed\s+to|instead\s+of)\b",
    re.IGNORECASE,
)
_POSITIVE_SCOPE_RE = re.compile(
    r"\b(?:focus(?:ed)?\s+(?:only\s+)?on|limited to|restricted to|"
    r"relevant to|confine(?:d)? to|only)\b",
    re.IGNORECASE,
)


def _regime_hits(text: Any) -> dict[str, int]:
    lower = " ".join(str(text or "").split()).casefold()
    hits: dict[str, int] = {}
    for regime, terms in _EXPLICIT_REGIME_TERMS.items():
        count = 0
        for term in terms:
            # Regime abbreviations such as ``rf`` must be whole tokens:
            # substring counting would classify ``metasurface`` as microwave.
            escaped = re.escape(term.casefold()).replace(r"\ ", r"\s+")
            count += len(
                re.findall(
                    rf"(?<![a-z0-9]){escaped}(?![a-z0-9])",
                    lower,
                )
            )
        if count:
            hits[regime] = count
    return hits


def _explicit_boundary_regimes(text: str) -> tuple[set[str], set[str]]:
    """Extract regimes attached to positive/negative boundary phrases."""

    def positive_marker_is_negated(start: int) -> bool:
        prefix = text[max(0, start - 24):start]
        return bool(
            re.search(
                r"\b(?:not|is\s+not|are\s+not|isn't|aren't)\s*$",
                prefix,
                re.IGNORECASE,
            )
        )

    def negative_marker_negates_boundary_phrase(end: int) -> bool:
        suffix = text[end:end + 40]
        return bool(
            re.match(
                r"\s+(?:(?:limited|restricted|confined)\s+to|only\b)",
                suffix,
                re.IGNORECASE,
            )
        )

    markers = [
        ("forbidden", match.start(), match.end())
        for match in _NEGATIVE_SCOPE_RE.finditer(text)
        if not negative_marker_negates_boundary_phrase(match.end())
    ] + [
        ("allowed", match.start(), match.end())
        for match in _POSITIVE_SCOPE_RE.finditer(text)
        if not positive_marker_is_negated(match.start())
    ]
    markers.sort(key=lambda item: (item[1], item[2]))
    allowed: set[str] = set()
    forbidden: set[str] = set()
    for index, (kind, _start, end) in enumerate(markers):
        next_start = next(
            (item[1] for item in markers[index + 1:] if item[1] >= end),
            len(text),
        )
        segment = text[end:next_start]
        hits = set(_regime_hits(segment))
        if kind == "allowed":
            # Handle the suffix form ``optical/near-IR only``.  A positive
            # marker at the end has no forward segment, so use the clause
            # before it only when no explicit negative marker is present.
            if not hits and not _NEGATIVE_SCOPE_RE.search(text):
                hits = set(_regime_hits(text[:end]))
            allowed.update(hits)
        else:
            forbidden.update(hits)
    return allowed, forbidden


def extract_explicit_scope_regimes(values: Iterable[Any]) -> Dict[str, Any]:
    """Extract only regimes attached to explicit scope-boundary language.

    Planner notes often mix rationale with one actionable clause.  Keeping the
    marker requirement here prevents ordinary mentions of neighbouring fields
    from silently becoming deny-list entries.
    """

    if isinstance(values, str):
        source_values: Iterable[Any] = (values,)
    else:
        source_values = values
    allowed: set[str] = set()
    forbidden: set[str] = set()
    matched_notes: list[str] = []
    for value in source_values:
        note = compact_text(value, 1200)
        if not note:
            continue
        note_allowed, note_forbidden = _explicit_boundary_regimes(note)
        if not note_allowed and not note_forbidden:
            continue
        allowed.update(note_allowed)
        forbidden.update(note_forbidden)
        matched_notes.append(note)
    return {
        "allowed_regimes": sorted(allowed),
        "forbidden_regimes": sorted(forbidden),
        "matched_notes": matched_notes,
    }


def assess_candidate_regime_boundary(
    candidate: Mapping[str, Any],
    *,
    allowed_regimes: Iterable[str] = (),
    forbidden_regimes: Iterable[str] = (),
) -> Dict[str, Any]:
    """Apply already-parsed regime constraints to bibliographic metadata."""

    allowed = {str(item) for item in allowed_regimes if str(item).strip()}
    forbidden = {str(item) for item in forbidden_regimes if str(item).strip()}
    title = str(candidate.get("title") or "")
    candidate_text = " ".join([title, str(candidate.get("abstract") or "")])
    candidate_hits = _regime_hits(candidate_text)
    title_hits = set(_regime_hits(title))
    incompatible = set(candidate_hits) & forbidden
    if allowed:
        incompatible.update(set(candidate_hits) - allowed)
    # A regime named in the title is a clear modality/spectral identity.  An
    # abstract-only mention must occur at least twice before it can trigger a
    # hard mismatch, avoiding rejection of a paper that merely compares a
    # neighbouring regime once.
    clear_incompatible = {
        regime
        for regime in incompatible
        if regime in title_hits or int(candidate_hits.get(regime, 0)) >= 2
    }
    common = {
        "allowed_regimes": sorted(allowed),
        "forbidden_regimes": sorted(forbidden),
        "candidate_regimes": sorted(candidate_hits),
    }
    if not clear_incompatible:
        return {
            "status": "compatible_or_ambiguous",
            "incompatible": False,
            **common,
        }
    return {
        "status": "incompatible_explicit_regime",
        "incompatible": True,
        **common,
        "incompatible_regimes": sorted(clear_incompatible),
        "reason": "candidate_regime_conflicts_with_explicit_section_guardrail",
    }


def assess_explicit_scope_boundary(
    section: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Dict[str, Any]:
    """Detect a clear regime mismatch only when section guardrails say so."""

    raw_guardrails = section.get("scope_guardrails") or []
    if isinstance(raw_guardrails, str):
        raw_guardrails = [raw_guardrails]
    guardrails = [
        compact_text(item, 240)
        for item in raw_guardrails
        if compact_text(item, 240)
    ]
    if not guardrails:
        return {
            "status": "no_explicit_boundary",
            "incompatible": False,
            "allowed_regimes": [],
            "forbidden_regimes": [],
            "candidate_regimes": [],
        }

    parsed = extract_explicit_scope_regimes(guardrails)
    allowed = set(parsed["allowed_regimes"])
    forbidden = set(parsed["forbidden_regimes"])

    if not allowed and not forbidden:
        return {
            "status": "no_supported_explicit_boundary",
            "incompatible": False,
            "allowed_regimes": [],
            "forbidden_regimes": [],
            "candidate_regimes": [],
        }

    return assess_candidate_regime_boundary(
        candidate,
        allowed_regimes=allowed,
        forbidden_regimes=forbidden,
    )


_CORE_EVIDENCE_SECTION_RE = re.compile(
    r"\b(?:results?|experimental|experiments?|methods?|materials\s+and\s+methods|"
    r"measurements?|characteri[sz]ation|implementation|evaluation|performance|"
    r"apparatus|setup)\b",
    re.IGNORECASE,
)
_CONTEXT_SECTION_RE = re.compile(
    r"\b(?:introduction|background|histor(?:y|ical)|perspective|related\s+work|"
    r"literature\s+review|motivation|overview|retrospective)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_ANALOGY_RE = re.compile(
    r"\b(?:analog(?:y|ous)|inspired\s+by|borrow(?:ed)?\s+from|follows?|"
    r"historically|originat(?:e|ed|ing)|background|precursor|counterpart)\b",
    re.IGNORECASE,
)


def _snippet_section_path(raw: Mapping[str, Any]) -> str:
    """Return the best available section label without assuming one schema."""

    direct = raw.get("section_path") or raw.get("section") or raw.get("section_title")
    if direct:
        return compact_text(direct, 240)
    for key in ("source_locator", "raw_metadata", "route_provenance"):
        nested = raw.get(key)
        if not isinstance(nested, Mapping):
            continue
        value = (
            nested.get("section_path")
            or nested.get("section")
            or nested.get("section_title")
            or nested.get("heading")
        )
        if value:
            return compact_text(value, 240)
    return ""


def _quantitative_forbidden_signatures(
    text: str,
    regimes: Iterable[str],
) -> list[str]:
    """Identify measured forbidden-regime signatures conservatively.

    A lone frequency unit is not enough for microwave/RF because optical
    devices can legitimately report modulation bandwidth in GHz.  It becomes
    decisive when the passage also names that regime, or reports multiple
    frequency operating points.  Other signatures likewise pair a numeric
    value with a regime-specific unit or modality term.
    """

    signatures: list[str] = []
    lower = " ".join(str(text or "").split()).casefold()
    numeric = r"\d+(?:\.\d+)?"
    for regime in regimes:
        matched = False
        if regime == "microwave_rf":
            frequencies = re.findall(
                rf"(?<![a-z0-9]){numeric}\s*(?:k|m|g)hz(?![a-z0-9])",
                lower,
            )
            named_regime = re.search(
                r"\b(?:microwaves?|radio[ -]?frequency|radiofrequency|rf|wireless)\b",
                lower,
            )
            matched = bool(frequencies) and (
                len(frequencies) >= 2 or named_regime is not None
            )
        elif regime == "terahertz":
            matched = bool(re.search(rf"\b{numeric}\s*thz\b", lower))
        elif regime == "acoustic":
            matched = bool(
                re.search(rf"\b{numeric}\s*(?:hz|khz|mhz)\b", lower)
                and re.search(r"\b(?:acoustic|ultrasound|sonic|sound[ -]?wave)\b", lower)
            )
        elif regime == "x_ray":
            matched = bool(
                re.search(rf"\b{numeric}\s*kev\b", lower)
                and re.search(r"\b(?:x[ -]?ray|xray)\b", lower)
            )
        elif regime == "electron_beam":
            matched = bool(
                re.search(rf"\b{numeric}\s*(?:kev|kv)\b", lower)
                and re.search(r"\belectron(?:[ -]?beam|\s+microscopy)\b", lower)
            )
        elif regime == "optical_near_ir":
            matched = bool(
                re.search(
                    rf"\b{numeric}\s*(?:nm|(?:micro|nano)?met(?:er|re)s?|um)\b",
                    lower,
                )
                and re.search(
                    r"\b(?:optical|visible|near[ -]?ir|near[ -]?infrared|infrared)\b",
                    lower,
                )
            )
        if matched:
            signatures.append(regime)
    return signatures


def assess_retrieved_paper_scope_boundary(
    section: Mapping[str, Any],
    candidate: Mapping[str, Any],
    retrieved_snippets: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Recheck one paper after retrieval using *all* returned snippets.

    Candidate metadata remains a hard boundary.  Body snippets are evaluated
    with their section path and aggregate paper context: a measured forbidden
    regime in Results/Methods is decisive, while a single historical analogy
    is retained as context.  Repeated or dominant forbidden-regime evidence
    can still quarantine a paper even when section labels are unavailable.
    """

    metadata = assess_explicit_scope_boundary(section, candidate)
    allowed = set(metadata.get("allowed_regimes") or [])
    forbidden = set(metadata.get("forbidden_regimes") or [])
    report: Dict[str, Any] = {
        "status": "no_explicit_boundary",
        "incompatible": False,
        "quarantine_all_snippets": False,
        "allowed_regimes": sorted(allowed),
        "forbidden_regimes": sorted(forbidden),
        "candidate_metadata": metadata,
        "snippet_evidence": [],
        "contextual_mentions": [],
        "decisive_conflicts": [],
        "aggregate_regime_hits": {"allowed": {}, "forbidden": {}},
        "incompatible_regimes": [],
        "observed_forbidden_regimes": [],
        "reason": "",
    }
    if not allowed and not forbidden:
        return report

    decisive_conflicts: list[dict[str, Any]] = []
    contextual_mentions: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    observed_forbidden: set[str] = set()
    allowed_totals: dict[str, int] = {regime: 0 for regime in allowed}
    forbidden_totals: dict[str, int] = {regime: 0 for regime in forbidden}
    forbidden_snippet_count = 0

    if metadata.get("incompatible"):
        decisive_conflicts.append({
            "source": "candidate_metadata",
            "reason": "candidate_metadata_explicit_hard_conflict",
            "matched_regimes": list(metadata.get("incompatible_regimes") or []),
            "title": compact_text(candidate.get("title"), 180),
        })

    for index, raw in enumerate(retrieved_snippets):
        if isinstance(raw, Mapping):
            text = str(raw.get("text") or raw.get("snippet") or "")
            chunk_id = str(raw.get("chunk_id") or raw.get("id") or "")
            section_path = _snippet_section_path(raw)
            chunk_metadata = {
                key: raw.get(key)
                for key in ("content_kind", "text_provenance", "source_locator")
                if raw.get(key) is not None
            }
        else:
            text = str(raw or "")
            chunk_id = ""
            section_path = ""
            chunk_metadata = {}
        hits = _regime_hits(text)
        matched = (set(hits) & forbidden) | (
            (set(hits) - allowed) if allowed else set()
        )
        for regime in allowed:
            allowed_totals[regime] = allowed_totals.get(regime, 0) + int(
                hits.get(regime, 0)
            )
        for regime in forbidden:
            forbidden_totals[regime] = forbidden_totals.get(regime, 0) + int(
                hits.get(regime, 0)
            )
        if not matched:
            continue
        forbidden_snippet_count += 1
        observed_forbidden.update(matched)
        quantitative = _quantitative_forbidden_signatures(text, matched)
        is_core = bool(_CORE_EVIDENCE_SECTION_RE.search(section_path))
        is_contextual = bool(
            _CONTEXT_SECTION_RE.search(section_path)
            or _CONTEXTUAL_ANALOGY_RE.search(text)
        )
        item = {
            "snippet_index": index,
            "chunk_id": chunk_id,
            "section_path": section_path,
            "section_class": (
                "core_evidence"
                if is_core
                else "contextual"
                if is_contextual
                else "unspecified"
            ),
            "matched_regimes": sorted(matched),
            "regime_hits": dict(hits),
            "quantitative_signatures": quantitative,
            "chunk_metadata": chunk_metadata,
            "text_preview": compact_text(text, 260),
        }
        all_evidence.append(item)
        if is_core and quantitative:
            decisive_conflicts.append({
                **item,
                "source": "retrieved_snippet",
                "reason": "quantitative_forbidden_signature_in_core_section",
            })
        else:
            contextual_mentions.append({
                **item,
                "source": "retrieved_snippet",
                "reason": (
                    "contextual_analogy_or_background_mention"
                    if is_contextual
                    else "single_non_decisive_forbidden_regime_mention"
                ),
            })

    allowed_total = sum(allowed_totals.values())
    forbidden_total = sum(forbidden_totals.values())
    repeated_and_not_outweighed = (
        forbidden_snippet_count >= 2
        and forbidden_total >= max(2, allowed_total)
    )
    dominant_forbidden = forbidden_total >= 3 and forbidden_total > allowed_total
    if (
        observed_forbidden
        and not decisive_conflicts
        and (repeated_and_not_outweighed or dominant_forbidden)
    ):
        decisive_conflicts.append({
            "source": "paper_aggregate",
            "reason": "repeated_or_dominant_forbidden_regime_evidence",
            "matched_regimes": sorted(observed_forbidden),
            "forbidden_snippet_count": forbidden_snippet_count,
            "allowed_hit_count": allowed_total,
            "forbidden_hit_count": forbidden_total,
        })

    decisive_regimes = {
        regime
        for item in decisive_conflicts
        for regime in item.get("matched_regimes", [])
    }
    report["snippet_evidence"] = all_evidence
    report["contextual_mentions"] = contextual_mentions
    report["decisive_conflicts"] = decisive_conflicts
    report["aggregate_regime_hits"] = {
        "allowed": {key: value for key, value in allowed_totals.items() if value},
        "forbidden": {key: value for key, value in forbidden_totals.items() if value},
    }
    report["observed_forbidden_regimes"] = sorted(observed_forbidden)
    report["incompatible_regimes"] = sorted(decisive_regimes)
    if decisive_conflicts:
        report.update({
            "status": (
                "candidate_metadata_boundary_conflict"
                if metadata.get("incompatible")
                else "retrieved_paper_has_decisive_boundary_conflict"
            ),
            "incompatible": True,
            "quarantine_all_snippets": True,
            "reason": str(decisive_conflicts[0].get("reason") or "decisive_boundary_conflict"),
        })
    else:
        report["status"] = (
            "retrieved_paper_contextual_forbidden_mentions_only"
            if contextual_mentions
            else "retrieved_paper_compatible_or_ambiguous"
        )
    return report


def normalize_scope_violation_records(value: Any) -> list[dict[str, Any]]:
    """Normalize optional structured scope/boundary audit fields.

    Old audit records may omit these fields.  String entries are retained as
    soft, unspecified records for backward compatibility, but their prose is
    never interpreted as a hard rejection signal.
    """

    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    normalized: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, Mapping):
            code = str(raw.get("code") or raw.get("type") or "unspecified").strip().casefold()
            severity = str(raw.get("severity") or "soft").strip().casefold()
            if severity not in _SCOPE_VIOLATION_SEVERITIES:
                severity = "soft"
            evidence = compact_text(
                raw.get("evidence") or raw.get("message") or raw.get("reason") or "",
                300,
            )
            normalized.append({
                "code": code or "unspecified",
                "severity": severity,
                "evidence": evidence,
            })
        elif str(raw).strip():
            normalized.append({
                "code": "unspecified",
                "severity": "soft",
                "evidence": compact_text(str(raw), 300),
            })
    return list(dict.fromkeys(
        (item["code"], item["severity"], item["evidence"])
        for item in normalized
    )) and [
        {"code": code, "severity": severity, "evidence": evidence}
        for code, severity, evidence in dict.fromkeys(
            (item["code"], item["severity"], item["evidence"])
            for item in normalized
        )
    ] or []


def scope_violation_outcome(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """Return deterministic hard/soft routing for structured audit fields."""

    records = normalize_scope_violation_records([
        *(candidate.get("scope_violations") or []),
        *(candidate.get("boundary_violations") or []),
    ])
    hard = [
        item for item in records
        if item["severity"] == "hard" or item["code"] in _HARD_SCOPE_VIOLATION_CODES
    ]
    soft = [
        item for item in records
        if item not in hard
        and (item["severity"] == "soft" or item["code"] in _SOFT_SCOPE_VIOLATION_CODES)
    ]
    return {
        "records": records,
        "hard": hard,
        "soft": soft,
        "hard_violation": bool(hard),
        "soft_violation": bool(soft),
    }

COVERAGE_OUTCOMES = (
    "material_ready",
    "material_ready_with_limits",
    "merge_required",
    "needs_more_literature",
)


def scalar_value(value: Any) -> str:
    """Return a stable string for plain values and enum instances."""

    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().casefold()


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return scalar_value(value) in {"1", "true", "yes", "y", "oa", "open"}


def compact_text(value: Any, limit: int = 480) -> str:
    """Collapse whitespace and cap text before it can enter a model payload."""

    text = " ".join(str(value or "").split())
    return text[: max(0, int(limit))]


_MOJIBAKE_MARKERS = ("鈥", "锟", "Ã", "Â", "â", "ï¿", "�")


def normalize_pipeline_text(value: Any) -> str:
    """Repair high-confidence UTF-8/Windows mojibake at input boundaries."""

    text = str(value or "")

    def score(candidate: str) -> int:
        return sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)

    best = text
    best_score = score(best)
    if best_score:
        for source_encoding in ("gbk", "latin1"):
            try:
                repaired = text.encode(source_encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if "�" in repaired:
                continue
            repaired_score = score(repaired)
            if repaired_score < best_score:
                best, best_score = repaired, repaired_score
    return best


def normalize_pipeline_structure(value: Any) -> Any:
    """Recursively normalize string values while preserving valid Unicode."""

    if isinstance(value, Mapping):
        return {
            key: normalize_pipeline_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_pipeline_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_pipeline_structure(item) for item in value)
    if isinstance(value, str):
        return normalize_pipeline_text(value)
    return value


def _has_http_url(value: Any) -> bool:
    return str(value or "").strip().startswith(("http://", "https://"))


def _has_typed_value(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.casefold() not in {
        "none", "null", "nan", "unknown", "n/a",
    }


_OA_URL_FIELDS = (
    "pdf_url", "oa_url", "open_access_url", "url_for_pdf",
    "best_oa_url", "html_url", "repository_url",
)
_S2_ID_FIELDS = (
    "semantic_scholar_id", "semantic_scholar_paper_id", "corpus_id",
)
_LOCAL_FULLTEXT_FIELDS = (
    "local_fulltext_path", "local_download_path", "fulltext_path",
    "parsed_text_path", "local_file_path", "local_fulltext",
    "has_local_fulltext",
)
_OA_STATUS_VALUES = {"yes", "gold", "green", "hybrid", "oa", "open", "open_access"}
_STRUCTURED_ROUTE_VALUES = {
    "s2_structured_body",
    "s2_structured_body_snippet",
    "s2_direct_structured_snippet",
    "structured_snippet",
    "structured_body",
}
_LOCAL_FULLTEXT_ROUTE_VALUES = {
    "local_fulltext",
    "local_prior_fulltext",
    "reused_local_fulltext",
    "reused_local_asset",
}


def _candidate_s2_identity(candidate: Mapping[str, Any]) -> bool:
    return any(
        _has_typed_value(candidate.get(key))
        for key in _S2_ID_FIELDS
    )


def _route_provenance(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    value = candidate.get("route_provenance")
    return value if isinstance(value, Mapping) else {}


def _candidate_has_s2_structured_route(candidate: Mapping[str, Any]) -> bool:
    """Return whether an S2 identity can reach the structured-body path.

    A Semantic Scholar identity is a typed handle for the bounded structured
    retriever.  It is not evidence that a body chunk has already been
    downloaded; it is the executable S2 route that can produce one.  Explicit
    body/chunk metadata is also accepted for candidates restored from a later
    materialisation stage.
    """

    if not _candidate_s2_identity(candidate):
        return False
    route = _route_provenance(candidate)
    availability = candidate.get("text_availability")
    availability = availability if isinstance(availability, Mapping) else {}
    availability_route = _route_provenance(availability)
    route_values = {
        scalar_value(candidate.get("materialization_route")),
        scalar_value(route.get("materialization_route")),
        scalar_value(candidate.get("content_depth")),
        scalar_value(candidate.get("text_provenance")),
        scalar_value(availability.get("materialization_route")),
        scalar_value(availability.get("content_depth")),
        scalar_value(availability.get("text_provenance")),
        scalar_value(availability_route.get("materialization_route")),
    }
    route_values.discard("")
    chunk_values = [
        *(candidate.get("chunk_ids") or []),
        *(candidate.get("canonical_chunk_ids") or []),
        *(candidate.get("new_chunk_ids") or []),
    ]
    has_structured_chunk = any(
        str(value or "").strip().casefold().startswith("s2chunk:")
        for value in chunk_values
    )
    has_structured_body_marker = any(
        bool(candidate.get(key)) or bool(availability.get(key))
        for key in (
            "structured_body", "structured_snippet", "s2_structured_body",
            "s2_body_snippet",
        )
    )
    # The identity itself is sufficient for a fresh S2 structured-body
    # attempt.  The additional signals make restored/materialized records
    # self-describing without changing the meaning of the route.
    return bool(
        has_structured_chunk
        or has_structured_body_marker
        or route_values & _STRUCTURED_ROUTE_VALUES
        or _candidate_s2_identity(candidate)
    )


def _candidate_has_local_fulltext_route(candidate: Mapping[str, Any]) -> bool:
    route = _route_provenance(candidate)
    availability = candidate.get("text_availability")
    availability = availability if isinstance(availability, Mapping) else {}
    route_values = {
        scalar_value(candidate.get("materialization_route")),
        scalar_value(route.get("materialization_route")),
        scalar_value(candidate.get("content_depth")),
        scalar_value(candidate.get("source_kind")),
        scalar_value(availability.get("materialization_route")),
        scalar_value(availability.get("content_depth")),
        scalar_value(availability.get("source_kind")),
    }
    has_local_path = any(
        _has_typed_value(candidate.get(key))
        or _has_typed_value(availability.get(key))
        for key in _LOCAL_FULLTEXT_FIELDS
    )
    return bool(
        has_local_path
        or route_values & _LOCAL_FULLTEXT_ROUTE_VALUES
        or (
            (
                scalar_value(candidate.get("content_depth")) == "fulltext"
                or scalar_value(availability.get("content_depth")) == "fulltext"
            )
            and (
                scalar_value(candidate.get("local_prior")) in {"1", "true", "yes"}
                or scalar_value(availability.get("local_prior")) in {"1", "true", "yes"}
            )
        )
    )


def candidate_has_legal_route(candidate: Mapping[str, Any]) -> bool:
    """Return whether a candidate has a legal, usable Phase 2 route.

    Scientific approval and material acquisition are intentionally separate.
    A plain backend label, DOI, abstract, or OA flag is not a route.  The
    route must be an explicitly OA-marked URL, a typed S2 structured-body
    handle, or a local full-text asset/route.  Institutional access is not
    inferred here because this contract governs the OA-only Phase 2 path.
    """

    oa_marked = bool_value(candidate.get("is_oa")) or scalar_value(
        candidate.get("oa_status")
    ) in _OA_STATUS_VALUES
    has_oa_url = oa_marked and any(
        _has_http_url(candidate.get(key))
        for key in _OA_URL_FIELDS
    )
    if not has_oa_url and oa_marked:
        alternate_urls = candidate.get("alternate_urls")
        alternate_urls = alternate_urls if isinstance(alternate_urls, Sequence) else []
        content_urls = candidate.get("content_urls")
        content_urls = content_urls if isinstance(content_urls, Mapping) else {}
        has_oa_url = any(
            _has_http_url(value)
            for value in alternate_urls
        ) or any(
            _has_http_url(value)
            for value in content_urls.values()
        )
    return bool(
        has_oa_url
        or _candidate_has_s2_structured_route(candidate)
        or _candidate_has_local_fulltext_route(candidate)
    )


@dataclass(frozen=True)
class CandidateDecisionContract:
    """Canonical, executable interpretation of one candidate audit record."""

    decision: str
    scope_fit: str
    action: str
    state: str
    can_materialize: bool
    route_available: bool
    reason: str


def canonical_candidate_decision(
    candidate: Mapping[str, Any],
    requested_action: Any = "",
) -> CandidateDecisionContract:
    """Map all candidate fields to one fail-closed executable state.

    ``requested_action`` is intentionally only an audit hint.  It can never
    promote a deferred/rejected/out-of-scope candidate, and approval alone is
    not acquisition permission.  Direct and adjacent approved candidates are
    materialised only when a legal OA, S2 structured-body, or local full-text
    route is already represented in the record.  Otherwise they remain
    discovery leads for a later route-bearing pass.
    """

    del requested_action  # retained in the signature for caller compatibility
    decision = scalar_value(candidate.get("decision")) or "deferred"
    scope_fit = scalar_value(candidate.get("scope_fit")) or "unreviewed"
    if decision not in _DECISION_VALUES:
        decision = "deferred"
    if scope_fit not in _SCOPE_VALUES:
        scope_fit = "unreviewed"
    violation_state = scope_violation_outcome(candidate)
    route_available = candidate_has_legal_route(candidate)

    if violation_state["hard_violation"]:
        return CandidateDecisionContract(
            decision="rejected",
            scope_fit="out_of_scope",
            action="reject",
            state="rejected_structured_scope_violation",
            can_materialize=False,
            route_available=route_available,
            reason="explicit_structured_scope_violation",
        )
    if (
        decision == "approved"
        and scope_fit in {"direct", "adjacent"}
        and violation_state["soft_violation"]
    ):
        return CandidateDecisionContract(
            decision=decision,
            scope_fit=scope_fit,
            action="discovery_lead",
            state="approved_soft_scope_mismatch",
            can_materialize=False,
            route_available=route_available,
            reason="soft_structured_scope_mismatch_requires_discovery_only",
        )

    if (
        decision == "approved"
        and scope_fit in {"direct", "adjacent"}
        and route_available
    ):
        return CandidateDecisionContract(
            decision=decision,
            scope_fit=scope_fit,
            action="materialize_now",
            state="approved_materialization_candidate",
            can_materialize=True,
            route_available=route_available,
            reason="approved_scope_has_legal_materialization_route",
        )
    if decision == "approved" and scope_fit in {"direct", "adjacent"}:
        return CandidateDecisionContract(
            decision=decision,
            scope_fit=scope_fit,
            action="discovery_lead",
            state="approved_discovery_lead",
            can_materialize=False,
            route_available=False,
            reason=(
                "approved_scope_without_legal_materialization_route; "
                "materialize_now_clamped_to_discovery_lead"
            ),
        )
    if decision == "approved" and scope_fit == "contextual":
        return CandidateDecisionContract(
            decision=decision,
            scope_fit=scope_fit,
            action="discovery_lead",
            state="approved_contextual_background",
            can_materialize=False,
            route_available=route_available,
            reason="contextual_sources cannot satisfy section coverage",
        )
    if decision == "deferred":
        return CandidateDecisionContract(
            decision=decision,
            scope_fit=scope_fit,
            action="reject",
            state="deferred_pending_audit",
            can_materialize=False,
            route_available=route_available,
            reason="explicit approval is required before materialization",
        )
    return CandidateDecisionContract(
        decision=decision,
        scope_fit=scope_fit,
        action="reject",
        state="rejected_or_scope_invalid",
        can_materialize=False,
        route_available=route_available,
        reason="candidate is not approved in an executable section scope",
    )


def candidate_is_materializable(candidate: Mapping[str, Any]) -> bool:
    contract = canonical_candidate_decision(candidate)
    return bool(
        contract.can_materialize
        and contract.decision == "approved"
        and contract.action == "materialize_now"
        and contract.route_available
    )


@dataclass(frozen=True)
class JsonDecodeResult:
    value: Any = None
    recovered: bool = False
    error: str = ""


def _balanced_json_fragment(text: str) -> str:
    """Extract one balanced JSON object/array after harmless prose."""

    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return ""
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                suffix = text[index + 1 :].strip()
                if suffix and suffix.strip("`").strip():
                    return ""
                return text[start : index + 1]
    return ""


def decode_json_payload(
    raw: Any,
    *,
    expected: str = "any",
    allow_single_object_for_list: bool = True,
) -> JsonDecodeResult:
    """Recover only safe JSON transport noise, never malformed decisions.

    Accepted recovery is limited to a UTF-8 BOM, Markdown code fences, a
    short prose prefix, and trailing commas.  Python literals, single-quoted
    objects, unclosed JSON, and arbitrary suffixes remain errors.
    """

    if not isinstance(raw, str):
        return JsonDecodeResult(error="payload must be a JSON string")
    original = raw
    text = raw.lstrip("\ufeff").strip()
    candidates: List[tuple[str, bool]] = [(text, text != original)]
    if text.startswith("```"):
        fenced = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        fenced = re.sub(r"\s*```$", "", fenced).strip()
        candidates.insert(0, (fenced, True))
    fragment = _balanced_json_fragment(text)
    if fragment and fragment != text:
        candidates.append((fragment, True))

    last_error = "invalid JSON"
    for candidate, recovered in candidates:
        variants = [(candidate, recovered)]
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        if repaired != candidate:
            variants.append((repaired, True))
        for variant, was_recovered in variants:
            try:
                value = json.loads(variant)
            except (TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                continue
            if expected == "object" and not isinstance(value, dict):
                last_error = "JSON payload must be an object"
                continue
            if expected == "list":
                if isinstance(value, dict) and allow_single_object_for_list:
                    value = [value]
                if not isinstance(value, list):
                    last_error = "JSON payload must be an array"
                    continue
            return JsonDecodeResult(value=value, recovered=was_recovered)
    return JsonDecodeResult(error=f"Invalid JSON: {last_error}")


def _normalise_terms(value: Any, limit: int = 8) -> List[str]:
    text = compact_text(value, 240).casefold()
    tokens = re.findall(r"[a-z0-9][a-z0-9+./-]{2,}", text)
    result: List[str] = []
    for token in tokens:
        if token in _STOP_WORDS or token in result:
            continue
        result.append(token)
        if len(result) >= limit:
            break
    return result


def _scientific_query_terms(value: Any, limit: int = 12) -> List[str]:
    """Extract stable scientific terms while dropping workflow boilerplate."""

    text = compact_text(value, 320).casefold()
    tokens = re.findall(r"[a-z0-9][a-z0-9+./-]{2,}", text)
    result: List[str] = []
    for token in tokens:
        if token in _STOP_WORDS or token in _QUERY_BOILERPLATE or token in result:
            continue
        result.append(token)
        if len(result) >= max(1, int(limit)):
            break
    return result


def _component_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("component", "name", "label", "description", "claim"):
            if str(value.get(key) or "").strip():
                return compact_text(value.get(key), 120)
        return ""
    return compact_text(value, 120)


def scientific_query_anchor(section_data: Mapping[str, Any]) -> str:
    identity = section_data.get("topic_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    scientific_object = compact_text(identity.get("scientific_object"), 120)
    title = compact_text(section_data.get("title"), 100)
    fallback = compact_text(section_data.get("scope_description"), 100)
    anchor_terms = _scientific_query_terms(
        scientific_object or title or fallback,
        limit=7,
    )
    for item in identity.get("core_anchor_tokens", []):
        anchor_terms.extend(_scientific_query_terms(item, limit=2))
    anchor_terms = list(dict.fromkeys(anchor_terms))[:9]
    return " ".join(anchor_terms) or "section-specific scientific evidence"


def normalize_scientific_query(
    query: Any,
    *,
    section_data: Mapping[str, Any] | None = None,
    components: Iterable[Any] = (),
    role: str = "",
    max_terms: int = 10,
    max_chars: int = 120,
) -> str:
    """Build a bounded provider query without dropping explicit components.

    The anchor and component terms are the semantic payload.  Role terms are
    appended only when they fit the deterministic term/character budget, so a
    long Phase-3 request cannot crowd the scientific object out of retrieval.
    """

    section_data = section_data if isinstance(section_data, Mapping) else {}
    anchor = scientific_query_anchor(section_data) if section_data else ""
    anchor_terms = _scientific_query_terms(anchor, limit=8)
    component_terms: List[str] = []
    for value in components:
        component_terms.extend(_scientific_query_terms(_component_name(value), limit=5))
    explicit_terms = _scientific_query_terms(query, limit=12)
    role_key = scalar_value(role)
    role_terms = _scientific_query_terms(ROLE_QUERY_TERMS.get(role_key, ""), limit=3)

    # Components are mandatory once supplied.  Keep the anchor ahead of them
    # for recall, but use the remaining capacity for every component before
    # optional query/role facet terms.
    mandatory = list(dict.fromkeys(anchor_terms + component_terms))
    if len(mandatory) > max(1, int(max_terms)):
        component_unique = list(dict.fromkeys(component_terms))
        anchor_unique = [term for term in anchor_terms if term not in component_unique]
        term_cap = max(1, int(max_terms))
        if len(component_unique) < term_cap:
            mandatory = (
                anchor_unique[: term_cap - len(component_unique)]
                + component_unique
            )
        else:
            mandatory = component_unique[:term_cap]

    ordered = list(dict.fromkeys(
        [*mandatory, *explicit_terms, *role_terms]
    ))
    selected: List[str] = []
    limit_terms = max(1, int(max_terms))
    limit_chars = max(24, int(max_chars))
    mandatory_set = set(mandatory)
    for term in ordered:
        if len(selected) >= limit_terms:
            break
        candidate = " ".join([*selected, term])
        if len(candidate) <= limit_chars:
            selected.append(term)
            continue
        # Character pressure may affect optional facets, but never makes a
        # normal component disappear.  The provider receives a bounded query;
        # the target remains in the returned target's component metadata.
        if term in mandatory_set:
            continue
    if not selected:
        selected = _scientific_query_terms(query, limit=limit_terms) or [
            "scientific", "evidence"
        ]
    return " ".join(selected)[:limit_chars].strip()


def candidate_query_affinity(
    candidate: Mapping[str, Any],
    queries: Iterable[str],
    *,
    topic_fingerprint: str = "",
    exact_topic_fingerprint: str = "",
) -> Dict[str, Any]:
    """Require meaningful query/content overlap before portfolio reuse."""

    if (
        exact_topic_fingerprint
        and topic_fingerprint
        and exact_topic_fingerprint == topic_fingerprint
    ):
        return {"accepted": True, "score": 1.0, "reason": "exact_topic_fingerprint"}
    content = " ".join(
        [
            str(candidate.get("title") or ""),
            str(candidate.get("abstract") or ""),
            str(candidate.get("tldr") or ""),
        ]
    )
    def affinity_terms(value: Any) -> set[str]:
        terms = set(_scientific_query_terms(value, limit=80))
        # Provider titles/abstracts commonly switch between singular and
        # plural scientific labels (mechanism/mechanisms, lens/lenses).
        terms.update(
            token[:-1]
            for token in list(terms)
            if token.endswith("s") and len(token) > 4 and not token.endswith("ss")
        )
        return terms

    content_terms = affinity_terms(content)
    # A stored query is supporting provenance, not a substitute for paper
    # content.  It is used only when metadata is unavailable (older ledgers).
    if not content_terms:
        content_terms = affinity_terms(
            " ".join(str(item) for item in candidate.get("query_texts") or []),
        )
    best: Dict[str, Any] = {"accepted": False, "score": 0.0, "reason": "no_query_affinity"}
    for raw_query in queries:
        query_terms = affinity_terms(raw_query)
        overlap = sorted(query_terms & content_terms)
        if len(query_terms) < 2 or len(overlap) < 2:
            continue
        score = len(overlap) / max(1, len(query_terms))
        if score >= 0.5 and score > float(best.get("score") or 0.0):
            best = {
                "accepted": True,
                "score": round(score, 3),
                "reason": "meaningful_query_overlap",
                "overlap_terms": overlap,
                "matched_query": compact_text(raw_query, 120),
            }
    return best


def evaluate_candidate_topic_affinity(
    candidate: Mapping[str, Any],
    section_data: Mapping[str, Any],
    *,
    queries: Iterable[str] = (),
    components: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Fail closed on candidates lacking the section's scientific object."""

    identity = section_data.get("topic_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    if not bool_value(identity.get("valid")):
        return {"accepted": True, "scope_fit": "unreviewed", "reason": "topic_contract_unavailable"}
    title = str(candidate.get("title") or "")
    abstract = str(candidate.get("abstract") or "")
    content = " ".join([title, abstract])
    from optomind_research.runtime.topic_identity import assess_topic_alignment, topic_tokens

    content_tokens = set(topic_tokens(content))
    object_tokens = set(topic_tokens(str(identity.get("scientific_object") or "")))
    core_tokens = set(topic_tokens(" ".join(str(item) for item in identity.get("core_anchor_tokens") or [])))
    supporting_tokens = set(
        topic_tokens(" ".join(str(item) for item in identity.get("supporting_anchor_tokens") or []))
    )
    component_tokens = set(topic_tokens(" ".join(_component_name(item) for item in components)))
    query_tokens = set(topic_tokens(" ".join(str(item) for item in queries)))
    distinctive_object_tokens = object_tokens - _GENERIC_TOPIC_OBJECT_TERMS
    object_specific_hits = sorted(content_tokens & distinctive_object_tokens)
    object_hits = sorted(content_tokens & (object_tokens | core_tokens))
    bridge_hits = sorted(content_tokens & (component_tokens | query_tokens))
    alignment = assess_topic_alignment(content, dict(identity), strict=True)
    passed = str(alignment.get("status") or "").casefold() == "passed"
    has_primary_object = (
        bool(object_specific_hits)
        if distinctive_object_tokens
        else len(object_hits) >= 2
    )
    bridge_object_hits = sorted(
        content_tokens
        & ((component_tokens | supporting_tokens) - _GENERIC_TOPIC_OBJECT_TERMS)
    )
    direct = passed and has_primary_object and (
        len(content_tokens & core_tokens) >= 2 or len(bridge_hits) >= 2
    )
    adjacent_bridge = (
        passed
        and (has_primary_object or bool(bridge_object_hits))
        and len(bridge_hits) >= 2
    )
    if direct:
        return {
            "accepted": True,
            "scope_fit": "direct",
            "topic_alignment": alignment,
            "object_hits": object_hits,
            "object_specific_hits": object_specific_hits,
            "bridge_object_hits": bridge_object_hits,
            "bridge_hits": bridge_hits,
            "reason": "topic_object_and_anchor_match",
        }
    if adjacent_bridge and passed:
        return {
            "accepted": True,
            "scope_fit": "adjacent",
            "topic_alignment": alignment,
            "object_hits": object_hits,
            "object_specific_hits": object_specific_hits,
            "bridge_object_hits": bridge_object_hits,
            "bridge_hits": bridge_hits,
            "reason": "explicit_topic_bridge",
        }
    return {
        "accepted": False,
        "scope_fit": "out_of_scope",
        "topic_alignment": alignment,
        "object_hits": object_hits,
        "object_specific_hits": object_specific_hits,
        "bridge_object_hits": bridge_object_hits,
        "bridge_hits": bridge_hits,
        "reason": "missing_topic_object_or_explicit_bridge",
    }


def build_uncovered_query_targets(
    section_data: Mapping[str, Any],
    *,
    roles: Iterable[str] = (),
    components: Iterable[Any] = (),
    existing_targets: Iterable[Mapping[str, Any]] = (),
    max_targets: int = 8,
) -> List[Dict[str, Any]]:
    """Create compact deterministic queries for every uncovered role/component."""

    ordered_roles = [role for role in COVERAGE_ROLES if role in {scalar_value(item) for item in roles}]
    component_names = list(dict.fromkeys(
        _component_name(item) for item in components if _component_name(item)
    ))[:8]
    anchor = scientific_query_anchor(section_data)
    targets: List[Dict[str, Any]] = []
    seen_queries: set[str] = set()

    for raw in existing_targets:
        if not isinstance(raw, Mapping):
            continue
        raw_components = list(raw.get("components") or raw.get("missing_components") or [])
        role = scalar_value(raw.get("role"))
        query = normalize_scientific_query(
            raw.get("query"),
            section_data=section_data,
            components=raw_components,
            role=role,
        )
        if not query:
            continue
        key = " ".join(query.casefold().split())
        if key in seen_queries:
            continue
        seen_queries.add(key)
        targets.append({
            "query": query,
            "role": role if role in COVERAGE_ROLES else "",
            "components": raw_components,
            "missing_components": list(raw.get("missing_components") or raw_components),
            "source": str(raw.get("source") or "phase3_request"),
        })

    if not ordered_roles and component_names:
        ordered_roles = ["foundation"]
    for role in ordered_roles:
        role_components = component_names or [""]
        for component in role_components[:2]:
            query = normalize_scientific_query(
                anchor,
                section_data=section_data,
                components=[component] if component else (),
                role=role,
            )
            key = " ".join(query.casefold().split())
            if not query or key in seen_queries:
                continue
            seen_queries.add(key)
            target_components = [component] if component else []
            targets.append({
                "query": query,
                "role": role,
                "components": target_components,
                "missing_components": target_components,
                "source": "deterministic_uncovered_gap",
            })
            if len(targets) >= max(1, int(max_targets)):
                return targets[:max_targets]
    return targets[:max_targets]


def derive_uncovered_roles(
    section_data: Mapping[str, Any],
    *,
    audit: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    source_ledger: Mapping[str, Any] | None = None,
) -> List[str]:
    """Derive unresolved role names from durable artifacts, not model memory."""

    audit = audit if isinstance(audit, Mapping) else {}
    plan = plan if isinstance(plan, Mapping) else {}
    source_ledger = source_ledger if isinstance(source_ledger, Mapping) else {}
    explicit = section_data.get("phase3_coverage_request") or {}
    explicit = explicit if isinstance(explicit, Mapping) else {}
    explicit_missing = {
        scalar_value(item)
        for item in explicit.get("missing_roles") or []
        if scalar_value(item) in COVERAGE_ROLES
    }
    candidates = {
        scalar_value(item)
        for item in [
            *(section_data.get("required_roles") or []),
        ]
        if scalar_value(item) in COVERAGE_ROLES
    }
    candidates.update(explicit_missing)
    role_audits = audit.get("role_audits") or {}
    explicitly_uncovered = set(explicit_missing)
    for role, value in role_audits.items():
        if role not in COVERAGE_ROLES or not isinstance(value, Mapping):
            continue
        if value.get("coverage_verdict") in {"none", "partial"} or value.get("gap_severity") == "blocking":
            candidates.add(role)
            explicitly_uncovered.add(role)
    for role, value in (plan.get("roles") or {}).items():
        if role not in COVERAGE_ROLES or not isinstance(value, Mapping):
            continue
        if value.get("priority") == "required" and value.get("gap_severity") in {"blocking", "important", "unknown"}:
            candidates.add(role)

    source_counts: Dict[str, int] = {role: 0 for role in COVERAGE_ROLES}
    for source in source_ledger.get("sources", []) or []:
        if not isinstance(source, Mapping):
            continue
        role = scalar_value(source.get("literature_role"))
        if role in source_counts and source.get("canonical_chunk_ids"):
            source_counts[role] += 1
    # A required role is a target, not proof that it is still uncovered.  Once
    # the durable audit or accepted source ledger closes it, remove it from the
    # generated query set unless an explicit missing-role signal overrides it.
    for role in list(candidates):
        audit_entry = role_audits.get(role)
        audit_sufficient = (
            isinstance(audit_entry, Mapping)
            and audit_entry.get("coverage_verdict") == "sufficient"
            and audit_entry.get("gap_severity") != "blocking"
        )
        if (
            role not in explicitly_uncovered
            and (audit_sufficient or source_counts[role] > 0)
        ):
            candidates.discard(role)
    if not candidates and source_ledger.get("breadth_target_met") is False:
        candidates.add(min(COVERAGE_ROLES, key=lambda role: (source_counts[role], COVERAGE_ROLES.index(role))))
    return [role for role in COVERAGE_ROLES if role in candidates]


def closed_scientific_components(
    query_targets: Iterable[Mapping[str, Any]],
    accepted_sources: Iterable[Mapping[str, Any]],
) -> List[str]:
    """Return deterministic component keys backed by accepted text sources."""

    sources = [item for item in accepted_sources if isinstance(item, Mapping)]
    closed: List[str] = []
    for target in query_targets:
        if not isinstance(target, Mapping):
            continue
        role = scalar_value(target.get("role"))
        components = list(target.get("components") or target.get("missing_components") or [])
        components = [_component_name(item) for item in components if _component_name(item)]
        relevant = [
            source for source in sources
            if role and scalar_value(source.get("literature_role")) == role
        ]
        if not relevant:
            continue
        for component in components:
            if component not in closed:
                closed.append(component)
        if not components and role and role not in closed:
            closed.append(role)
    return closed


@dataclass(frozen=True)
class CoverageReadiness:
    structural_task_complete: bool
    scientific_coverage_ready: bool
    outcome: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structural_task_complete": self.structural_task_complete,
            "scientific_coverage_ready": self.scientific_coverage_ready,
            "outcome": self.outcome,
            "reason": self.reason,
        }


def evaluate_coverage_readiness(
    *,
    required_artifacts: Sequence[str],
    work_dir_exists: Any,
    package: Mapping[str, Any] | None,
) -> CoverageReadiness:
    package = package if isinstance(package, Mapping) else {}
    # ``work_dir_exists`` is supplied as the actual artifact-presence result by
    # callers; keeping this function I/O-free makes it usable in unit tests.
    structural = bool(work_dir_exists) and bool(package)
    adaptive_outcome = scalar_value(
        package.get("coverage_outcome") or package.get("readiness_outcome")
    )
    if structural and adaptive_outcome in COVERAGE_OUTCOMES:
        if adaptive_outcome == "needs_more_literature":
            return CoverageReadiness(
                True,
                False,
                adaptive_outcome,
                str(package.get("coverage_reason") or "adaptive contract has unresolved load-bearing evidence gaps"),
            )
        return CoverageReadiness(
            True,
            True,
            adaptive_outcome,
            str(package.get("coverage_reason") or "adaptive section contract is writable"),
        )
    coverage_status = scalar_value(package.get("coverage_status"))
    breadth_met = package.get("breadth_target_met") is True
    open_gaps = bool(package.get("blocking_gaps_remain")) or coverage_status not in {"coverage_sufficient", "sufficient"}
    scientific = structural and breadth_met and not open_gaps
    if scientific:
        return CoverageReadiness(True, True, "completed", "scientific coverage and breadth gates passed")
    if structural:
        return CoverageReadiness(True, False, "needs_more_literature", "package is structurally complete but scientific coverage is not ready")
    return CoverageReadiness(False, False, "failed", "required structural artifacts are incomplete")


@dataclass(frozen=True)
class AuditCallAdmission:
    """Pre-call result for the one-batched-audit-per-wave rule."""

    admitted: bool
    reason: str
    wave_index: int
    predicted_input_tokens: int
    output_reserve_tokens: int
    cumulative_after_reserve: int
    audit_calls_after: int


def estimate_json_tokens(payload: Any) -> int:
    """Conservative, dependency-free token estimate for compact JSON payloads."""

    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(payload)
    # Four characters/token is deliberately conservative for mixed scientific
    # text and keeps the estimate deterministic across tokenizer versions.
    return max(1, (len(encoded) + 3) // 4)


def _load_qwen_pricing(model_name: str, input_tokens: int) -> tuple[float, float, str]:
    """Resolve configured CNY rates without contacting a provider."""

    default = (12.0, 54.0, "config_default")
    try:
        path = Path(__file__).resolve().parents[2] / "config" / "model_pricing.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        default_data = data.get("default") if isinstance(data, Mapping) else {}
        fallback = (
            float((default_data or {}).get("input_cny_per_million", default[0])),
            float((default_data or {}).get("output_cny_per_million", default[1])),
            "config_default",
        )
        models = data.get("models") if isinstance(data, Mapping) else {}
        rows = models.get(model_name) if isinstance(models, Mapping) else None
        if not isinstance(rows, list) or not rows:
            return fallback
        ordered = sorted(
            (item for item in rows if isinstance(item, Mapping)),
            key=lambda item: int(item.get("max_input_tokens") or 0),
        )
        selected = ordered[-1] if ordered else None
        for item in ordered:
            if int(input_tokens or 0) <= int(item.get("max_input_tokens") or 0):
                selected = item
                break
        if selected is None:
            return fallback
        return (
            float(selected.get("input_cny_per_million", fallback[0])),
            float(selected.get("output_cny_per_million", fallback[1])),
            "config_model",
        )
    except Exception:
        return default


def normalize_qwen_usage(
    response: Mapping[str, Any] | None,
    *,
    fallback_input_tokens: int = 0,
    fallback_output_tokens: int = 0,
    model_tier: str | None = None,
    model_name: str | None = None,
) -> Dict[str, Any]:
    """Normalize provider, wrapper, and deterministic Qwen usage shapes once.

    Qwen wrappers have returned top-level counts, OpenAI-compatible
    ``usage.prompt_tokens``/``completion_tokens``, and ``_llm_usage``
    estimates over time.  Prefer provider-reported counts; otherwise use the
    bounded short-path estimate that was admitted before the call.  If
    provider cost is absent and a model tier is supplied, derive an explicitly
    labelled estimate from the repository pricing configuration.
    """

    raw = response if isinstance(response, Mapping) else {}
    containers: list[Mapping[str, Any]] = [raw]
    for key in ("usage", "_llm_usage", "response_metadata", "meta"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            containers.append(value)

    def first_int(keys: tuple[str, ...]) -> tuple[int, str]:
        for container in containers:
            for key in keys:
                value = container.get(key)
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number >= 0:
                    return number, key
        return 0, ""

    input_tokens, input_source = first_int(
        (
            "input_tokens", "prompt_tokens", "input_token_count",
            "prompt_token_count", "promptTokens", "estimated_input_tokens",
        )
    )
    output_tokens, output_source = first_int(
        (
            "output_tokens", "completion_tokens", "output_token_count",
            "completion_token_count", "completionTokens", "estimated_output_tokens",
        )
    )
    if not input_tokens:
        input_tokens = max(0, int(fallback_input_tokens or 0))
        input_source = "bounded_fallback" if input_tokens else "missing"
    if not output_tokens:
        output_tokens = max(0, int(fallback_output_tokens or 0))
        output_source = "bounded_fallback" if output_tokens else "missing"

    cost = 0.0
    cost_source = "unavailable"
    cost_estimated = False
    for container in containers:
        for key in ("cost_cny", "total_cost_cny", "estimated_cost_cny"):
            value = container.get(key)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed < 0:
                continue
            cost = parsed
            cost_source = "estimated_reported" if "estimated" in key else "provider_reported"
            cost_estimated = "estimated" in key
            break
        if cost_source != "unavailable":
            break

    resolved_model = str(model_name or "").strip()
    if not resolved_model:
        for container in containers:
            for key in ("model_name", "model", "resolved_model"):
                value = str(container.get(key) or "").strip()
                if value:
                    resolved_model = value
                    break
            if resolved_model:
                break
    if model_tier and not resolved_model:
        try:
            from config.qwen_config import get_model_name

            resolved_model = str(get_model_name(model_tier) or "").strip()
        except Exception:
            resolved_model = str(model_tier).strip()
    pricing_source = ""
    if cost_source == "unavailable" and model_tier:
        # Keep the fallback price calculation in the same ledger used by the
        # runtime worker.  This prevents the section-coverage path from
        # silently drifting away from the repository's audited list prices.
        try:
            from .cost_ledger import estimate_call_cost_cny, load_model_pricing

            cost = estimate_call_cost_cny(
                resolved_model,
                input_tokens,
                output_tokens,
            )
            pricing_source = str(
                load_model_pricing().get("pricing_source")
                or "configured_list_price"
            )
        except Exception:
            # The cost ledger itself has a conservative built-in fallback, but
            # accounting must never make a model call fail if an import or
            # pricing read is unavailable.
            cost = 0.0
            pricing_source = "unavailable"
        cost_source = "estimated_list_price"
        cost_estimated = True

    result: Dict[str, Any] = {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_cny": round(cost, 6),
        "cost_basis": cost_source,
        "cost_is_estimated": bool(cost_estimated),
        "input_source": input_source,
        "output_source": output_source,
    }
    # Preserve the old exact return shape for direct callers.  Production
    # orchestrators pass model_tier and receive the canonical receipt fields.
    if model_tier:
        result.update({
            "model_tier": str(model_tier),
            "model_name": resolved_model,
            "cost_provenance": (
                "provider_reported"
                if cost_source == "provider_reported"
                else "reported_estimate"
                if cost_source == "estimated_reported"
                else "configured_list_price_estimate"
                if cost_source == "estimated_list_price"
                else "unavailable"
            ),
            "pricing_source": pricing_source,
            "usage_receipt_id": hashlib.sha1(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16],
        })
    return result


def build_compact_batched_audit_payload(
    *,
    section: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    wave_index: int,
    max_candidates: Optional[int] = None,
    components: Iterable[Any] = (),
    covered_roles: Iterable[str] = (),
    retained_lanes: Iterable[str] = (),
    remaining_candidate_count: int = 0,
) -> Dict[str, Any]:
    """Build the delta-only payload used by the single section audit call.

    ``max_candidates`` is an explicit caller ceiling only.  The local
    controller owns batching, so an omitted value means "no truncation here";
    callers that need a fixed search-wave ceiling pass it explicitly.
    """

    identity = section.get("topic_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    rows: List[Dict[str, Any]] = []
    candidate_list = list(candidates)
    if max_candidates is not None and int(max_candidates) > 0:
        candidate_list = candidate_list[: int(max_candidates)]
    for candidate in candidate_list:
        if not isinstance(candidate, Mapping):
            continue
        rows.append({
            "candidate_id": scalar_value(candidate.get("candidate_id")),
            "material_identity": compact_text(
                candidate.get("material_identity")
                or candidate.get("doi")
                or candidate.get("semantic_scholar_id")
                or candidate.get("title"),
                160,
            ),
            "role": scalar_value(candidate.get("role")),
            "title": compact_text(candidate.get("title"), 180),
            "year": candidate.get("year"),
            "venue": compact_text(candidate.get("venue"), 80),
            "doi": compact_text(candidate.get("doi"), 100),
            "abstract": compact_text(candidate.get("abstract"), 420),
            "is_oa": bool_value(candidate.get("is_oa")),
            "s2_id": compact_text(
                candidate.get("semantic_scholar_id")
                or candidate.get("semantic_scholar_paper_id")
                or candidate.get("corpus_id"),
                100,
            ),
            "candidate_route": compact_text(
                candidate.get("materialization_route")
                or candidate.get("text_provenance"),
                100,
            ),
        })
    payload: Dict[str, Any] = {
        "schema_version": "phase2.coverage_batched_audit.v1",
        "section_id": compact_text(section.get("section_id"), 80),
        "wave_index": max(0, int(wave_index)),
        "scientific_object": compact_text(
            identity.get("scientific_object")
            or section.get("title")
            or section.get("scope_description"),
            180,
        ),
        "section_role": compact_text(
            section.get("section_role") or section.get("role") or "general",
            60,
        ),
        # These compact constraints are part of the audit contract.  The
        # candidate list alone is insufficient to judge direct section fit.
        "chapter_argument": compact_text(section.get("chapter_argument"), 360),
        "key_questions": [
            compact_text(item, 180)
            for item in (
                [section.get("key_questions")]
                if isinstance(section.get("key_questions"), str)
                else section.get("key_questions") or []
            )
            if compact_text(item, 180)
        ][:5],
        "scope_guardrails": [
            compact_text(item, 180)
            for item in (
                [section.get("scope_guardrails")]
                if isinstance(section.get("scope_guardrails"), str)
                else section.get("scope_guardrails") or []
            )
            if compact_text(item, 180)
        ][:6],
        "required_roles": [
            scalar_value(item)
            for item in (section.get("required_roles") or [])
            if scalar_value(item)
        ][:6],
        "target_components": [compact_text(item, 120) for item in components if compact_text(item, 120)][:8],
        "candidates": rows,
        "batch_candidate_count": len(rows),
        "remaining_candidate_count": max(0, int(remaining_candidate_count or 0)),
        "prior_covered_roles": sorted(
            str(item).strip().casefold()
            for item in covered_roles
            if str(item).strip()
        ),
        "prior_retained_lanes": sorted(
            str(item).strip().casefold()
            for item in retained_lanes
            if str(item).strip()
        ),
        "audit_protocol": (
            "return one JSON decision per candidate; apply the section constraints "
            "as binding; reject or make discovery_lead any candidate outside an "
            "explicit spectral, modality, or application boundary; do not request "
            "another candidate-by-candidate chat pass; when a boundary or chapter "
            "mismatch exists, report structured scope_violations or "
            "boundary_violations records with code, severity (hard|soft), and "
            "short evidence; hard records reject, soft records remain discovery_lead"
        ),
    }
    payload["estimated_input_tokens"] = estimate_json_tokens(payload)
    payload["payload_fingerprint"] = stable_payload_fingerprint(payload)
    return payload


def bounded_audit_output_tokens(
    candidate_count: int,
    *,
    base_tokens: int = 600,
    per_candidate_tokens: int = 260,
    hard_cap_tokens: int = 12_000,
) -> int:
    """Reserve output tokens for one compact batched candidate judgement.

    The reserve is deliberately smaller than the old per-candidate 900-token
    allowance because the local envelope now owns schema and optional fields;
    the model only supplies decisions, reasons, and scores.  The reserve is
    still bounded by a hard cap so a large batch cannot claim the whole
    per-call context window.
    """

    count = max(0, int(candidate_count or 0))
    allowance = int(base_tokens) + count * max(0, int(per_candidate_tokens))
    return min(max(1, int(hard_cap_tokens)), allowance)


def plan_local_audit_batch(
    *,
    section: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    wave_index: int,
    per_call_budget_tokens: int,
    output_base_tokens: int = 600,
    output_tokens_per_candidate: int = 260,
    output_hard_cap_tokens: int = 12_000,
    max_candidates: int = 40,
    components: Iterable[Any] = (),
    covered_roles: Iterable[str] = (),
    retained_lanes: Iterable[str] = (),
    remaining_candidate_count: int = 0,
) -> Dict[str, Any]:
    """Build the largest ranked prefix that fits one local audit call.

    Ranking is done by the controller before this helper.  The helper only
    sizes the prompt from token estimates so no single local audit call can
    exceed the per-call context budget, while still allowing batches far
    larger than the historical fixed six-candidate ceiling.
    """

    selected: List[Mapping[str, Any]] = []
    payload: Dict[str, Any] = {}
    allowance = 0
    for candidate in candidates[: max(1, int(max_candidates))]:
        if not isinstance(candidate, Mapping):
            continue
        trial = [*selected, candidate]
        trial_payload = build_compact_batched_audit_payload(
            section=section,
            candidates=trial,
            wave_index=wave_index,
            max_candidates=None,
            components=components,
            covered_roles=covered_roles,
            retained_lanes=retained_lanes,
            remaining_candidate_count=remaining_candidate_count,
        )
        trial_allowance = bounded_audit_output_tokens(
            len(trial),
            base_tokens=output_base_tokens,
            per_candidate_tokens=output_tokens_per_candidate,
            hard_cap_tokens=output_hard_cap_tokens,
        )
        predicted = estimate_json_tokens(trial_payload)
        if predicted + trial_allowance > max(1, int(per_call_budget_tokens)):
            break
        selected = trial
        payload = trial_payload
        allowance = trial_allowance
    return {
        "batch": selected,
        "payload": payload,
        "predicted_input_tokens": estimate_json_tokens(payload) if selected else 0,
        "output_allowance_tokens": allowance,
        "batch_candidate_count": len(selected),
    }


def normalize_local_audit_records(
    parsed: Any,
    batch: Sequence[Mapping[str, Any]],
    *,
    default_scope_fit: str = "contextual",
    default_decision: str = "deferred",
    default_reason: str = "deferred_pending_local_reaudit",
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Repair model output for one local batch without promoting trust.

    The local code owns the envelope: unknown IDs are dropped, missing rows
    are filled with safe deferred defaults, and an approval without a usable
    scope or reason is downgraded rather than guessed.  Only structurally
    unusable responses return an error so the controller can retry.
    """

    errors: List[str] = []
    if isinstance(parsed, dict):
        parsed_rows = parsed.get("candidates", parsed)
    else:
        parsed_rows = parsed
    if not isinstance(parsed_rows, list):
        return [], ["provider_response_malformed: candidate audit response must contain a list"]
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    for record in parsed_rows:
        if not isinstance(record, dict):
            errors.append("provider_response_malformed: non-object record")
            continue
        candidate_id = str(record.get("candidate_id") or "").strip()
        if not candidate_id:
            errors.append("provider_response_malformed: record missing candidate_id")
            continue
        by_id.setdefault(candidate_id, []).append(dict(record))
    batch_ids = [str(row.get("candidate_id") or "") for row in batch]
    batch_id_set = set(batch_ids)
    unknown = sorted(set(by_id) - batch_id_set)
    if unknown:
        errors.append(
            "provider_response_unknown_ids_dropped:"
            + ",".join(str(item) for item in unknown[:5])
        )
    records: List[Dict[str, Any]] = []
    for row in batch:
        candidate_id = str(row.get("candidate_id") or "")
        raw_list = by_id.get(candidate_id)
        raw = dict(raw_list[0]) if raw_list else {}
        scope_fit = str(raw.get("scope_fit") or "").strip().casefold()
        decision = str(raw.get("decision") or "").strip().casefold()
        reason = str(raw.get("audit_reason") or "").strip()
        if scope_fit not in {"direct", "adjacent", "contextual", "out_of_scope"}:
            scope_fit = str(default_scope_fit).strip().casefold()
        if decision not in {"approved", "rejected", "deferred"}:
            decision = str(default_decision).strip().casefold()
        if decision == "approved":
            if scope_fit not in {"direct", "adjacent", "contextual"}:
                decision = "rejected"
                scope_fit = "out_of_scope"
                reason = (
                    reason
                    or "approved_out_of_usable_scope_clamped_to_rejected"
                )[:500]
            if not reason:
                decision = "deferred"
                reason = "deferred_pending_local_reaudit_missing_reason"
        not_usable = raw.get("not_usable_for")
        records.append({
            "candidate_id": row.get("candidate_id"),
            "scope_fit": scope_fit,
            "decision": decision,
            "audit_reason": reason or str(default_reason),
            "not_usable_for": (
                list(not_usable)
                if isinstance(not_usable, list)
                else []
            ),
            "role_fit": raw.get("role_fit"),
            "semantic_score": raw.get(
                "semantic_score",
                raw.get("relevance_score"),
            ),
            "candidate_decision": raw.get("candidate_decision"),
        })
    return records, errors


@dataclass(frozen=True)
class LocalAuditStopDecision:
    stop: bool
    reason: str
    examined_count: int
    remaining_count: int
    remaining_max_semantic_score: float


def _candidate_semantic_score(candidate: Mapping[str, Any]) -> float:
    for key in ("semantic_score", "relevance_score"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, min(1.0, float(value)))
    return 0.0


def _candidate_has_semantic_score(candidate: Mapping[str, Any]) -> bool:
    for key in ("semantic_score", "relevance_score"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def evaluate_local_audit_stop(
    *,
    examined_count: int,
    soft_candidate_target: int,
    remaining_candidates: Sequence[Mapping[str, Any]],
    last_batch_new_lanes: int = 0,
    last_batch_new_roles: int = 0,
    last_batch_semantic_gain: float = 0.0,
    no_gain_batches: int = 0,
    min_semantic_score: float = 0.35,
    strong_semantic_score: float = 0.7,
) -> LocalAuditStopDecision:
    """Decide whether another local batch is still worth examining.

    Stopping is driven by marginal coverage gain, role coverage, semantic
    score, and token budgets -- never by a fixed tiny candidate count or
    topic-specific keywords.  The soft target only tightens the bar after
    broad visibility has been reached; it never pads a small pool.
    """

    remaining = [
        item for item in remaining_candidates if isinstance(item, Mapping)
    ]
    remaining_max = max(
        (_candidate_semantic_score(item) for item in remaining),
        default=0.0,
    )
    remaining_has_scores = any(
        _candidate_has_semantic_score(item) for item in remaining
    )
    remaining_count = len(remaining)
    if remaining_count == 0:
        return LocalAuditStopDecision(
            True, "local_pool_exhausted", examined_count, 0, 0.0
        )
    # Without any semantic-score signal, one all-rejected/no-gain batch is not
    # enough evidence to abandon the pool; require a second no-gain batch.
    min_no_gain_batches = 2 if not remaining_has_scores else 1
    if (
        int(no_gain_batches) >= min_no_gain_batches
        and int(last_batch_new_lanes) == 0
        and int(last_batch_new_roles) == 0
        and remaining_max < float(min_semantic_score)
    ):
        return LocalAuditStopDecision(
            True,
            "local_marginal_gain_exhausted",
            examined_count,
            remaining_count,
            remaining_max,
        )
    if (
        examined_count >= max(1, int(soft_candidate_target))
        and remaining_has_scores
        and remaining_max < float(strong_semantic_score)
    ):
        return LocalAuditStopDecision(
            True,
            "local_soft_visibility_target_reached",
            examined_count,
            remaining_count,
            remaining_max,
        )
    return LocalAuditStopDecision(
        False, "", examined_count, remaining_count, remaining_max
    )


def admit_batched_audit_call(
    *,
    wave_index: int,
    audit_calls_in_wave: int,
    predicted_input_tokens: int,
    output_reserve_tokens: int,
    cumulative_input_tokens: int,
    cumulative_budget_tokens: int,
    per_call_budget_tokens: int,
    audit_calls_total: int = 0,
    audit_call_budget: int = 1,
) -> AuditCallAdmission:
    """Admit exactly one compact batched audit in a bounded wave."""

    predicted = max(0, int(predicted_input_tokens))
    reserve = max(0, int(output_reserve_tokens))
    cumulative = max(0, int(cumulative_input_tokens))
    total = cumulative + predicted + reserve
    next_calls = max(0, int(audit_calls_total)) + 1
    if int(audit_calls_in_wave) >= 1:
        return AuditCallAdmission(
            False, "one_batched_audit_per_wave_exceeded", max(0, int(wave_index)),
            predicted, reserve, total, next_calls,
        )
    if next_calls > max(1, int(audit_call_budget)):
        return AuditCallAdmission(
            False, "section_audit_call_budget_exceeded", max(0, int(wave_index)),
            predicted, reserve, total, next_calls,
        )
    context = admit_context_call(
        predicted_input_tokens=predicted,
        output_reserve_tokens=reserve,
        cumulative_input_tokens=cumulative,
        cumulative_budget_tokens=cumulative_budget_tokens,
        per_call_budget_tokens=per_call_budget_tokens,
    )
    return AuditCallAdmission(
        context.admitted,
        context.reason,
        max(0, int(wave_index)),
        predicted,
        reserve,
        total,
        next_calls,
    )


def structured_snippet_route_decision(
    *,
    text: Any,
    scope_fit: Any,
    context_complete: Any,
    use_permission: Any,
    visual_required: bool = False,
    context_limitations: Iterable[Any] = (),
) -> Dict[str, Any]:
    """Classify S2 body text as peer evidence or a full-text escalation lead."""

    scope = scalar_value(scope_fit)
    permission = scalar_value(use_permission)
    limitations = [
        compact_text(item, 140)
        for item in context_limitations
        if compact_text(item, 140)
    ]
    adequate = bool(
        str(text or "").strip()
        and scope == "direct"
        and bool(context_complete)
        and permission == "factual_support"
        and not limitations
        and not visual_required
    )
    if adequate:
        return {
            "accepted_as_peer_text_evidence": True,
            "fulltext_escalation_required": False,
            "reason": "direct_context_complete_structured_snippet_with_factual_permission",
            "limitations": [],
        }
    reasons = []
    if scope != "direct":
        reasons.append("snippet_scope_is_not_direct")
    if not bool(context_complete):
        reasons.append("snippet_depth_inadequate")
    if permission != "factual_support":
        reasons.append("factual_permission_not_proven")
    if limitations:
        reasons.extend(limitations)
    if visual_required:
        reasons.append("visual_asset_required")
    return {
        "accepted_as_peer_text_evidence": False,
        "fulltext_escalation_required": bool(
            visual_required
            or not bool(context_complete)
            or bool(limitations)
            or permission != "factual_support"
        ),
        "reason": ";".join(dict.fromkeys(reasons)) or "structured_snippet_not_usable",
        "limitations": list(dict.fromkeys(reasons)),
    }


def build_adaptive_coverage_contract(*args: Any, **kwargs: Any) -> Any:
    """Lazy compatibility export for the shared adaptive quality contract."""

    from .review_quality_contract import build_adaptive_coverage_contract as _build

    return _build(*args, **kwargs)


def evaluate_adaptive_coverage(*args: Any, **kwargs: Any) -> Any:
    """Lazy compatibility export for the shared adaptive quality contract."""

    from .review_quality_contract import evaluate_adaptive_coverage as _evaluate

    return _evaluate(*args, **kwargs)


@dataclass(frozen=True)
class ContextAdmission:
    admitted: bool
    reason: str
    predicted_input_tokens: int
    output_reserve_tokens: int
    cumulative_after_reserve: int


def admit_context_call(
    *,
    predicted_input_tokens: int,
    output_reserve_tokens: int,
    cumulative_input_tokens: int,
    cumulative_budget_tokens: int,
    per_call_budget_tokens: int,
) -> ContextAdmission:
    """Preflight both per-call and cumulative context budgets."""

    predicted = max(0, int(predicted_input_tokens))
    reserve = max(0, int(output_reserve_tokens))
    cumulative = max(0, int(cumulative_input_tokens))
    total = cumulative + predicted + reserve
    if predicted + reserve > max(1, int(per_call_budget_tokens)):
        return ContextAdmission(
            False,
            "per_call_context_budget_exceeded",
            predicted,
            reserve,
            total,
        )
    if total > max(1, int(cumulative_budget_tokens)):
        return ContextAdmission(
            False,
            "cumulative_context_budget_exceeded",
            predicted,
            reserve,
            total,
        )
    return ContextAdmission(True, "admitted", predicted, reserve, total)


def stable_payload_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "COVERAGE_ROLES",
    "COVERAGE_OUTCOMES",
    "ROLE_QUERY_TERMS",
    "CandidateDecisionContract",
    "CoverageReadiness",
    "AuditCallAdmission",
    "ContextAdmission",
    "LocalAuditStopDecision",
    "JsonDecodeResult",
    "admit_context_call",
    "admit_batched_audit_call",
    "bounded_audit_output_tokens",
    "build_compact_batched_audit_payload",
    "plan_local_audit_batch",
    "normalize_local_audit_records",
    "evaluate_local_audit_stop",
    "build_adaptive_coverage_contract",
    "build_uncovered_query_targets",
    "candidate_query_affinity",
    "candidate_has_legal_route",
    "candidate_is_materializable",
    "canonical_candidate_decision",
    "closed_scientific_components",
    "compact_text",
    "decode_json_payload",
    "derive_uncovered_roles",
    "evaluate_coverage_readiness",
    "evaluate_adaptive_coverage",
    "evaluate_candidate_topic_affinity",
    "extract_explicit_scope_regimes",
    "assess_candidate_regime_boundary",
    "assess_explicit_scope_boundary",
    "assess_retrieved_paper_scope_boundary",
    "estimate_json_tokens",
    "normalize_scope_violation_records",
    "normalize_qwen_usage",
    "normalize_pipeline_structure",
    "normalize_pipeline_text",
    "scope_violation_outcome",
    "scalar_value",
    "structured_snippet_route_decision",
    "normalize_scientific_query",
    "scientific_query_anchor",
    "stable_payload_fingerprint",
]
