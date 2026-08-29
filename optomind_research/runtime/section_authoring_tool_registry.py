"""Section Review Authoring tool registry — 12 deterministic FunctionTools.

All tools are pure-Python closures bound to a SectionAuthoringContext.
No LLM calls happen inside any tool. The agent handles all writing judgements.

Phase 3.1 hardening:
- Independent citation audit (agent cannot self-certify flags).
- Asset allowlist: every paper_id and chunk_id is validated against the ledger + KB.
- Audit-freshness sentinel: submit_section_draft/submit_revision invalidate the audit;
  validate_authoring_package refuses until run_citation_audit is called again.
- CJK detection: drafts with Chinese/Japanese/Korean characters fail validation.
- Word-count gate: drafts < 50 words fail validation.
- verified_local is computed programmatically; the agent cannot assert it.
- Visual query filtered by section's allowed paper_ids.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agentscope.tool import FunctionTool

from .article_completion_schemas import SectionHandoffCard
from .artifact_store import atomic_write_json, atomic_write_text
from .section_authoring_assets import (
    CanonicalAssetGraph,
    build_canonical_asset_graph,
    normalize_text,
)
from .section_authoring_schemas import (
    AuditFlag,
    AuthoringSourceEntry,
    CoverageFeedbackItem,
    CitationEntry,
    EvidenceItem,
    ParagraphPlan,
    RevisionEntry,
    SectionArgumentPlan,
    SectionAuthoringAudit,
    SectionAuthoringContext as _SchemaCtx,
    SectionAuthoringPackage,
    SectionCitationMap,
    SectionCoverageFeedback,
    SectionEvidencePacket,
    SectionRevisionHistory,
    SectionVisualPlacement,
    VisualPlacement,
)
from .tool_provider import SectionAuthoringContext, ToolProvider
from .topic_identity import assess_topic_alignment
from .synthesis_bundle import build_synthesis_bundle
from .evidence_portfolio_selector import select_evidence_portfolio
from optomind_research.review_writer import assess_citation_support
from optomind_research.scientific_text_english_normalizer import (
    repair_likely_scientific_mojibake,
)

logger = logging.getLogger(__name__)

_NOW = lambda: datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers shared across tools
# ---------------------------------------------------------------------------

def _write_artifact(work_dir: Path, filename: str, data: Any) -> None:
    if hasattr(data, "model_dump"):
        payload = data.model_dump()
    elif isinstance(data, dict):
        payload = data
    else:
        payload = {"value": data}
    atomic_write_json(work_dir / filename, payload)


def _read_artifact(work_dir: Path, filename: str) -> Optional[Dict]:
    p = work_dir / filename
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _append_artifact_history(
    work_dir: Path,
    history_filename: str,
    artifact_name: str,
    payload: Dict[str, Any],
    *,
    reason: str,
) -> int:
    """Archive a superseded planning artifact and return its history index.

    Argument planning and evidence selection are revision-capable scientific
    activities.  Treating their first accepted version as immutable prevents a
    later audit from repairing source breadth or claim coverage.  This compact
    append-only ledger preserves provenance without coupling the tools to a
    particular model or topic.
    """
    existing = _read_artifact(work_dir, history_filename) or {}
    raw_entries = existing.get("entries", [])
    entries = list(raw_entries) if isinstance(raw_entries, list) else []
    entries.append({
        "revision_index": len(entries),
        "artifact_name": artifact_name,
        "reason": _safe_str(reason, 300),
        "superseded_at": _NOW(),
        "payload": payload,
    })
    atomic_write_json(
        work_dir / history_filename,
        {
            "schema_version": "1.0",
            "artifact_name": artifact_name,
            "entries": entries,
            "total_revisions": len(entries),
            "updated_at": _NOW(),
        },
    )
    return len(entries)


def _safe_str(v: Any, limit: int = 2000) -> str:
    s = str(v) if not isinstance(v, str) else v
    return s[:limit]


_SPAN_MATCH_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00b5": "\u03bc",
    "\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3",
    "\u2074": "4", "\u2075": "5", "\u2076": "6", "\u2077": "7",
    "\u2078": "8", "\u2079": "9", "\u207a": "+", "\u207b": "-",
    "\u00ad": "",
})


def _normalize_span_for_match(value: Any) -> str:
    """Normalize harmless typography while preserving scientific symbols.

    Greek letters and other semantic symbols must not be stripped: alpha and
    beta, for example, identify different optical modes.  Only typographic
    variants such as dash/minus, micro signs, and superscript digits are
    canonicalized.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_SPAN_MATCH_TRANSLATION)
    text = re.sub(r"\s*-\s*", "-", text)
    return normalize_text(text).casefold()


def _auto_select_spans(
    chunk_normalized_text: str,
    claim_text: str,
    support_hint: str,
) -> List[str]:
    """Select 1–2 sentences from a canonical chunk that best support a claim.

    Scoring rules (higher = better match):
    - +3 per numeric token (digits, units) shared with claim_text + support_hint
    - +2 per Greek-letter word shared
    - +1 per other content word shared (≥4 chars, not a stop word)
    Sentences are split on '.!?' boundaries. The top-scoring sentence is always
    selected if its score ≥ 2; a second sentence is added if it independently
    scores ≥ 2 and contains a different numeric token.
    Returns [] when no sentence meets the threshold (caller rejects the item).
    """
    import re as _re
    _GREEK = frozenset("αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ")
    _STOP = frozenset({
        "the", "and", "for", "are", "was", "with", "has", "have", "been",
        "that", "this", "from", "can", "its", "into", "than", "when",
    })

    def _tokens(text: str):
        return _re.findall(r"[a-zA-ZΑ-ω]+|\d+(?:\.\d+)?", text)

    def _groups(text: str) -> tuple[set[str], set[str], set[str]]:
        content: set[str] = set()
        numbers: set[str] = set()
        greek: set[str] = set()
        for token in (item.lower() for item in _tokens(text)):
            if _re.fullmatch(r"\d+(?:\.\d+)?", token):
                numbers.add(token)
            elif any(char in _GREEK for char in token):
                greek.add(token)
            elif len(token) >= 4 and token not in _STOP:
                content.add(token)
        return content, numbers, greek

    def _split_sentences(text: str) -> list[str]:
        # Protect common scientific abbreviations and decimal points before
        # splitting. This avoids fragments such as "approx." and "i.e.".
        marker = "\ue000"
        protected = str(text or "")
        for abbreviation in (
            "e.g.", "i.e.", "approx.", "Fig.", "Figs.", "Eq.", "Eqs.",
            "Ref.", "Refs.", "et al.",
        ):
            protected = protected.replace(abbreviation, abbreviation.replace(".", marker))
        protected = _re.sub(r"(?<=\d)\.(?=\d)", marker, protected)
        return [
            item.replace(marker, ".").strip()
            for item in _re.split(r"(?<=[.!?])\s+", protected)
            if item.strip()
        ]

    query_raw = f"{claim_text} {support_hint}"
    query_content, query_numbers, query_greek = _groups(query_raw)

    raw_sentences = _split_sentences(chunk_normalized_text)
    if not raw_sentences:
        return []

    scored = []
    for sentence in raw_sentences:
        content, numbers, greek = _groups(sentence)
        matched_content = content & query_content
        matched_numbers = numbers & query_numbers
        matched_greek = greek & query_greek
        score = len(matched_content) + 3 * len(matched_numbers) + 2 * len(matched_greek)
        query_weight = max(1, len(query_content) + 3 * len(query_numbers) + 2 * len(query_greek))
        coverage = score / query_weight
        specificity = (
            len(matched_content) + len(matched_numbers) + len(matched_greek)
        ) / max(1, len(content) + len(numbers) + len(greek))
        scored.append((
            sentence, score, coverage, specificity,
            matched_content, matched_numbers, matched_greek,
        ))
    scored.sort(key=lambda item: (item[1], item[2], item[3], -len(item[0])), reverse=True)

    selected: list[str] = []
    seen_content: set[str] = set()
    seen_numbers: set[str] = set()
    seen_greek: set[str] = set()
    for sentence, score, _, _, content, numbers, greek in scored:
        if score < 2 or (not numbers and not greek and len(content) < 2):
            break
        if not selected:
            selected.append(sentence)
            seen_content.update(content)
            seen_numbers.update(numbers)
            seen_greek.update(greek)
        elif (
            numbers - seen_numbers
            or greek - seen_greek
            or len(content - seen_content) >= 2
        ):
            selected.append(sentence)
            seen_content.update(content)
            seen_numbers.update(numbers)
            seen_greek.update(greek)
            break
        if len(selected) >= 2:
            break

    return selected


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", str(text or "")))


def _paragraph_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()])


_STRONG_CAUSAL_RE = re.compile(
    r"\b(?:causes?|caused|drives?|driven by|results? in|arises? from|"
    r"because of|leads? to|proves?|demonstrates?|increases?|decreases?)\b",
    re.I,
)


_ABSTRACT_CLAIM_ALLOWED_KINDS = frozenset({
    "paper_reported_claim",
    "background",
    "trend",
    "candidate_lead",
    "author_synthesis",
})
_ABSTRACT_ALLOWED_KINDS = frozenset({
    "background",
    "trend",
    "candidate_lead",
    "author_synthesis",
})
_MECHANISM_CLAIM_RE = re.compile(
    r"\b(?:mechanis(?:m|ms)|causal(?:ity|ly)?|physical process|"
    r"underlying process|interaction(?:s)?|pathway(?:s)?)\b",
    re.I,
)
_METHOD_CLAIM_RE = re.compile(
    r"\b(?:method(?:s|ology)?|technique(?:s)?|fabrication|procedure(?:s)?|"
    r"characteri[sz]ation|experimental setup)\b",
    re.I,
)
_PAPER_ATTRIBUTION_RE = re.compile(
    r"\b(?:the (?:paper|study|authors?) (?:reports?|reported|finds?|found|"
    r"shows?|showed|suggests?|suggested|attributes?|attributed|proposes?|"
    r"proposed|observes?|observed|demonstrates?|demonstrated)|"
    r"according to (?:the )?(?:paper|study|authors?)|"
    r"as reported (?:in|by)|the abstract (?:reports?|states?|suggests?))\b",
    re.I,
)
_ABSTRACT_GROUNDING_STOPWORDS = frozenset({
    "abstract", "according", "author", "authors", "paper", "study",
    "report", "reports", "reported", "show", "shows", "showed",
    "find", "finds", "found", "suggest", "suggests", "suggested",
    "state", "states", "propose", "proposes", "proposed", "that",
    "the", "this", "these", "those", "with", "from", "into",
    "under", "through", "using", "their", "its", "and", "for",
})


def _normalise_claim_kinds(value: Any) -> set[str]:
    if isinstance(value, str):
        raw = [value]
    else:
        try:
            raw = list(value or [])
        except TypeError:
            raw = []
    return {
        str(item).strip().casefold().replace("-", "_")
        for item in raw
        if str(item).strip()
    }


def _claim_kind_aliases(kind: str) -> set[str]:
    aliases = {
        "causality": {"causality", "mechanism"},
        "mechanism": {"mechanism", "causality"},
    }
    return aliases.get(kind, {kind})


def _grounding_token(token: str) -> str:
    value = str(token or "").casefold()
    if len(value) > 5 and value.endswith("ing"):
        return value[:-3]
    if len(value) > 4 and value.endswith("ed"):
        return value[:-2]
    if len(value) > 4 and value.endswith("es"):
        return value[:-2]
    if len(value) > 3 and value.endswith("s"):
        return value[:-1]
    return value


def _abstract_reported_claim_is_grounded(claim_text: str, asset_text: str) -> bool:
    """Accept a qualified author report only when its content is in the abstract."""

    if not _PAPER_ATTRIBUTION_RE.search(claim_text) or not str(asset_text or "").strip():
        return False
    claim_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", claim_text))
    asset_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", asset_text))
    if not claim_numbers.issubset(asset_numbers):
        return False
    claim_tokens = {
        _grounding_token(token)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", claim_text)
        if token.casefold() not in _ABSTRACT_GROUNDING_STOPWORDS
    }
    asset_tokens = {
        _grounding_token(token)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", asset_text)
    }
    if not claim_tokens:
        return False
    supported = claim_tokens & asset_tokens
    return len(supported) >= 2 and len(supported) / len(claim_tokens) >= 0.85


def _claim_kind_guard_error(
    *,
    content_depth: str,
    allowed_claim_kinds: Any,
    writing_permission: str,
    claim_text: str,
    asset_id: str,
    asset_text: str = "",
) -> str | None:
    """Reject claims that exceed a chunk's explicit claim-kind ceiling."""

    depth = str(content_depth or "").strip().casefold().replace("-", "_")
    allowed = _normalise_claim_kinds(allowed_claim_kinds)
    if depth == "abstract_claim":
        invalid = sorted(allowed - _ABSTRACT_CLAIM_ALLOWED_KINDS)
        if invalid:
            return (
                f"{asset_id} abstract_claim declares unsupported claim kind(s) "
                f"{invalid}; allowed={sorted(_ABSTRACT_CLAIM_ALLOWED_KINDS)}."
            )
    elif depth in {"abstract", "abstract_only", "tldr"}:
        invalid = sorted(allowed - _ABSTRACT_ALLOWED_KINDS)
        if invalid:
            return (
                f"{asset_id} abstract content declares unsupported claim kind(s) "
                f"{invalid}; allowed={sorted(_ABSTRACT_ALLOWED_KINDS)}."
            )

    detected: List[str] = []
    if _MEASUREMENT_PATTERN.search(claim_text):
        detected.append("measurement")
    if _STRONG_CAUSAL_RE.search(claim_text):
        detected.append("causality")
    if _MECHANISM_CLAIM_RE.search(claim_text):
        detected.append("mechanism")
    if _METHOD_CLAIM_RE.search(claim_text):
        detected.append("method")

    if depth in {"abstract", "abstract_only", "tldr", "abstract_claim"}:
        if writing_permission == "factual_assertion":
            return (
                f"{asset_id} {depth} material cannot support an unqualified "
                "factual assertion."
            )
        if depth == "abstract_claim" and detected:
            reported_and_grounded = (
                writing_permission == "hedged_factual_assertion"
                and "paper_reported_claim" in allowed
                and _abstract_reported_claim_is_grounded(claim_text, asset_text)
            )
            if not reported_and_grounded:
                return (
                    f"{asset_id} abstract_claim cannot support inferred or ungrounded "
                    f"claim kind(s) {sorted(set(detected))}; a high-risk abstract claim "
                    "must be qualified, explicitly attributed to the paper, and grounded "
                    "in the materialized abstract text."
                )
            return None
        if detected and allowed:
            disallowed = [
                kind for kind in dict.fromkeys(detected)
                if not (_claim_kind_aliases(kind) & allowed)
            ]
            if disallowed:
                return (
                    f"{asset_id} claim kind(s) {sorted(set(disallowed))} exceed "
                    f"allowed_claim_kinds={sorted(allowed)}."
                )

    if allowed and detected:
        disallowed = [
            kind for kind in dict.fromkeys(detected)
            if not (_claim_kind_aliases(kind) & allowed)
        ]
        if disallowed:
            return (
                f"{asset_id} claim kind(s) {sorted(set(disallowed))} exceed "
                f"allowed_claim_kinds={sorted(allowed)}."
            )
    return None


def _permission_guard_error(
    *,
    use_permission: str,
    writing_permission: str,
    claim_text: str,
    asset_id: str,
    content_depth: str = "",
    allowed_claim_kinds: Any = (),
    asset_text: str = "",
) -> str | None:
    """Enforce content permission independently of model-supplied labels."""

    permission = str(use_permission or "discovery_only").casefold()
    writing = str(writing_permission or "factual_assertion").casefold()
    claim_kind_error = _claim_kind_guard_error(
        content_depth=content_depth,
        allowed_claim_kinds=allowed_claim_kinds,
        writing_permission=writing,
        claim_text=claim_text,
        asset_id=asset_id,
        asset_text=asset_text,
    )
    if claim_kind_error:
        return claim_kind_error
    has_measurement = bool(_MEASUREMENT_PATTERN.search(claim_text))
    has_strong_causal = bool(_STRONG_CAUSAL_RE.search(claim_text))
    factual_writing = writing in {"factual_assertion", "hedged_factual_assertion"}
    if permission == "discovery_only":
        return f"{asset_id} is discovery_only and cannot enter the evidence packet or factual plan."
    if permission == "background_and_candidate_only" and factual_writing and (
        has_measurement or has_strong_causal or writing == "factual_assertion"
    ):
        return (
            f"{asset_id} has background_and_candidate_only permission; it cannot support "
            "a precise number, strong causal statement, or single-paper factual assertion."
        )
    if permission == "contextual_or_qualified_support" and writing == "factual_assertion":
        return (
            f"{asset_id} has contextual_or_qualified_support permission; use a qualified "
            "or synthesis statement instead of an unqualified factual assertion."
        )
    return None


def _has_verified_permission_provenance(chunk: Any) -> bool:
    """Return whether a canonical chunk carries an explicit trust contract.

    ``r3_2`` is one valid migration marker, not the permission check itself.
    S2/abstract routes and any explicit depth, permission, or claim-kind
    contract must be checked even when that marker is absent.  A pre-contract
    fulltext fixture with no route or permission data remains compatible with
    the historical unit tests.
    """

    provenance = getattr(chunk, "route_provenance", {}) or {}
    depth = str(getattr(chunk, "content_depth", "") or "").strip().casefold()
    permission = str(getattr(chunk, "use_permission", "") or "").strip().casefold()
    allowed = _normalise_claim_kinds(getattr(chunk, "allowed_claim_kinds", ()))
    discovery = str(getattr(chunk, "discovery_route", "") or "").strip().casefold()
    materialization = str(
        getattr(chunk, "materialization_route", "") or ""
    ).strip().casefold()
    source_kind = str(getattr(chunk, "source_kind", "") or "").strip().casefold()
    route_text = " ".join([
        discovery,
        materialization,
        " ".join(str(value).casefold() for value in provenance.values()),
    ])

    legacy_fulltext = (
        not provenance
        and discovery in {"", "unknown", "legacy_unresolved"}
        and materialization in {"", "unknown", "not_materialized"}
        and (
            depth in {"fulltext", "structured_snippet"}
            or (
                depth in {"", "metadata"}
                and str(getattr(chunk, "evidence_level", "") or "").casefold()
                in {"fulltext", "structured_snippet"}
            )
        )
        and source_kind in {"", "fulltext", "structured_snippet"}
        and permission in {"", "discovery_only"}
    )
    if legacy_fulltext:
        return False

    return bool(
        str(provenance.get("migration") or "").casefold() == "r3_2"
        or depth in {
            "abstract_claim", "abstract", "abstract_only", "tldr",
        }
        or permission
        or allowed
        or any(
            marker in route_text
            for marker in ("s2", "semantic_scholar", "structured_snippet", "abstract_claim")
        )
        or discovery not in {"", "unknown", "legacy_unresolved"}
        or materialization not in {"", "unknown", "not_materialized"}
    )


def _legacy_fulltext_fixture(chunk: Any) -> bool:
    """Return whether the old no-contract fulltext compatibility is safe."""

    return (
        not _has_verified_permission_provenance(chunk)
        and (
            str(getattr(chunk, "content_depth", "") or "").casefold()
            in {"fulltext", "structured_snippet"}
            or (
                str(getattr(chunk, "content_depth", "") or "").casefold()
                in {"", "metadata"}
                and str(getattr(chunk, "evidence_level", "") or "").casefold()
                in {"fulltext", "structured_snippet"}
                and str(getattr(chunk, "source_kind", "") or "").casefold()
                in {"", "fulltext", "structured_snippet"}
            )
        )
        and bool(getattr(chunk, "normalized_text", ""))
        and str(getattr(chunk, "use_permission", "") or "discovery_only").casefold()
        == "discovery_only"
    )


def _resolve_all_kbs(ctx: SectionAuthoringContext) -> List[Path]:
    """Return all non-None existing KB paths (both main and staging)."""
    result = []
    for p in (
        ctx.kb_sqlite,
        ctx.temp_kb_sqlite,
        *(ctx.additional_kb_sqlite_paths or []),
    ):
        if p is not None and p.exists() and p not in result:
            result.append(p)
    return result


def _build_asset_graph(ctx: SectionAuthoringContext) -> CanonicalAssetGraph:
    """Rebuild the Phase-2 trust graph from artifacts and both KBs."""
    return build_canonical_asset_graph(
        material_package_path=ctx.material_package_path,
        source_ledger_path=ctx.source_ledger_path,
        work_dir=ctx.work_dir,
        kb_paths=_resolve_all_kbs(ctx),
        overlay_path=ctx.section_overlay_path,
    )


# ---------------------------------------------------------------------------
# Phase 3.1 — Independent audit helpers
# ---------------------------------------------------------------------------

_PORTFOLIO_STOPWORDS = frozenset({
    "about", "after", "also", "among", "and", "are", "because", "been",
    "before", "between", "both", "but", "can", "chapter", "claim", "could",
    "does", "each", "for", "from", "have", "into", "its", "more", "most",
    "must", "not", "only", "other", "our", "paper", "review", "section",
    "should", "show", "such", "than", "that", "the", "their", "these",
    "this", "through", "under", "using", "was", "were", "what", "when",
    "where", "which", "while", "with", "within", "would",
})


def _portfolio_terms(value: Any) -> set[str]:
    """Return stable scientific retrieval terms for a section or chunk."""
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,23}", str(value or "").lower())
        if token not in _PORTFOLIO_STOPWORDS
        and not token.isdigit()
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def _portfolio_argument_job(role: str) -> str:
    """Translate heterogeneous Phase-2 role names into authoring jobs."""
    value = str(role or "").lower()
    if any(token in value for token in ("foundation", "landmark", "history")):
        return "establish the field baseline or historical turning point"
    if any(token in value for token in ("mechanism", "physics", "principle")):
        return "explain a physical mechanism or causal boundary"
    if any(token in value for token in ("method", "design", "fabrication")):
        return "compare a method, design route, or implementation trade-off"
    if any(token in value for token in ("frontier", "recent", "emerging")):
        return "identify a recent advance and its unresolved limitation"
    if any(token in value for token in ("controvers", "debate", "limit", "gap")):
        return "bound a disagreement, limitation, or evidence gap"
    if any(token in value for token in ("application", "deployment", "engineering")):
        return "connect the argument to an application or engineering constraint"
    if any(token in value for token in ("review", "perspective", "roadmap")):
        return "provide field-level synthesis or taxonomy"
    return "supply an independent scientific perspective"


def _chunk_information_quality(text: Any, source_kind: Any = "") -> tuple[float, List[str]]:
    """Score whether a chunk is useful prose rather than document debris.

    This score is deliberately topic-independent.  It suppresses reference
    lists, author/affiliation blocks, and low-information table fragments while
    rewarding passages that contain scientific reasoning, methods, results,
    limitations, or conclusions.  It never changes source admissibility; it
    only chooses a better representative chunk from each already-audited paper.
    """

    normalized = normalize_text(text)
    lower = normalized.lower()
    reasons: List[str] = []
    score = 0.0
    if len(normalized) < 180:
        score -= 25.0
        reasons.append("very_short")
    elif len(normalized) >= 500:
        score += 6.0

    doi_hits = len(re.findall(r"\b10\.\d{4,9}/\S+", lower))
    citation_hits = len(re.findall(r"\bet al\.|\[\d+(?:\s*[-,]\s*\d+)*\]", lower))
    year_hits = len(re.findall(r"\b(?:19|20)\d{2}\b", lower))
    reference_like = (
        lower.startswith(("references ", "bibliography "))
        or str(source_kind or "").lower() in {"references", "bibliography"}
        or doi_hits >= 3
        or (citation_hits >= 5 and year_hits >= 5)
    )
    if reference_like:
        score -= 90.0
        reasons.append("reference_list_like")

    metadata_hits = sum(
        token in lower
        for token in (
            "correspondence:", "corresponding author", "affiliation",
            "received:", "accepted:", "contributed equally", "author contributions",
        )
    )
    email_hits = len(re.findall(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", lower))
    if metadata_hits or email_hits >= 2:
        score -= 35.0
        reasons.append("front_matter_like")

    scientific_signals = (
        "we demonstrate", "we report", "we show", "we find", "results show",
        "measured", "measurement", "experiment", "fabricat", "method",
        "mechanism", "because", "therefore", "limitation", "challenge",
        "conclusion", "future work", "performance", "trade-off", "tradeoff",
        "sensitivity", "selectivity", "quality factor", "resonance",
    )
    signal_count = sum(token in lower for token in scientific_signals)
    if signal_count:
        score += min(30.0, signal_count * 5.0)
        reasons.append("scientific_reasoning")

    alpha_words = re.findall(r"\b[a-zA-Z][a-zA-Z'-]{2,}\b", normalized)
    numeric_tokens = re.findall(r"\b\d+(?:\.\d+)?\b", normalized)
    if alpha_words and len(numeric_tokens) > max(20, len(alpha_words) // 2):
        score -= 18.0
        reasons.append("table_fragment_like")
    if len(re.findall(r"[.!?](?:\s|$)", normalized)) >= 2:
        score += 5.0
        reasons.append("multi_sentence_prose")
    return score, reasons


def _transfer_boundary(scope_fit: str) -> tuple[bool, str]:
    """Return the explicit writing boundary for non-direct evidence."""

    scope = str(scope_fit or "unreviewed").lower()
    if scope == "adjacent":
        return True, (
            "Use only as explicitly labelled cross-domain transfer evidence. "
            "State which principle is transferred, keep the claim hedged, and "
            "do not transplant quantitative performance or deployment validation."
        )
    if scope == "contextual":
        return True, (
            "Use only for context or interpretive synthesis, not as direct factual "
            "support for the section topic or its quantitative performance."
        )
    if scope == "unreviewed":
        return True, (
            "Treat as orientation only until its scope is reviewed; avoid load-bearing "
            "or quantitative claims."
        )
    return False, ""


def _claim_specific_authoring_anchors(
    claims: Iterable[Dict[str, Any]],
    graph: CanonicalAssetGraph,
    *,
    limit: int = 12,
) -> List[str]:
    """Select the best canonical chunks for each claim before generic ranking.

    A paper-diverse portfolio is useful for synthesis, but it can accidentally
    omit the one paragraph that contains a load-bearing number or mechanism.
    R4 must therefore carry at least one locally relevant anchor per claim and,
    when the verifier supplied component-level mappings, one anchor per
    distinct component while the bounded batch still has room.
    """

    selected: List[str] = []
    max_items = max(0, int(limit))
    for raw_claim in claims:
        if len(selected) >= max_items or not isinstance(raw_claim, dict):
            break
        statement = str(
            raw_claim.get("effective_statement")
            or raw_claim.get("supported_rewrite")
            or raw_claim.get("authoring_statement")
            or raw_claim.get("statement")
            or ""
        )
        claim_terms = _portfolio_terms(statement)
        claim_numbers = {
            value
            for value in re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?", statement)
            if not re.fullmatch(r"(?:19|20)\d{2}", value)
        }
        raw_ids = [
            str(value)
            for value in (
                list(raw_claim.get("supporting_text_chunk_ids") or [])
                + list(raw_claim.get("supporting_chunk_ids") or [])
                + list(raw_claim.get("factual_support_chunk_ids") or [])
                + list(raw_claim.get("core_chunk_ids") or [])
            )
            if str(value) in graph.chunks
        ]
        candidate_ids = list(dict.fromkeys(raw_ids))
        if not candidate_ids:
            continue

        component_groups: List[List[str]] = []
        for component in raw_claim.get("evidence_component_map") or []:
            if not isinstance(component, dict):
                continue
            ids = [
                str(value)
                for value in component.get("chunk_ids") or []
                if str(value) in graph.chunks
            ]
            if ids:
                component_groups.append(ids)
        groups = component_groups or [candidate_ids]

        def score(chunk_id: str) -> tuple[float, str]:
            chunk = graph.chunks[chunk_id]
            text = str(chunk.normalized_text or "")
            text_terms = _portfolio_terms(text[:8000])
            text_numbers = set(
                re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?", text)
            )
            matched_numbers = len(claim_numbers & text_numbers)
            missing_numbers = len(claim_numbers - text_numbers)
            value = (
                5.0 * len(claim_terms & text_terms)
                + 35.0 * matched_numbers
                - 20.0 * missing_numbers
                + (4.0 if chunk_id in (raw_claim.get("core_chunk_ids") or []) else 0.0)
                + min(5.0, len(text) / 1200.0)
            )
            return value, chunk_id

        for group in groups:
            available = [chunk_id for chunk_id in group if chunk_id not in selected]
            if not available or len(selected) >= max_items:
                continue
            best = max(available, key=score)
            selected.append(best)

        # A quantitative claim must carry the passage containing its literal,
        # even when a different component passage ranked slightly higher.
        if claim_numbers and len(selected) < max_items:
            number_covering = [
                chunk_id
                for chunk_id in candidate_ids
                if chunk_id not in selected
                and claim_numbers.issubset(
                    set(re.findall(
                        r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?",
                        str(graph.chunks[chunk_id].normalized_text or ""),
                    ))
                )
            ]
            if number_covering:
                selected.append(max(number_covering, key=score))
    return selected[:max_items]


def _build_authoring_evidence_portfolio(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    authoring_context: Optional[Dict[str, Any]] = None,
    *,
    max_papers: int = 10,
    preview_chars: int = 480,
) -> Dict[str, Any]:
    """Build the authoring portfolio through the shared R3.3 selector."""
    authoring_core_limit = max(
        4,
        int(
            (ctx.section_data or {}).get(
                "authoring_core_chunk_limit", 12
            )
            or 12
        ),
    )
    ac = authoring_context or {}
    claims = ac.get("claims") or ctx.section_data.get("claims") or []
    relation_edges = ac.get("relation_edges") or ctx.section_data.get("relation_edges") or []
    has_verified_contract = any(
        _has_verified_permission_provenance(chunk)
        for chunk in graph.chunks.values()
    )
    records = []
    for chunk in graph.chunks.values():
        records.append(
            {
                "chunk_id": chunk.chunk_id,
                "paper_id": chunk.paper_id,
                "paper_title": chunk.paper_title,
                "paper_year": chunk.paper_year,
                "normalized_text": chunk.normalized_text,
                "scope_fit": chunk.scope_fit,
                # Pre-R3.2 unit/legacy callers had no route provenance at all.
                # Keep their explicit fulltext fixtures usable for compatibility;
                # migrated production assets always take the strict permission
                # path and discovery_only remains excluded by the selector.
                "use_permission": (
                    "factual_support"
                    if _legacy_fulltext_fixture(chunk)
                    else chunk.use_permission
                ),
                "content_depth": chunk.content_depth,
                "context_complete": chunk.context_complete,
                "evidence_level": chunk.evidence_level,
                "source_kind": chunk.source_kind,
                "literature_role": chunk.literature_role,
                "not_usable_for": list(chunk.not_usable_for),
                "route_provenance": dict(chunk.route_provenance),
            }
        )
    selection = select_evidence_portfolio(
        section=ctx.section_data,
        candidates=records,
        claims=[item for item in claims if isinstance(item, dict)],
        relation_edges=[item for item in relation_edges if isinstance(item, dict)],
        allowed_paper_ids=list(graph.papers),
        allowed_chunk_ids=list(graph.chunks),
        max_core_chunks=min(
            authoring_core_limit, max(1, int(max_papers)) * 2
        ),
        max_core_chunks_per_paper=2,
    )
    required, available = _synthesis_source_requirement(ctx, graph)
    portfolio_status = selection.status
    portfolio_readiness = selection.readiness_status
    portfolio_reasons = list(selection.reasons)
    # Pre-R3.2 fixtures did not carry explicit role targets.  Preserve their
    # historical authoring behavior while keeping the shared selector strict
    # for migrated production assets.
    if (
        not has_verified_contract
        and selection.material_status == "material_ready"
        and selection.real_claim_count
    ):
        portfolio_status = "ready"
        portfolio_readiness = "ready_for_authoring"
        portfolio_reasons = []
    papers: List[Dict[str, Any]] = []
    claim_anchor_ids = _claim_specific_authoring_anchors(
        [item for item in claims if isinstance(item, dict)],
        graph,
        limit=12,
    )
    recommended_ids = list(dict.fromkeys([
        *claim_anchor_ids,
        *selection.core_chunk_ids,
    ]))[:authoring_core_limit]
    for paper_id in selection.core_paper_ids:
        core_ids = selection.core_chunk_ids_by_paper.get(paper_id, [])
        if not core_ids:
            continue
        chunk = graph.chunks.get(core_ids[0])
        if chunk is None:
            continue
        paper = graph.papers.get(paper_id)
        text = repair_likely_scientific_mojibake(normalize_text(chunk.normalized_text))
        preview = text[:preview_chars]
        if len(text) > preview_chars:
            preview = preview.rsplit(" ", 1)[0].rstrip() + "..."
        roles = [
            item.strip()
            for item in str(chunk.literature_role or (paper.literature_role if paper else "")).split(",")
            if item.strip()
        ]
        transfer_required, transfer_note = _transfer_boundary(chunk.scope_fit)
        alternatives = [
            item for item in selection.candidate_chunk_ids_by_paper.get(paper_id, [])[:2]
            if item != chunk.chunk_id
        ]
        papers.append(
            {
                "paper_id": paper_id,
                "title": chunk.paper_title or (paper.title if paper else ""),
                "year": chunk.paper_year if chunk.paper_year is not None else (paper.year if paper else None),
                "literature_roles": roles,
                "scope_fit": chunk.scope_fit,
                "argument_job": _portfolio_argument_job(roles[0] if roles else ""),
                "recommended_chunk_id": chunk.chunk_id,
                "alternative_chunk_ids": alternatives,
                "chunk_preview": preview,
                "matched_section_terms": [],
                "ranking_score": 0.0,
                "information_quality_score": 0.0,
                "information_quality_flags": [],
                "transfer_boundary_required": transfer_required,
                "transfer_boundary_note": transfer_note,
                "not_usable_for": list(chunk.not_usable_for),
            }
        )
    return {
        "status": portfolio_status,
        "material_status": selection.material_status,
        "readiness_status": portfolio_readiness,
        "reasons": portfolio_reasons,
        "minimum_synthesis_sources": required,
        "available_synthesis_sources": available,
        "selected_paper_count": len(papers),
        "query_terms": selection.query_terms,
        "recommended_batch_chunk_ids": recommended_ids,
        "claim_anchor_chunk_ids": claim_anchor_ids,
        "papers": papers,
        "selector_version": selection.selector_version,
        "selection_diagnostics": selection.diagnostics,
        "candidate_chunk_ids": selection.candidate_chunk_ids,
        "candidate_paper_ids": selection.candidate_paper_ids,
        "instruction": (
            "Give the papers distinct argumentative jobs. Retrieve only the bounded core batch first; "
            "use the candidate pool by ID when the section has a documented gap."
        ),
    }
def _portfolio_repair_assets(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    authoring_context: Optional[Dict[str, Any]] = None,
    *,
    max_assets: int = 40,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return a paper-diverse repair list instead of a chunk-ID-sorted dump."""

    portfolio = _build_authoring_evidence_portfolio(
        ctx,
        graph,
        authoring_context,
    )
    assets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    papers = [
        item for item in portfolio.get("papers", []) if isinstance(item, dict)
    ]
    # First pass: one high-information representative from every paper.
    # Second pass: at most two alternatives per paper.  This preserves source
    # diversity even when one paper has hundreds of chunks.
    for key in ("recommended_chunk_id", "alternative_chunk_ids"):
        for paper in papers:
            values = paper.get(key, [])
            chunk_ids = values if isinstance(values, list) else [values]
            for raw_chunk_id in chunk_ids:
                chunk_id = str(raw_chunk_id or "")
                if (
                    not chunk_id
                    or chunk_id in seen
                    or chunk_id not in graph.chunks
                ):
                    continue
                record = graph.chunks[chunk_id]
                transfer_required, transfer_note = _transfer_boundary(
                    record.scope_fit
                )
                assets.append({
                    "chunk_id": record.chunk_id,
                    "paper_id": record.paper_id,
                    "scope_fit": record.scope_fit,
                    "literature_role": record.literature_role,
                    "not_usable_for": list(record.not_usable_for),
                    "transfer_boundary_required": transfer_required,
                    "transfer_boundary_note": transfer_note,
                })
                seen.add(chunk_id)
                if len(assets) >= max_assets:
                    return portfolio, assets
    return portfolio, assets


_AUDIT_STALE_MARKER = "_audit_stale"
_REVISION_CONTROL_FILENAME = "SECTION_REVISION_CONTROL.json"
_MAX_LOCAL_REVISION_ATTEMPTS = 2

# Patterns for measurement-like numbers that require a citation in the same sentence
_MEASUREMENT_PATTERN = re.compile(
    r'\b\d+(?:\.\d+)?(?:\s*[-–—]?\s*(?:nm|fs|ps|ns|nJ|uJ|µJ|μJ|mJ|eV|meV|keV|'
    r'cm(?:\^?-?\d+|⁻¹)?|GHz|THz|MHz|kHz|Hz|K|°C|mW|W|kW|MW|'
    r'dB(?:/(?:m|cm))?|mV|V|mA|A|%|×10))',
    re.UNICODE,
)
# The historical measurement expression above is intentionally retained for
# compatibility with existing assets.  This companion expression is more
# conservative about typography and catches the forms that most often matter
# in a review: values with units, percentages, scientific notation, and
# statistical thresholds.
_CLEAR_MEASUREMENT_PATTERN = re.compile(
    r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:%|"
    r"nm|um|µm|μm|mm|cm|m|fs|ps|ns|µs|μs|ms|s|"
    r"Hz|kHz|MHz|GHz|THz|eV|meV|keV|J|mJ|µJ|μJ|nJ|"
    r"W|mW|kW|V|mV|A|mA|K|°C|dB)\b|"
    r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:×|x)\s*10\s*\^?\s*[-+]?\d+\b",
    re.IGNORECASE,
)
_FORMULA_OR_SYMBOL_PATTERN = re.compile(
    r"(?:\$\$?.+?\$|\\(?:frac|sqrt|sum|prod|langle|rangle|hat|bar|tilde|"
    r"alpha|beta|gamma|delta|lambda|omega|sigma|partial)\b|"
    r"\b[A-Za-z][A-Za-z0-9]*\s*[\^_]\s*\{?[-+A-Za-z0-9.]+|"
    r"\b(?:n|p|r|R|CI|OR|HR)\s*[=<>]\s*[-+]?\d|"
    r"[A-Za-z]\s*=\s*[^=\s])",
    re.IGNORECASE,
)
_STRONG_CAUSAL_PATTERN = re.compile(
    r"\b(?:causes?|caused|drives?|driven by|results? in|leads? to|"
    r"proves?|demonstrates?|establishes?|"
    r"is responsible for|accounts for)\b",
    re.IGNORECASE,
)
_STRONG_COMPARISON_PATTERN = re.compile(
    r"\b(?:outperform(?:s|ed)?|higher than|lower than|better than|"
    r"worse than|compared (?:with|to)|exceed(?:s|ed)?|surpass(?:es|ed)?|"
    r"statistically significant|significantly higher|significantly lower|"
    r"correlat(?:es?|ion)|confidence interval|odds ratio|p\s*[<=>])\b",
    re.IGNORECASE,
)
_SOURCE_SPECIFIC_RESULT_PATTERN = re.compile(
    r"\b(?:we|the authors?|the study|the experiment|measurements?|"
    r"experiments?)\s+(?:report(?:s|ed)?|observe(?:s|d)?|measure(?:s|d)?|"
    r"finds?|found|achieve(?:s|d)?|show(?:s|ed)?)\b",
    re.IGNORECASE,
)
_STRONG_ASSERTION_PATTERN = re.compile(
    r"\b(?:does not|do not|did not|cannot|never|no evidence|fails? to|"
    r"cures?\s+(?:cancer|disease|all|every)|guarantees?\s+(?:that|the|"
    r"a|an|performance|success)|eliminates? all|always succeeds?)\b",
    re.IGNORECASE,
)
_CJK_PATTERN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')


def _detect_cjk(text: str) -> bool:
    return bool(_CJK_PATTERN.search(str(text or "")))


class _AssetAllowlist:
    """Compatibility view for the superseded legacy audit implementation."""

    def __init__(self, graph: CanonicalAssetGraph) -> None:
        self.paper_ids = frozenset(graph.papers)
        self.chunk_ids = frozenset(graph.chunks)
        self.visual_chunk_ids = frozenset(graph.visuals)


def _build_asset_allowlist(ctx: SectionAuthoringContext) -> _AssetAllowlist:
    return _AssetAllowlist(_build_asset_graph(ctx))


def _extract_ref_markers(text: str) -> List[str]:
    """Extract all [REF:xxx] paper_id values from draft text."""
    return re.findall(r'\[REF:([^\]]+)\]', str(text or ""))


def _find_uncited_measurements(draft_text: str) -> List[str]:
    """Find sentences with measurement-like numbers but no [REF:xxx] marker."""
    uncited = []
    sentences = _split_draft_sentences(draft_text)
    for sent in sentences:
        if (
            (_MEASUREMENT_PATTERN.search(sent) or _CLEAR_MEASUREMENT_PATTERN.search(sent))
            and not re.search(r"\[REF:", sent, re.I)
        ):
            uncited.append(sent.strip()[:150])
    return uncited


def _citation_risk_class(text: str) -> str:
    """Classify the scientific risk of a sentence without using an LLM.

    R4 only blocks claims whose failure would materially falsify a review:
    exact values, equations/notation, strong causal or comparative language,
    statistical claims, and source-specific results.  Ordinary background,
    relationship statements, and bounded synthesis remain auditable warnings.
    """
    claim = normalize_text(
        re.sub(r"\[REF:[^\]]+\]", " ", str(text or ""), flags=re.I)
    )
    if _CLEAR_MEASUREMENT_PATTERN.search(claim) or _MEASUREMENT_PATTERN.search(claim):
        return "exact_measurement"
    if _FORMULA_OR_SYMBOL_PATTERN.search(claim):
        return "formula_or_symbol"
    if _STRONG_CAUSAL_PATTERN.search(claim):
        return "strong_causal"
    if _STRONG_COMPARISON_PATTERN.search(claim):
        return "strong_comparison_or_statistical"
    if _SOURCE_SPECIFIC_RESULT_PATTERN.search(claim):
        return "source_specific_result"
    if _STRONG_ASSERTION_PATTERN.search(claim):
        return "strong_assertion"
    if re.search(r"\b(?:relationship|associated with|corresponds to|linked to|"
                  r"collectively|across the literature|in this context|"
                  r"suggests?|indicates?|is consistent with)\b", claim, re.I):
        return "relationship_or_synthesis"
    return "background_or_synthesis"


def _find_uncited_high_risk_claims(draft_text: str) -> List[tuple[str, str]]:
    """Find blocking technical sentences without duplicating measurement flags."""
    found: List[tuple[str, str]] = []
    sentences = _split_draft_sentences(draft_text)
    for sent in sentences:
        sentence = sent.strip()
        if not sentence or re.search(r"\[REF:", sentence, re.I):
            continue
        risk = _citation_risk_class(sentence)
        if risk in {
            "formula_or_symbol",
            "source_specific_result",
        }:
            found.append((sentence[:180], risk))
    return found


def _graph_ready_error(graph: CanonicalAssetGraph) -> Optional[str]:
    if graph.is_empty:
        return "Canonical Phase-2 asset graph is empty; submission rejected."
    return None


def _restriction_conflict(text: str, restrictions: List[str] | tuple[str, ...]) -> Optional[str]:
    """Return the canonical not_usable_for rule that conflicts with a claim."""
    # Reference IDs frequently contain DOI numbers.  They are provenance
    # syntax, not scientific measurements, and must never trigger a
    # quantitative restriction.
    claim = normalize_text(
        re.sub(r"\[REF:[^\]]+\]", " ", str(text or ""), flags=re.I)
    ).lower()
    for raw in restrictions:
        rule = normalize_text(raw).lower()
        if not rule:
            continue
        keywords = {
            token for token in re.findall(r"[a-z][a-z0-9-]{3,}", rule)
            if token not in {"this", "that", "with", "from", "paper", "source", "evidence", "claims"}
        }
        if keywords and len(keywords & set(re.findall(r"[a-z][a-z0-9-]{3,}", claim))) >= min(2, len(keywords)):
            return str(raw)
        if "quant" in rule or "exact" in rule or "measurement" in rule:
            if re.search(r"\b\d+(?:\.\d+)?\b", claim):
                return str(raw)
        if "causal" in rule or "causation" in rule or "mechanism" in rule:
            if re.search(r"\b(?:causes?|drives?|results? in|because|mechanism)\b", claim):
                return str(raw)
    return None


def _requires_strict_citation_entailment(text: str) -> bool:
    """Return whether a sentence needs direct local entailment to be publishable.

    Exact measurements, causal claims, explicit performance comparisons, and
    source-specific experimental results remain fail-closed.  Review-level
    background and bounded synthesis may use a canonically related source even
    when the local chunk is not a near-verbatim restatement; weak overlap is
    recorded for editorial inspection rather than blocking the whole article.
    """

    return _citation_risk_class(text) in {
        "exact_measurement",
        "formula_or_symbol",
        "strong_causal",
        "strong_comparison_or_statistical",
        "source_specific_result",
    }


def _citation_note_hard_failure(
    note: str,
    *,
    trusted_tokens: Optional[Set[str]] = None,
) -> bool:
    """Classify deterministic support-failure mechanisms, not domain words."""

    lower = str(note or "").lower()
    if "negates a predicate asserted" in lower:
        return True
    if "zero evidence overlap with the cited chunk text" not in lower:
        return False
    if trusted_tokens is None:
        return True
    clause_text = str(note or "").split(":", 1)[-1].strip()
    clause_tokens = _citation_match_tokens(clause_text)
    return not bool(clause_tokens & trusted_tokens)


def _audit_flag_dict(flag: Any) -> Dict[str, Any]:
    if isinstance(flag, dict):
        data = dict(flag)
    elif hasattr(flag, "model_dump"):
        try:
            data = dict(flag.model_dump())
        except Exception:
            data = {}
    else:
        data = {}
    if not data:
        data = {
            "flag_type": str(getattr(flag, "flag_type", "")),
            "sentence_index": int(getattr(flag, "sentence_index", -1) or -1),
            "severity": str(getattr(flag, "severity", "")),
            "reason": str(getattr(flag, "reason", "")),
            "risk_class": str(getattr(flag, "risk_class", "")),
        }
    # ``flag_type`` is the persisted Pydantic field; ``type`` is the stable
    # tool-result alias used by downstream workers and audit consumers.
    flag_type = str(data.get("flag_type") or data.get("type") or "")
    data.setdefault("flag_type", flag_type)
    data.setdefault("type", flag_type)
    return data


def _blocking_flag_signature(flags: Iterable[Any]) -> str:
    """Create a stable signature for the current technical blockers.

    The signature deliberately excludes the prose of a reason.  A model often
    changes wording while leaving the same unsupported equation or sentence in
    place; that must count as the same blocker for convergence purposes.
    """
    keys: List[str] = []
    for raw in flags:
        flag = _audit_flag_dict(raw)
        if str(flag.get("severity") or "") != "blocking" or flag.get("resolved"):
            continue
        flag_type = str(flag.get("flag_type") or flag.get("type") or "")
        sentence_index = str(flag.get("sentence_index", -1))
        risk = str(flag.get("risk_class") or "")
        reason = normalize_text(flag.get("reason") or "")
        # For section-level flags without an index, retain a short normalized
        # reason so two different missing-contract failures do not collapse.
        suffix = reason[:80] if sentence_index == "-1" else ""
        keys.append("|".join((flag_type, sentence_index, risk, suffix)))
    return ";".join(sorted(dict.fromkeys(keys)))


def _read_revision_control(ctx: SectionAuthoringContext) -> Dict[str, Any]:
    data = _read_artifact(ctx.work_dir, _REVISION_CONTROL_FILENAME) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", "r4.revision_control.v1")
    data.setdefault("section_id", ctx.section_id)
    data.setdefault("max_local_revision_attempts", _MAX_LOCAL_REVISION_ATTEMPTS)
    data.setdefault("revision_attempts", 0)
    data.setdefault("last_blocking_signature", "")
    data.setdefault("last_blocking_count", 0)
    data.setdefault("stop_revising", False)
    data.setdefault("history", [])
    return data


def _update_revision_control(
    ctx: SectionAuthoringContext,
    blocking_flags: Iterable[Any],
    warning_flags: Iterable[Any] = (),
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    """Update the bounded local revision controller after one audit."""
    control = _read_revision_control(ctx)
    blockers = [_audit_flag_dict(flag) for flag in blocking_flags]
    warnings = [_audit_flag_dict(flag) for flag in warning_flags]
    signature = _blocking_flag_signature(blockers)
    count = len(blockers)
    previous_signature = str(control.get("last_blocking_signature") or "")
    previous_count = int(control.get("last_blocking_count", 0) or 0)
    attempts = int(control.get("revision_attempts", 0) or 0)
    # A repeated audit of the initial draft is harmless; require at least one
    # actual revision attempt before treating a repeated signature as a failed
    # convergence step.  This avoids stopping a validator that is merely
    # re-reading the same durable draft.
    max_attempts = int(
        control.get("max_local_revision_attempts", _MAX_LOCAL_REVISION_ATTEMPTS)
        or _MAX_LOCAL_REVISION_ATTEMPTS
    )
    same_signature = bool(
        signature and signature == previous_signature and attempts >= 1
    )
    # Only declare no_improvement on the final allowed attempt so earlier
    # revision slots are not wasted when the blocker count merely stays flat.
    no_improvement = bool(
        blockers
        and previous_signature
        and not same_signature
        and attempts >= max(1, max_attempts - 1)
        and count >= previous_count
    )
    maxed = bool(blockers and attempts >= max_attempts)
    stop_revising = bool(blockers and (same_signature or no_improvement or maxed))
    if stop_revising:
        if same_signature:
            stop_reason = "repeated_blocking_signature"
        elif no_improvement:
            stop_reason = "blocking_count_not_improved"
        else:
            stop_reason = "max_local_revision_attempts_reached"
        recommended_action = "remove_or_qualify"
    else:
        stop_reason = ""
        recommended_action = "batch_all_blockers_once"
    history = list(control.get("history") or [])
    history.append({
        "revision_attempts": attempts,
        "blocking_count": count,
        "blocking_signature": signature,
        "same_signature": same_signature,
        "no_improvement": no_improvement,
        "stop_revising": stop_revising,
        "created_at": _NOW(),
    })
    control.update({
        "last_blocking_signature": signature,
        "last_blocking_count": count,
        "blocking_flags": blockers,
        "editorial_warnings": warnings[:12],
        "same_signature": same_signature,
        "no_improvement": no_improvement,
        "stop_revising": stop_revising,
        "stop_reason": stop_reason,
        "recommended_action": recommended_action,
        "history": history[-8:],
    })
    if persist:
        _write_artifact(ctx.work_dir, _REVISION_CONTROL_FILENAME, control)
    return control


def _has_durable_section_candidate(work_dir: Path) -> bool:
    """Return whether a useful section can be retained for human review."""
    draft_path = work_dir / "SECTION_DRAFT_EN.md"
    evidence_path = work_dir / "SECTION_EVIDENCE_PACKET.json"
    citation_path = work_dir / "SECTION_CITATION_MAP.json"
    if not all(path.exists() for path in (draft_path, evidence_path, citation_path)):
        _restore_last_valid_section_candidate(work_dir)
    if not all(path.exists() for path in (draft_path, evidence_path, citation_path)):
        return False
    try:
        draft = draft_path.read_text(encoding="utf-8").strip()
        if _word_count(draft) < 50:
            return False
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        citations = json.loads(citation_path.read_text(encoding="utf-8"))
        return isinstance(evidence, dict) and isinstance(citations, dict)
    except Exception:
        return False


def _write_awaiting_human_review_package(
    ctx: SectionAuthoringContext,
    *,
    reason: str,
    control: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist a durable candidate without mislabeling it as completed."""
    draft = (ctx.work_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8").strip()
    citation = _read_artifact(ctx.work_dir, "SECTION_CITATION_MAP.json") or {}
    audit = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_AUDIT.json") or {}
    artifacts = {
        name: name
        for name in (
            "SECTION_AUTHORING_CONTEXT.json",
            "SECTION_ARGUMENT_PLAN.json",
            "SECTION_EVIDENCE_PACKET.json",
            "SECTION_DRAFT_EN.md",
            "SECTION_CITATION_MAP.json",
            "SECTION_AUTHORING_AUDIT.json",
            "SECTION_REVISION_HISTORY.json",
            _REVISION_CONTROL_FILENAME,
        )
        if (ctx.work_dir / name).exists()
    }
    package = SectionAuthoringPackage(
        section_id=ctx.section_id,
        section_title=ctx.section_title,
        chapter_argument=ctx.chapter_argument,
        authoring_status="completed_with_limits",
        word_count=_word_count(draft),
        paragraph_count=_paragraph_count(draft),
        cited_sentences=int(citation.get("total_cited_sentences", 0) or 0),
        total_flags=int(audit.get("total_flags", 0) or 0),
        blocking_flags=int(audit.get("total_blocking_flags", 0) or 0),
        papers_cited=list(citation.get("papers_cited", [])),
        artifacts=artifacts,
        created_at=_NOW(),
    )
    payload = package.model_dump()
    payload["review_gate"] = {
        "status": "completed_with_limits",
        "reason": reason,
        "blocking_flags": int(audit.get("total_blocking_flags", 0) or 0),
        "convergence": dict(control or {}),
    }
    _write_artifact(ctx.work_dir, "SECTION_AUTHORING_PACKAGE.json", payload)
    _write_artifact(ctx.work_dir, "SECTION_CONVERGENCE_DECISION.json", payload["review_gate"])
    return (
        "VALIDATION_PASSED_WITH_LIMITS: Durable draft/evidence/citation "
        f"package retained after bounded revision attempts ({reason}). "
        "Automatic paraphrasing stopped; retained hard flags remain available "
        "for downstream review/repair."
    )


def _section_word_budget(ctx: SectionAuthoringContext) -> int:
    contract = dict(ctx.section_data.get("section_contract") or {})
    try:
        return max(
            0,
            int(
                contract.get("word_budget")
                or contract.get("target_word_budget")
                or ctx.section_data.get("estimated_word_budget")
                or 0
            ),
        )
    except (TypeError, ValueError):
        return 0


def _synthesis_source_requirement(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
) -> tuple[int, int]:
    """Return ``(required, available)`` distinct synthesis sources.

    The gate deliberately measures section-level reading breadth rather than
    citations per sentence.  A 900-word section normally needs four
    independently useful papers in its argument plan; longer sections scale up
    gradually to eight.  If the audited package contains fewer papers, the
    requirement fails soft at the number genuinely available.
    """

    owners_with_text = {
        chunk.paper_id
        for chunk in graph.chunks.values()
        if chunk.normalized_text
        and chunk.paper_id in graph.papers
        and graph.papers[chunk.paper_id].scope_fit != "out_of_scope"
    }
    available = len(owners_with_text)
    if available <= 0:
        return 0, 0
    # A narrow upstream package is a literature-coverage problem, not a reason
    # to make section authoring churn forever.  The Phase-2 portfolio auditor
    # owns that failure mode.  This authoring gate activates only when a
    # genuinely plural synthesis pool (four or more papers) exists.
    if available < 4:
        return 0, available
    word_budget = _section_word_budget(ctx) or 900
    desired = min(8, max(4, (word_budget + 249) // 250))
    return min(available, desired), available


def _synthesis_source_diversity_error(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    cited_paper_ids: Iterable[str],
) -> Optional[str]:
    """Validate review-level synthesis breadth in the finished prose.

    The argument-plan gate proves that the author *planned* to read broadly.
    This companion gate proves that the final section actually used that
    breadth.  It deliberately operates once per section, not once per
    sentence: ordinary background and author synthesis remain free of
    citation clutter, while a long review section cannot silently collapse
    onto one or two papers after planning five or six.
    """

    required, available = _synthesis_source_requirement(ctx, graph)
    if required <= 0:
        return None
    usable = {
        str(paper_id)
        for paper_id in cited_paper_ids
        if str(paper_id) in graph.papers
        and graph.papers[str(paper_id)].scope_fit != "out_of_scope"
    }
    if len(usable) >= required:
        return None
    return (
        "insufficient synthesis-source diversity in the finished section: the audited draft "
        f"uses {len(usable)} distinct usable paper(s), but this section requires "
        f"at least {required} from {available} available audited paper(s). "
        "Revise the chapter-level synthesis to use additional relevant papers "
        "already present in the argument plan. This is a section-level breadth "
        "requirement, not a requirement to cite every sentence."
    )


def _claim_judgment_map(ctx: SectionAuthoringContext) -> Dict[str, Dict[str, Any]]:
    """Return the R4 claim-strength ledger without trusting model output."""

    raw = ctx.section_data.get("judgment_ledger") or []
    if isinstance(raw, dict):
        raw = raw.get("claims") or raw.get("entries") or []
    result: Dict[str, Dict[str, Any]] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and str(item.get("claim_id") or "").strip():
            result[str(item["claim_id"])] = item
    return result


def _claim_strength_plan_error(
    claim_id: str,
    strength: str,
    permission: str,
    *,
    location: str,
) -> Optional[str]:
    """Enforce qualified language at the claim-to-paragraph boundary."""

    strength = str(strength or "qualified").casefold()
    permission = str(permission or "factual_assertion").casefold()
    if strength == "established":
        return None
    if strength == "qualified" and permission == "factual_assertion":
        return (
            f"{location} claim {claim_id} is qualified, but factual_assertion was requested; "
            "use hedged_factual_assertion or interpretive_synthesis and preserve the recorded boundary."
        )
    if strength == "open" and permission not in {
        "evidence_gap_only", "interpretive_synthesis", "common_background", "structural_transition",
    }:
        return (
            f"{location} claim {claim_id} is open/unresolved and cannot be written as "
            f"{permission}; expose it as an evidence gap or qualified synthesis."
        )
    if strength == "boundary" and permission in {"factual_assertion", "hedged_factual_assertion"}:
        return (
            f"{location} claim {claim_id} is a boundary or conflict and cannot be written "
            f"as {permission}; state the boundary as interpretation."
        )
    return None


_WRITING_PERMISSION_RANK = {
    "evidence_gap_only": 0,
    "structural_transition": 1,
    "common_background": 2,
    "interpretive_synthesis": 3,
    "hedged_factual_assertion": 4,
    "factual_assertion": 5,
}


_WRITING_PERMISSION_EXPLICIT_ALIASES = {
    "qualified_factual_assertion": "hedged_factual_assertion",
    "qualified_assertion": "hedged_factual_assertion",
    "background": "common_background",
    "transition": "structural_transition",
    "contextual_or_qualified_support": "hedged_factual_assertion",
    "background_and_candidate_only": "common_background",
    "factual_support": "factual_assertion",
    "can_quote_canonical_text_and_measurements": "factual_assertion",
}


def _normalize_writing_permission(value: Any) -> str:
    """Normalize only deterministic writing-permission aliases and suffixes.

    Canonical paragraph permissions stay authoritative.  Evidence-level labels
    are accepted only when their downstream meaning is unambiguous and
    conservative; arbitrary unknown strings are returned unchanged so the
    canonical validators can reject them rather than hide a fabrication.
    """

    raw = str(value or "").casefold().strip().replace("-", "_")
    if not raw:
        return raw
    if raw in _WRITING_PERMISSION_RANK:
        return raw
    alias = _WRITING_PERMISSION_EXPLICIT_ALIASES.get(raw)
    if alias:
        return alias
    for canonical in sorted(
        _WRITING_PERMISSION_RANK,
        key=len,
        reverse=True,
    ):
        prefix = f"{canonical}_"
        if not raw.startswith(prefix):
            continue
        suffix = raw[len(prefix):]
        if not suffix:
            continue
        if any(
            other != canonical and other in suffix
            for other in _WRITING_PERMISSION_RANK
        ):
            continue
        return canonical
    return raw


def _safe_permission_ceiling(
    strength: str,
    requested: str,
    *,
    has_evidence_chunks: bool,
) -> str:
    """Return a non-upgrading writing permission for one claim."""

    strength = str(strength or "qualified").casefold().strip()
    requested = str(requested or "factual_assertion").casefold().strip()
    if requested not in _WRITING_PERMISSION_RANK:
        return requested
    if strength == "established":
        return requested
    if strength == "qualified":
        ceiling = "hedged_factual_assertion"
    elif strength == "boundary":
        ceiling = "interpretive_synthesis"
    elif strength == "open":
        ceiling = "interpretive_synthesis" if has_evidence_chunks else "evidence_gap_only"
    else:
        ceiling = "hedged_factual_assertion"
    if _WRITING_PERMISSION_RANK[requested] <= _WRITING_PERMISSION_RANK[ceiling]:
        return requested
    return ceiling


def _normalize_argument_plan_permissions(
    ctx: SectionAuthoringContext,
    raw_paragraphs: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Downshift unsafe model permissions before validation.

    This is intentionally one-way.  The model can request a weaker mode, but
    it cannot promote an open or qualified claim beyond the ledger ceiling.
    Unknown claim IDs are left untouched so the canonical asset validator can
    reject fabricated references rather than hiding them.
    """

    judgment_map = _claim_judgment_map(ctx)
    normalized: List[Dict[str, Any]] = []
    corrections: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_paragraphs):
        paragraph = dict(raw) if isinstance(raw, dict) else raw
        if not isinstance(paragraph, dict):
            normalized.append(paragraph)
            continue
        requested = str(
            paragraph.get("writing_permission") or "factual_assertion"
        ).casefold().strip()
        claim_ids = [str(item) for item in paragraph.get("key_claims", [])]
        known = [judgment_map.get(item) for item in claim_ids]
        known = [item for item in known if isinstance(item, dict)]
        if not known or requested not in _WRITING_PERMISSION_RANK:
            normalized.append(paragraph)
            continue
        has_chunks = bool(paragraph.get("evidence_chunk_ids"))
        ceilings = [
            _safe_permission_ceiling(
                str(item.get("strength") or "qualified"),
                requested,
                has_evidence_chunks=has_chunks,
            )
            for item in known
        ]
        target = min(
            ceilings,
            key=lambda value: _WRITING_PERMISSION_RANK.get(value, 99),
        )
        if _WRITING_PERMISSION_RANK.get(target, 99) < _WRITING_PERMISSION_RANK.get(requested, 99):
            paragraph["writing_permission"] = target
            corrections.append({
                "paragraph_index": index,
                "claim_ids": claim_ids,
                "requested_permission": requested,
                "normalized_permission": target,
                "reason": (
                    "Deterministic claim-strength ceiling; permission was lowered "
                    "without adding or upgrading evidence."
                ),
            })
        normalized.append(paragraph)
    return normalized, corrections


def _normalize_evidence_packet_permissions(
    ctx: SectionAuthoringContext,
    raw_items: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply the same downward-only claim ceiling to evidence items.

    An evidence item is a support candidate, not permission to strengthen an
    unresolved claim.  In particular, an ``open`` claim with a real chunk may
    be retained as interpretive material, but it must not enter the packet as
    an unqualified factual assertion.  Unknown claim IDs remain untouched so
    the canonical provenance validator can reject them.
    """
    judgment_map = _claim_judgment_map(ctx)
    normalized: List[Dict[str, Any]] = []
    corrections: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        item = dict(raw) if isinstance(raw, dict) else raw
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        requested = str(
            item.get("writing_permission") or "factual_assertion"
        ).casefold().strip()
        claim_ids = [str(value) for value in item.get("claim_ids", [])]
        known = [judgment_map.get(value) for value in claim_ids]
        known = [value for value in known if isinstance(value, dict)]
        if known and requested in _WRITING_PERMISSION_RANK:
            ceilings = [
                _safe_permission_ceiling(
                    str(value.get("strength") or "qualified"),
                    requested,
                    has_evidence_chunks=bool(item.get("chunk_id")),
                )
                for value in known
            ]
            target = min(
                ceilings,
                key=lambda value: _WRITING_PERMISSION_RANK.get(value, 99),
            )
            if _WRITING_PERMISSION_RANK.get(target, 99) < _WRITING_PERMISSION_RANK.get(requested, 99):
                item["writing_permission"] = target
                corrections.append({
                    "stage": "evidence_packet",
                    "item_index": index,
                    "claim_ids": claim_ids,
                    "requested_permission": requested,
                    "normalized_permission": target,
                    "reason": (
                        "Deterministic claim-strength ceiling; evidence permission "
                        "was lowered without adding or upgrading evidence."
                    ),
                })
        normalized.append(item)
    return normalized, corrections


def _record_permission_corrections(
    ctx: SectionAuthoringContext,
    corrections: List[Dict[str, Any]],
) -> None:
    """Merge plan/evidence permission corrections into one audit artifact."""
    existing = _read_artifact(ctx.work_dir, "SECTION_PERMISSION_CORRECTIONS.json") or {}
    previous = list(existing.get("corrections", [])) if isinstance(existing, dict) else []
    merged = previous + list(corrections)
    # Preserve order while avoiding duplicate records after idempotent retries.
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in merged:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    _write_artifact(ctx.work_dir, "SECTION_PERMISSION_CORRECTIONS.json", {
        "schema_version": "r4.permission_corrections.v1",
        "section_id": ctx.section_id,
        "corrections": unique,
        "count": len(unique),
    })


def _record_submission_normalizations(
    ctx: SectionAuthoringContext,
    normalizations: List[Dict[str, Any]],
) -> None:
    """Persist deterministic schema repairs made at the tool boundary.

    The authoring model is allowed to use a small, documented set of common
    aliases, but this compatibility layer never invents a claim, chunk, or
    paper identifier.  Persisting every repair keeps the convenience layer
    auditable and makes repeated schema drift visible to later evaluation.
    """

    if not normalizations:
        return
    filename = "SECTION_SUBMISSION_NORMALIZATIONS.json"
    existing = _read_artifact(ctx.work_dir, filename) or {}
    previous = list(existing.get("normalizations", [])) if isinstance(existing, dict) else []
    merged = previous + list(normalizations)
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in merged:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    _write_artifact(ctx.work_dir, filename, {
        "schema_version": "r4.submission_normalizations.v1",
        "section_id": ctx.section_id,
        "normalizations": unique,
        "count": len(unique),
    })


def _as_list(value: Any) -> List[Any]:
    """Coerce a scalar identifier into a one-item list without changing it."""

    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _coerce_word_target(value: Any) -> Any:
    """Convert a simple model word target such as ``"180 words"`` to 180."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else value


def _normalize_argument_plan_contract(
    data: Dict[str, Any],
    graph: CanonicalAssetGraph,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Normalize common plan aliases while retaining fail-closed provenance.

    Only field names and trivial scalar/list shapes are repaired.  Paper IDs
    may be filled solely from the canonical owner of already supplied, valid
    chunk IDs.  Unknown IDs are deliberately retained so the validator rejects
    them instead of silently masking fabrication.
    """

    normalized = dict(data)
    repairs: List[Dict[str, Any]] = []
    if not normalized.get("argument_flow") and normalized.get("overall_argument"):
        normalized["argument_flow"] = normalized.get("overall_argument")
        repairs.append({
            "stage": "argument_plan",
            "field": "argument_flow",
            "action": "mapped_alias",
            "source_field": "overall_argument",
        })

    raw_paragraphs = normalized.get("paragraphs", [])
    if not isinstance(raw_paragraphs, list):
        return normalized, repairs

    alias_map = {
        "claim_ids": "key_claims",
        "evidence_chunks": "evidence_chunk_ids",
        "chunk_ids": "evidence_chunk_ids",
        "chunks_used": "evidence_chunk_ids",
        "permission": "writing_permission",
        "word_target": "expected_word_count",
    }
    list_fields = {"key_claims", "evidence_chunk_ids", "paper_ids"}
    paragraphs: List[Any] = []
    for index, raw in enumerate(raw_paragraphs):
        if not isinstance(raw, dict):
            paragraphs.append(raw)
            continue
        paragraph = dict(raw)
        for alias, canonical in alias_map.items():
            if not paragraph.get(canonical) and paragraph.get(alias) is not None:
                paragraph[canonical] = paragraph.get(alias)
                repairs.append({
                    "stage": "argument_plan",
                    "paragraph_index": index,
                    "field": canonical,
                    "action": "mapped_alias",
                    "source_field": alias,
                })
        for field_name in list_fields:
            value = paragraph.get(field_name)
            if value is not None and not isinstance(value, list):
                paragraph[field_name] = _as_list(value)
                repairs.append({
                    "stage": "argument_plan",
                    "paragraph_index": index,
                    "field": field_name,
                    "action": "coerced_to_list",
                })
        raw_permission = str(paragraph.get("writing_permission") or "").casefold().strip()
        normalized_permission = _normalize_writing_permission(
            paragraph.get("writing_permission")
        )
        if normalized_permission != raw_permission:
            paragraph["writing_permission"] = normalized_permission
            repairs.append({
                "stage": "argument_plan",
                "paragraph_index": index,
                "field": "writing_permission",
                "action": (
                    "normalized_enum_alias"
                    if raw_permission in _WRITING_PERMISSION_EXPLICIT_ALIASES
                    else "normalized_enum_prefix"
                ),
                "source_value": raw_permission,
                "normalized_value": paragraph["writing_permission"],
            })
        if "expected_word_count" in paragraph:
            coerced = _coerce_word_target(paragraph.get("expected_word_count"))
            if coerced != paragraph.get("expected_word_count"):
                paragraph["expected_word_count"] = coerced
                repairs.append({
                    "stage": "argument_plan",
                    "paragraph_index": index,
                    "field": "expected_word_count",
                    "action": "coerced_word_target",
                })
        paragraph.setdefault("paragraph_index", index)
        paragraph.setdefault("function", "synthesis")

        chunk_ids = [str(value) for value in _as_list(paragraph.get("evidence_chunk_ids"))]
        supplied_papers = [str(value) for value in _as_list(paragraph.get("paper_ids")) if str(value)]
        if chunk_ids and not supplied_papers and all(chunk_id in graph.chunks for chunk_id in chunk_ids):
            owners = list(dict.fromkeys(graph.chunks[chunk_id].paper_id for chunk_id in chunk_ids))
            paragraph["paper_ids"] = owners
            repairs.append({
                "stage": "argument_plan",
                "paragraph_index": index,
                "field": "paper_ids",
                "action": "filled_from_canonical_chunk_owners",
                "count": len(owners),
            })
        paragraphs.append(paragraph)
    normalized["paragraphs"] = paragraphs
    return normalized, repairs


def _canonical_claim_chunk_map(ctx: SectionAuthoringContext) -> Dict[str, List[str]]:
    """Return claim-to-chunk links already audited by Phase 3.

    Empty mappings remain absent so legacy fixtures are unaffected.  When the
    canonical R3 handoff provides links, R4 may narrow or omit a claim but must
    never silently attach that claim to another merely related paragraph.
    """

    result: Dict[str, List[str]] = {}
    for claim in ctx.section_data.get("claims", []):
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            continue
        chunk_ids = list(dict.fromkeys(
            str(value)
            for value in (
                list(claim.get("supporting_text_chunk_ids") or [])
                + list(claim.get("supporting_chunk_ids") or [])
                + list(claim.get("factual_support_chunk_ids") or [])
                + list(claim.get("core_chunk_ids") or [])
            )
            if str(value)
        ))
        if chunk_ids:
            result[str(claim["claim_id"])] = chunk_ids
    return result


def _scientific_numeric_literals(value: Any) -> set[str]:
    """Extract non-year numeric literals used by high-risk claim checks."""

    return {
        raw
        for raw in re.findall(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?", str(value or ""))
        if not re.fullmatch(r"(?:19|20)\d{2}", raw)
    }


def _claim_evidence_requirements(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
) -> List[Dict[str, Any]]:
    """Return compact, canonical claim-to-evidence repair guidance.

    This is deliberately derived from the Phase-3 handoff rather than from an
    authoring-model submission.  It gives a failed writer the exact legal IDs
    needed for one bounded repair without weakening provenance validation.
    """

    claims = {
        str(claim.get("claim_id")): claim
        for claim in ctx.section_data.get("claims", [])
        if isinstance(claim, dict) and claim.get("claim_id")
    }
    requirements: List[Dict[str, Any]] = []
    for claim_id, chunk_ids in _canonical_claim_chunk_map(ctx).items():
        legal_ids = [chunk_id for chunk_id in chunk_ids if chunk_id in graph.chunks]
        if not legal_ids:
            continue
        owners = list(dict.fromkeys(graph.chunks[chunk_id].paper_id for chunk_id in legal_ids))
        claim = claims.get(claim_id, {})
        requirements.append({
            "claim_id": claim_id,
            "effective_statement": str(
                claim.get("effective_statement")
                or claim.get("supported_rewrite")
                or claim.get("authoring_statement")
                or claim.get("statement")
                or ""
            )[:500],
            "allowed_chunk_ids": legal_ids[:12],
            "owner_paper_ids": owners[:12],
        })
    return requirements


def _plan_chunk_defaults(ctx: SectionAuthoringContext) -> Dict[str, Dict[str, Any]]:
    """Return accepted claim/permission defaults keyed by canonical chunk ID."""

    plan = _read_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json") or {}
    defaults: Dict[str, Dict[str, Any]] = {}
    for paragraph in plan.get("paragraphs", []):
        if not isinstance(paragraph, dict):
            continue
        claim_ids = [str(value) for value in _as_list(paragraph.get("key_claims")) if str(value)]
        permission = str(paragraph.get("writing_permission") or "").casefold().strip()
        for raw_chunk_id in _as_list(paragraph.get("evidence_chunk_ids")):
            chunk_id = str(raw_chunk_id)
            entry = defaults.setdefault(chunk_id, {"claim_ids": [], "permissions": []})
            entry["claim_ids"] = list(dict.fromkeys([*entry["claim_ids"], *claim_ids]))
            if permission in _WRITING_PERMISSION_RANK:
                entry["permissions"].append(permission)
    for entry in defaults.values():
        permissions = list(entry.pop("permissions", []))
        entry["writing_permission"] = (
            min(permissions, key=lambda item: _WRITING_PERMISSION_RANK[item])
            if permissions else ""
        )
    return defaults


def _normalize_evidence_packet_contract(
    data: Any,
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Normalize evidence packet shape against the accepted argument plan.

    A bare list is wrapped as ``items``.  Missing claim IDs and permissions are
    copied only from the already accepted plan for the same chunk, and a missing
    paper ID is copied only from the canonical asset graph.  Incorrect supplied
    IDs are never replaced and therefore still fail validation.
    """

    repairs: List[Dict[str, Any]] = []
    if isinstance(data, list):
        normalized: Dict[str, Any] = {"items": list(data)}
        repairs.append({
            "stage": "evidence_packet",
            "field": "items",
            "action": "wrapped_bare_list",
        })
    elif isinstance(data, dict):
        normalized = dict(data)
    else:
        return {}, repairs

    if "items" not in normalized and isinstance(normalized.get("evidence_items"), list):
        normalized["items"] = list(normalized.get("evidence_items") or [])
        repairs.append({
            "stage": "evidence_packet",
            "field": "items",
            "action": "mapped_alias",
            "source_field": "evidence_items",
        })
    if "items" not in normalized and isinstance(normalized.get("chunks_used"), list):
        normalized["items"] = list(normalized.get("chunks_used") or [])
        repairs.append({
            "stage": "evidence_packet",
            "field": "items",
            "action": "mapped_alias",
            "source_field": "chunks_used",
        })
    if "uncovered_claim_ids" not in normalized and normalized.get("uncovered_claims") is not None:
        normalized["uncovered_claim_ids"] = _as_list(normalized.get("uncovered_claims"))
        repairs.append({
            "stage": "evidence_packet",
            "field": "uncovered_claim_ids",
            "action": "mapped_alias",
            "source_field": "uncovered_claims",
        })

    defaults = _plan_chunk_defaults(ctx)
    items: List[Any] = []
    for index, raw in enumerate(normalized.get("items", [])):
        if not isinstance(raw, dict):
            items.append(raw)
            continue
        item = dict(raw)
        for alias in ("evidence_chunk_id", "evidence_chunk"):
            if not item.get("chunk_id") and item.get(alias):
                item["chunk_id"] = item.get(alias)
                repairs.append({
                    "stage": "evidence_packet",
                    "item_index": index,
                    "field": "chunk_id",
                    "action": "mapped_alias",
                    "source_field": alias,
                })
                break
        if not item.get("paper_id") and item.get("source_paper_id"):
            item["paper_id"] = item.get("source_paper_id")
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "paper_id",
                "action": "mapped_alias",
                "source_field": "source_paper_id",
            })
        if not item.get("claim_ids") and item.get("claims") is not None:
            item["claim_ids"] = _as_list(item.get("claims"))
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "claim_ids",
                "action": "mapped_alias",
                "source_field": "claims",
            })
        if not item.get("writing_permission") and item.get("permission") is not None:
            item["writing_permission"] = item.get("permission")
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "writing_permission",
                "action": "mapped_alias",
                "source_field": "permission",
            })
        if not item.get("support_hint") and item.get("support_hints") is not None:
            item["support_hint"] = item.get("support_hints")
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "support_hint",
                "action": "mapped_alias",
                "source_field": "support_hints",
            })

        chunk_id = str(item.get("chunk_id") or "")
        plan_default = defaults.get(chunk_id, {})
        if not item.get("claim_ids") and plan_default.get("claim_ids"):
            item["claim_ids"] = list(plan_default["claim_ids"])
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "claim_ids",
                "action": "filled_from_accepted_argument_plan",
            })
        elif "claim_ids" in item and not isinstance(item.get("claim_ids"), list):
            item["claim_ids"] = _as_list(item.get("claim_ids"))
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "claim_ids",
                "action": "coerced_to_list",
            })

        raw_permission = str(
            item.get("writing_permission") or ""
        ).casefold().strip()
        normalized_permission = _normalize_writing_permission(
            item.get("writing_permission")
        )
        if normalized_permission != raw_permission:
            item["writing_permission"] = normalized_permission
            requested = item["writing_permission"]
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "writing_permission",
                "action": (
                    "normalized_enum_alias"
                    if raw_permission in _WRITING_PERMISSION_EXPLICIT_ALIASES
                    else "normalized_enum_prefix"
                ),
                "source_value": str(
                    raw.get("writing_permission") or raw.get("permission") or ""
                ),
                "normalized_value": requested,
            })
        else:
            requested = normalized_permission
        if requested not in _WRITING_PERMISSION_RANK and plan_default.get("writing_permission"):
            item["writing_permission"] = plan_default["writing_permission"]
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "writing_permission",
                "action": "filled_from_accepted_argument_plan",
            })
        if not item.get("paper_id") and chunk_id in graph.chunks:
            item["paper_id"] = graph.chunks[chunk_id].paper_id
            repairs.append({
                "stage": "evidence_packet",
                "item_index": index,
                "field": "paper_id",
                "action": "filled_from_canonical_chunk_owner",
            })
        items.append(item)
    normalized["items"] = items

    if "uncovered_claim_ids" not in normalized:
        plan = _read_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json") or {}
        planned_claims = {
            str(claim_id)
            for paragraph in plan.get("paragraphs", [])
            if isinstance(paragraph, dict)
            for claim_id in _as_list(paragraph.get("key_claims"))
            if str(claim_id)
        }
        covered_claims = {
            str(claim_id)
            for item in items
            if isinstance(item, dict)
            for claim_id in _as_list(item.get("claim_ids"))
            if str(claim_id)
        }
        normalized["uncovered_claim_ids"] = sorted(planned_claims - covered_claims)
        if planned_claims:
            repairs.append({
                "stage": "evidence_packet",
                "field": "uncovered_claim_ids",
                "action": "derived_from_accepted_argument_plan",
                "count": len(normalized["uncovered_claim_ids"]),
            })
    return normalized, repairs


def _validate_argument_plan_data(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    raw_paragraphs: List[Dict[str, Any]],
) -> List[str]:
    errors: List[str] = []
    if _graph_ready_error(graph):
        return [_graph_ready_error(graph) or "asset graph unavailable"]
    valid_claims = {
        str(claim.get("claim_id")): str(
            claim.get("authoring_statement")
            or claim.get("statement")
            or claim.get("original_statement")
            or ""
        )
        for claim in ctx.section_data.get("claims", [])
        if isinstance(claim, dict) and claim.get("claim_id")
    }
    allowed_permissions = {
        "factual_assertion",
        "hedged_factual_assertion",
        "interpretive_synthesis",
        "common_background",
        "structural_transition",
        "evidence_gap_only",
    }
    permissions_requiring_chunks = {
        "factual_assertion", "hedged_factual_assertion",
    }
    permissions_without_chunks = {
        "common_background", "structural_transition", "evidence_gap_only", "interpretive_synthesis",
    }
    canonical_claim_chunks = _canonical_claim_chunk_map(ctx)
    for index, paragraph in enumerate(raw_paragraphs):
        if not isinstance(paragraph, dict):
            errors.append(f"paragraph[{index}] must be an object")
            continue
        chunk_ids = [str(item) for item in paragraph.get("evidence_chunk_ids", [])]
        paper_ids = [str(item) for item in paragraph.get("paper_ids", [])]
        permission = str(paragraph.get("writing_permission") or "factual_assertion")
        if permission not in allowed_permissions:
            errors.append(
                f"paragraph[{index}] has invalid writing_permission={permission!r}; "
                f"allowed={sorted(allowed_permissions)}"
            )
        claim_ids = [str(item) for item in paragraph.get("key_claims", [])]
        unknown_claims = [item for item in claim_ids if valid_claims and item not in valid_claims]
        if unknown_claims:
            errors.append(f"paragraph[{index}] unknown claim_id(s): {unknown_claims}")
        unknown_chunks = [item for item in chunk_ids if item not in graph.chunks]
        unknown_papers = [item for item in paper_ids if item not in graph.papers]
        if unknown_chunks:
            errors.append(f"paragraph[{index}] unknown chunk_id(s): {unknown_chunks}")
        if unknown_papers:
            errors.append(f"paragraph[{index}] unknown paper_id(s): {unknown_papers}")
        if unknown_chunks or unknown_papers:
            continue
        owner_ids = {graph.chunks[item].paper_id for item in chunk_ids}
        if chunk_ids and owner_ids != set(paper_ids):
            errors.append(
                f"paragraph[{index}] paper/chunk ownership mismatch: papers={sorted(set(paper_ids))}, "
                f"chunk_owners={sorted(owner_ids)}"
            )
        if permission in permissions_requiring_chunks and chunk_ids and claim_ids:
            mapped_claims = {
                claim_id: set(canonical_claim_chunks.get(claim_id, []))
                for claim_id in claim_ids
                if canonical_claim_chunks.get(claim_id)
            }
            unmapped_claims = [
                claim_id
                for claim_id in claim_ids
                if canonical_claim_chunks and claim_id not in canonical_claim_chunks
            ]
            if unmapped_claims:
                errors.append(
                    f"paragraph[{index}] factual writing cannot bind Phase-3 claim(s) "
                    f"without canonical support chunks: {unmapped_claims}; use an "
                    "evidence-gap or interpretive permission instead"
                )
            for claim_id, allowed_ids in mapped_claims.items():
                if not (set(chunk_ids) & allowed_ids):
                    errors.append(
                        f"paragraph[{index}] claim {claim_id} is not paired with any "
                        "canonical Phase-3 support chunk; allowed chunk_id(s): "
                        f"{sorted(allowed_ids)[:8]}"
                    )
            if mapped_claims:
                allowed_union = set().union(*mapped_claims.values())
                unrelated = [chunk_id for chunk_id in chunk_ids if chunk_id not in allowed_union]
                if unrelated:
                    errors.append(
                        f"paragraph[{index}] chunk_id(s) are not canonical support for "
                        f"the listed claim(s): {unrelated}"
                    )
        claim_text = " ".join(
            [str(paragraph.get("topic_sentence") or "")]
            + [valid_claims.get(item, item) for item in claim_ids]
        )
        if permission in permissions_requiring_chunks and chunk_ids:
            combined_chunk_text = " ".join(
                str(graph.chunks[chunk_id].normalized_text or "")
                for chunk_id in chunk_ids
            )
            unsupported_numbers = sorted(
                _scientific_numeric_literals(claim_text)
                - _scientific_numeric_literals(combined_chunk_text)
            )
            if unsupported_numbers:
                errors.append(
                    f"paragraph[{index}] quantitative literal(s) are absent from the "
                    f"selected canonical chunks: {unsupported_numbers}"
                )
        judgment_map = _claim_judgment_map(ctx)
        for claim_id in claim_ids:
            judgment = judgment_map.get(claim_id)
            if judgment:
                strength_error = _claim_strength_plan_error(
                    claim_id,
                    str(judgment.get("strength") or "qualified"),
                    permission,
                    location=f"paragraph[{index}]",
                )
                if strength_error:
                    errors.append(strength_error)
        if permission in permissions_requiring_chunks and not chunk_ids:
            errors.append(
                f"paragraph[{index}] writing_permission={permission!r} requires at least one "
                "canonical chunk. If no evidence is available for this section, use "
                "writing_permission='interpretive_synthesis' to synthesize from background "
                "knowledge without citing specific chunks, or 'common_background' / "
                "'structural_transition' / 'evidence_gap_only' for framing paragraphs. "
                "These permissions do not require evidence_chunk_ids or paper_ids."
            )
        if permission in permissions_without_chunks and not chunk_ids:
            if _MEASUREMENT_PATTERN.search(claim_text):
                errors.append(
                    f"paragraph[{index}] cannot use writing_permission={permission} for a "
                    "measurement-bearing statement without canonical evidence"
                )
            if re.search(
                r"\b(?:causes?|caused|drives?|driven by|results? in|arises? from|"
                r"because of|leads? to|proves?|demonstrates?)\b",
                claim_text,
                re.I,
            ):
                errors.append(
                    f"paragraph[{index}] cannot use writing_permission={permission} for a "
                    "strong causal or demonstrative claim without canonical evidence"
                )
        for chunk_id in chunk_ids:
            chunk = graph.chunks[chunk_id]
            if _has_verified_permission_provenance(chunk):
                permission_error = _permission_guard_error(
                    use_permission=chunk.use_permission,
                    writing_permission=permission,
                    claim_text=claim_text,
                    asset_id=chunk_id,
                    content_depth=chunk.content_depth,
                    allowed_claim_kinds=chunk.allowed_claim_kinds,
                    asset_text=chunk.normalized_text,
                )
                if permission_error:
                    errors.append(f"paragraph[{index}] {permission_error}")
            if chunk.scope_fit == "out_of_scope":
                errors.append(
                    f"paragraph[{index}] uses out-of-scope chunk {chunk_id}"
                )
            if (
                chunk.scope_fit == "adjacent"
                and permission == "factual_assertion"
            ):
                errors.append(
                    f"paragraph[{index}] cannot use adjacent-domain chunk "
                    f"{chunk_id} for an unqualified factual assertion; use "
                    "hedged_factual_assertion or interpretive_synthesis and "
                    "state the transfer boundary"
                )
            if chunk.scope_fit == "contextual" and permission == "factual_assertion":
                errors.append(
                    f"paragraph[{index}] cannot use contextual chunk {chunk_id} "
                    "for a factual assertion"
                )
            conflict = _restriction_conflict(claim_text, chunk.not_usable_for)
            if conflict:
                errors.append(
                    f"paragraph[{index}] violates {chunk_id} not_usable_for: {conflict}"
                )

    contract = dict(ctx.section_data.get("section_contract") or {})
    word_budget = _section_word_budget(ctx)
    if word_budget:
        planned_words = sum(
            max(0, int(paragraph.get("expected_word_count") or 0))
            for paragraph in raw_paragraphs
            if isinstance(paragraph, dict)
        )
        runaway_ceiling = int(word_budget * 4)
        if planned_words > runaway_ceiling:
            errors.append(
                f"argument plan exceeds the broad safety ceiling of "
                f"{runaway_ceiling} words (soft target={word_budget}); "
                f"plan requests {planned_words}"
            )
    paragraph_functions = list(contract.get("paragraph_functions") or [])
    if paragraph_functions:
        minimum = 1 if len(paragraph_functions) == 1 else max(2, len(paragraph_functions) - 1)
        if len(raw_paragraphs) < minimum:
            errors.append(
                f"argument plan requires at least {minimum} paragraphs for "
                f"{len(paragraph_functions)} contracted functions"
            )
    return errors


def _validate_evidence_items(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    raw_items: List[Dict[str, Any]],
    uncovered_claim_ids: List[str],
) -> tuple[List[str], List[Dict[str, Any]]]:
    errors: List[str] = []
    canonical: List[Dict[str, Any]] = []
    if _graph_ready_error(graph):
        return [_graph_ready_error(graph) or "asset graph unavailable"], canonical
    valid_claim_ids = {
        str(claim.get("claim_id")) for claim in ctx.section_data.get("claims", [])
        if isinstance(claim, dict) and claim.get("claim_id")
    }
    judgment_map = _claim_judgment_map(ctx)
    canonical_claim_chunks = _canonical_claim_chunk_map(ctx)
    for claim_id in uncovered_claim_ids:
        if valid_claim_ids and str(claim_id) not in valid_claim_ids:
            errors.append(f"unknown uncovered claim_id: {claim_id}")
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"item[{index}] must be an object")
            continue
        chunk_id = str(raw.get("chunk_id") or "")
        paper_id = str(raw.get("paper_id") or "")
        if chunk_id not in graph.chunks:
            errors.append(f"item[{index}] unknown chunk_id: {chunk_id}")
            continue
        chunk = graph.chunks[chunk_id]
        if paper_id not in graph.papers:
            errors.append(f"item[{index}] unknown paper_id: {paper_id}")
            continue
        if chunk.paper_id != paper_id:
            errors.append(
                f"item[{index}] paper/chunk ownership mismatch: {chunk_id} belongs to {chunk.paper_id}, not {paper_id}"
            )
            continue
        paper = graph.papers[paper_id]
        if chunk.scope_fit == "out_of_scope":
            errors.append(f"item[{index}] uses out-of-scope chunk {chunk_id}")
        # scope_fit is always canonicalized from the ledger; agent-supplied values are silently corrected
        claim_ids = [str(item) for item in raw.get("claim_ids", [])]
        unknown_claims = [item for item in claim_ids if valid_claim_ids and item not in valid_claim_ids]
        if unknown_claims:
            errors.append(f"item[{index}] unknown claim_id(s): {unknown_claims}")
        permission = str(raw.get("writing_permission") or "factual_assertion")
        if (
            permission in {"factual_assertion", "hedged_factual_assertion"}
            and canonical_claim_chunks
            and not claim_ids
        ):
            errors.append(
                f"item[{index}] factual evidence must name at least one canonical "
                "Phase-3 claim_id"
            )
        if permission in {"factual_assertion", "hedged_factual_assertion"} and claim_ids:
            for claim_id in claim_ids:
                allowed_ids = set(canonical_claim_chunks.get(claim_id, []))
                if canonical_claim_chunks and not allowed_ids:
                    errors.append(
                        f"item[{index}] claim {claim_id} has no canonical Phase-3 "
                        "support chunk and cannot receive factual evidence"
                    )
                elif allowed_ids and chunk_id not in allowed_ids:
                    errors.append(
                        f"item[{index}] chunk {chunk_id} is not canonical Phase-3 "
                        f"support for claim {claim_id}; allowed={sorted(allowed_ids)[:8]}"
                    )
        for claim_id in claim_ids:
            judgment = judgment_map.get(claim_id)
            if judgment:
                strength_error = _claim_strength_plan_error(
                    claim_id,
                    str(judgment.get("strength") or "qualified"),
                    permission,
                    location=f"item[{index}]",
                )
                if strength_error:
                    errors.append(strength_error)
        claim_text = " ".join(
            str(
                claim.get("authoring_statement")
                or claim.get("statement")
                or claim.get("original_statement")
                or ""
            )
            for claim in ctx.section_data.get("claims", [])
            if isinstance(claim, dict) and str(claim.get("claim_id")) in claim_ids
        )
        if _has_verified_permission_provenance(chunk):
            permission_error = _permission_guard_error(
                use_permission=chunk.use_permission,
                writing_permission=permission,
                claim_text=claim_text,
                asset_id=chunk_id,
                content_depth=chunk.content_depth,
                allowed_claim_kinds=chunk.allowed_claim_kinds,
                asset_text=chunk.normalized_text,
            )
            if permission_error:
                errors.append(f"item[{index}] {permission_error}")
        if chunk.scope_fit == "contextual" and permission == "factual_assertion":
            errors.append(f"item[{index}] cannot promote contextual evidence to factual_assertion")
        conflict = _restriction_conflict(claim_text, chunk.not_usable_for)
        if conflict and permission == "factual_assertion":
            errors.append(f"item[{index}] violates {paper_id} not_usable_for: {conflict}")
        # Span resolution: use model-provided spans if present; auto-select otherwise.
        raw_spans = [normalize_text(item) for item in raw.get("exact_spans", []) if normalize_text(item)]
        support_hint = str(raw.get("support_hint") or "")
        exact_span_source = "model_provided"

        if raw_spans:
            # Validate model-provided spans against canonical text.
            spans = []
            for span in raw_spans:
                if span in chunk.normalized_text:
                    spans.append(span)
                elif _normalize_span_for_match(span) in _normalize_span_for_match(chunk.normalized_text):
                    spans.append(span)  # typography variant — accept as-is
                else:
                    errors.append(
                        f"item[{index}] exact_span is not contained in canonical chunk {chunk_id}: {span[:80]!r}"
                    )
        else:
            # Auto-select from canonical chunk text.
            spans = []
            if permission != "evidence_gap_only":
                spans = _auto_select_spans(chunk.normalized_text, claim_text, support_hint)
                if not spans:
                    errors.append(
                        f"item[{index}] auto-span selection found no relevant sentence in chunk {chunk_id} "
                        f"for claim '{claim_text[:60]}'; provide a support_hint or choose a more relevant chunk"
                    )
                else:
                    exact_span_source = "deterministic_resolver"

        canonical.append({
            **raw,
            "chunk_id": chunk_id,
            "paper_id": paper_id,
            "paper_title": chunk.paper_title or paper.title,
            "year": chunk.paper_year,
            "literature_role": chunk.literature_role or paper.literature_role,
            "scope_fit": chunk.scope_fit,
            "evidence_level": chunk.evidence_level,
            "exact_spans": spans,
            "exact_span_source": exact_span_source,
            "not_usable_for": list(dict.fromkeys([
                *_as_list(chunk.not_usable_for),
                *_as_list(raw.get("not_usable_for")),
            ])),
        })
    return errors, canonical


# ---------------------------------------------------------------------------
# 1. load_authoring_context
# ---------------------------------------------------------------------------

def _make_load_authoring_context(ctx: SectionAuthoringContext):
    def load_authoring_context() -> str:
        """Load the section authoring context from Phase 2 outputs.

        Reads SECTION_MATERIAL_PACKAGE.json, SECTION_SOURCE_LEDGER.json,
        and section blueprint data. Writes SECTION_AUTHORING_CONTEXT.json.

        No arguments required.
        """
        try:
            # Load material package
            mp: Dict[str, Any] = {}
            if ctx.material_package_path and ctx.material_package_path.exists():
                try:
                    mp = json.loads(ctx.material_package_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            # Also try work_dir for previously written SECTION_MATERIAL_PACKAGE.json
            if not mp:
                mp_local = _read_artifact(ctx.work_dir, "SECTION_MATERIAL_PACKAGE.json") or {}
                mp = mp_local

            # Load source ledger
            sl: Dict[str, Any] = {}
            if ctx.source_ledger_path and ctx.source_ledger_path.exists():
                try:
                    sl = json.loads(ctx.source_ledger_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not sl:
                sl = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}

            sources = []
            for s in sl.get("sources", []):
                sources.append(AuthoringSourceEntry(
                    paper_id=s.get("paper_id", ""),
                    doi=s.get("doi", ""),
                    title=s.get("title", ""),
                    year=s.get("year"),
                    venue=s.get("venue", ""),
                    authors=list(s.get("authors", []))[:8],
                    literature_role=s.get("literature_role", ""),
                    scope_fit=s.get("scope_fit", "unreviewed"),
                    # The claim contract carries the exact selected IDs.  The
                    # source summary only needs a small paper-level preview;
                    # the complete ledger remains in the audit artifact.
                    canonical_chunk_ids=list(s.get("canonical_chunk_ids", []))[:8],
                    acquisition_status=str(s.get("acquisition_status", "unknown")),
                    not_usable_for=list(s.get("not_usable_for", []))[:8],
                    discovery_route=str(s.get("discovery_route", "unknown")),
                    materialization_route=str(
                        s.get("materialization_route", "not_materialized")
                    ),
                    content_depth=str(s.get("content_depth", "metadata")),
                    use_permission=str(
                        s.get("use_permission", "discovery_only")
                    ),
                    allowed_claim_kinds=list(s.get("allowed_claim_kinds", []))[:12],
                    # Full route events/conflict details remain in the source
                    # ledger and PHASE3_AUTHORING_AUDIT.json.  They are not
                    # repeated in every model-facing context.
                    route_events=[],
                    metadata_conflicts=[],
                    relation_roles=[],
                ))

            # Gather visual chunk IDs from KB — filtered by section's allowed papers
            graph = _build_asset_graph(ctx)
            if graph.is_empty:
                return json.dumps({
                    "status": "error",
                    "error": "Canonical Phase-2 asset graph is empty; authoring fails closed.",
                    "diagnostics": graph.diagnostics,
                }, ensure_ascii=False)
            visual_chunk_ids = sorted(graph.visuals)

            # Load gap report summary if available
            gap_summary_extra = ""
            phase2_gap_report: Dict[str, Any] = {}
            if ctx.gap_report_path and ctx.gap_report_path.exists():
                try:
                    gr = json.loads(ctx.gap_report_path.read_text(encoding="utf-8"))
                    phase2_gap_report = gr if isinstance(gr, dict) else {}
                    gap_summary_extra = gr.get("summary", "")[:300]
                except Exception:
                    pass
            if not phase2_gap_report:
                phase2_gap_report = _read_artifact(ctx.work_dir, "SECTION_GAP_REPORT.json") or {}
            if not gap_summary_extra:
                gap_summary_extra = _safe_str(
                    phase2_gap_report.get("summary")
                    or phase2_gap_report.get("overall_coverage_status")
                    or mp.get("gap_summary", ""),
                    300,
                )

            # Claims from the R4 bridge.  For a canonical Phase-3 handoff these
            # are already the compact authorable claims (evidence-gap claims
            # are excluded and recorded separately), so every authorable claim
            # ID must be exposed; an arbitrary twenty-item slice silently
            # dropped the remaining authorable claims in strong claim-pool
            # sections.
            claims = list(ctx.section_data.get("claims", []))
            def _effective_permission(value: str, chunk: Any | None = None) -> str:
                if value != "discovery_only" or chunk is None:
                    return value
                if _legacy_fulltext_fixture(chunk):
                    return "factual_support"
                return value
            source_permissions = {
                str(item.paper_id): _effective_permission(str(item.use_permission or "discovery_only"))
                for item in sources
                if str(item.paper_id)
            }
            chunk_to_paper = {
                chunk_id: chunk.paper_id for chunk_id, chunk in graph.chunks.items()
            }
            chunk_permissions = {
                chunk_id: _effective_permission(
                    str(chunk.use_permission or "discovery_only"), chunk
                )
                for chunk_id, chunk in graph.chunks.items()
            }
            bundle_path = (
                ctx.synthesis_bundle_path
                if ctx.synthesis_bundle_path
                else ctx.work_dir / "SYNTHESIS_BUNDLE.json"
            )
            synthesis_bundle: Dict[str, Any] = {}
            if bundle_path.exists():
                try:
                    loaded_bundle = json.loads(
                        bundle_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded_bundle, dict):
                        synthesis_bundle = loaded_bundle
                except Exception:
                    synthesis_bundle = {}
            if not synthesis_bundle:
                bundle_object = build_synthesis_bundle(
                    section=ctx.section_data,
                    claims=claims,
                    relation_edges=ctx.section_data.get("relation_edges") or [],
                    source_permissions=source_permissions,
                    chunk_permissions=chunk_permissions,
                    allowed_paper_ids=list(graph.papers),
                    allowed_chunk_ids=list(graph.chunks),
                    chunk_to_paper=chunk_to_paper,
                )
                candidate_pool_path = ctx.work_dir / "SYNTHESIS_CANDIDATE_POOL.json"
                atomic_write_json(
                    candidate_pool_path,
                    {
                        "schema_version": "synthesis_candidate_pool.v1",
                        "section_id": ctx.section_id,
                        "paper_ids": bundle_object.candidate_paper_ids,
                        "chunk_ids": bundle_object.candidate_chunk_ids,
                        "retrieval_mode": "on_demand_by_chunk_id",
                    },
                )
                synthesis_bundle = bundle_object.to_dict()
                synthesis_bundle["candidate_pool_ref"] = candidate_pool_path.name
                atomic_write_json(bundle_path, synthesis_bundle)
            minimum_synthesis_sources, available_synthesis_sources = (
                _synthesis_source_requirement(ctx, graph)
            )
            revision_instructions = dict(ctx.revision_instructions or {})
            existing_draft_text = str(ctx.existing_draft_text or "")

            authoring_ctx = _SchemaCtx(
                section_id=ctx.section_id,
                section_title=ctx.section_title,
                chapter_argument=ctx.chapter_argument,
                scope_guardrails=ctx.scope_guardrails,
                coverage_status=mp.get("coverage_status", "unknown"),
                total_sources=mp.get("total_sources", len(sources)),
                sources_by_role=mp.get("sources_by_role", {}),
                chunk_ids_by_role=mp.get("chunk_ids_by_role", {}),
                blocking_gaps_remain=bool(mp.get("blocking_gaps_remain", False)),
                gap_summary=gap_summary_extra or mp.get("gap_summary", ""),
                sources=sources,
                visual_chunk_ids=visual_chunk_ids,
                claims=claims,
                mentor_advice=dict(ctx.mentor_advice or {}),
                full_review_argument=ctx.full_review_argument,
                topic_identity=dict(ctx.topic_identity or {}),
                section_role=ctx.section_role,
                preceding_section_conclusion=ctx.preceding_section_conclusion,
                following_section_role=ctx.following_section_role,
                transition_contract=dict(ctx.transition_contract or {}),
                terminology_ledger=dict(ctx.terminology_ledger or {}),
                revision_instructions=revision_instructions,
                existing_draft_text=existing_draft_text,
                phase2_gap_report=phase2_gap_report,
                section_contract=dict(ctx.section_data.get("section_contract") or {}),
                minimum_synthesis_sources=minimum_synthesis_sources,
                available_synthesis_sources=available_synthesis_sources,
                synthesis_bundle=synthesis_bundle,
                judgment_ledger=list(ctx.section_data.get("judgment_ledger") or []),
                claim_strength_policy=dict(
                    ctx.section_data.get("claim_strength_policy") or {}
                ),
            )
            _write_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json", authoring_ctx)

            return json.dumps({
                "status": "ok",
                "section_id": ctx.section_id,
                "section_title": ctx.section_title,
                "chapter_argument": ctx.chapter_argument[:300],
                "coverage_status": authoring_ctx.coverage_status,
                "total_sources": authoring_ctx.total_sources,
                "sources_by_role": authoring_ctx.sources_by_role,
                "blocking_gaps_remain": authoring_ctx.blocking_gaps_remain,
                "gap_summary": authoring_ctx.gap_summary,
                "visual_chunk_count": len(visual_chunk_ids),
                "visual_chunk_ids": visual_chunk_ids,
                "claim_count": len(claims),
                "claims": claims,
                    "synthesis_bundle": synthesis_bundle,
                    "judgment_ledger": authoring_ctx.judgment_ledger,
                    "claim_strength_policy": authoring_ctx.claim_strength_policy,
                "section_contract": authoring_ctx.section_contract,
                "minimum_synthesis_sources": authoring_ctx.minimum_synthesis_sources,
                "available_synthesis_sources": authoring_ctx.available_synthesis_sources,
                "scope_guardrails": authoring_ctx.scope_guardrails,
                "asset_graph": {
                    "papers": len(graph.papers),
                    "chunks": len(graph.chunks),
                    "visuals": len(graph.visuals),
                    "source_kbs": list(graph.source_kbs),
                    "diagnostics": graph.diagnostics,
                },
                "writing_context_fields": {
                    "mentor_advice": bool(authoring_ctx.mentor_advice),
                    "full_review_argument": bool(authoring_ctx.full_review_argument),
                    "topic_identity": bool(authoring_ctx.topic_identity),
                    "section_role": bool(authoring_ctx.section_role),
                    "preceding_section_conclusion": bool(authoring_ctx.preceding_section_conclusion),
                    "following_section_role": bool(authoring_ctx.following_section_role),
                    "transition_contract": bool(authoring_ctx.transition_contract),
                    "terminology_ledger": bool(authoring_ctx.terminology_ledger),
                    "revision_instructions": bool(
                        authoring_ctx.revision_instructions
                    ),
                    "existing_draft_text": bool(
                        authoring_ctx.existing_draft_text
                    ),
                    "phase2_gap_report": bool(authoring_ctx.phase2_gap_report),
                },
                "revision_mode": bool(revision_instructions),
                "revision_instructions": revision_instructions,
                "existing_draft_text": existing_draft_text,
                "artifact": "SECTION_AUTHORING_CONTEXT.json",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:300]})

    return load_authoring_context


# ---------------------------------------------------------------------------
# 2. inspect_material_package
# ---------------------------------------------------------------------------

def _make_inspect_material_package(ctx: SectionAuthoringContext):
    def inspect_material_package() -> str:
        """Inspect the available sources, chunks, and gaps for this section.

        Reads SECTION_AUTHORING_CONTEXT.json and SECTION_GAP_REPORT.json.
        Returns a role-by-role breakdown of available evidence.
        No arguments required.
        """
        try:
            ac = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json") or {}
            gap_data = _read_artifact(ctx.work_dir, "SECTION_GAP_REPORT.json") or {}

            chunk_ids_by_role: Dict[str, List[str]] = ac.get("chunk_ids_by_role", {})
            sources_by_role: Dict[str, int] = ac.get("sources_by_role", {})
            graph = _build_asset_graph(ctx)
            evidence_portfolio = _build_authoring_evidence_portfolio(
                ctx,
                graph,
                ac,
            )

            role_detail = {}
            for role, chunk_ids in chunk_ids_by_role.items():
                role_detail[role] = {
                    "chunk_count": len(chunk_ids),
                    "paper_count": sources_by_role.get(role, 0),
                    "chunk_ids": chunk_ids[:10],
                    "status": "sufficient" if len(chunk_ids) >= 3 else ("partial" if chunk_ids else "empty"),
                }

            gaps = [
                {"role": g.get("role"), "severity": g.get("severity"),
                 "description": g.get("description", "")[:200]}
                for g in gap_data.get("gaps", [])
            ]

            return json.dumps({
                "status": "ok",
                "section_id": ctx.section_id,
                "coverage_status": ac.get("coverage_status", "unknown"),
                "total_sources": ac.get("total_sources", 0),
                "blocking_gaps_remain": ac.get("blocking_gaps_remain", False),
                "role_detail": role_detail,
                "documented_gaps": gaps,
                "visual_chunk_count": len(ac.get("visual_chunk_ids", [])),
                "claim_count": len(ac.get("claims", [])),
                "claims": ac.get("claims", []),
                "evidence_portfolio": evidence_portfolio,
                "section_contract": ac.get("section_contract", {}),
                "scope_guardrails": ac.get("scope_guardrails", []),
                "writing_context": {
                    "mentor_advice": ac.get("mentor_advice", {}),
                    "full_review_argument": ac.get("full_review_argument", ""),
                    "section_role": ac.get("section_role", ""),
                    "preceding_section_conclusion": ac.get("preceding_section_conclusion", ""),
                    "following_section_role": ac.get("following_section_role", ""),
                    "transition_contract": ac.get("transition_contract", {}),
                    "terminology_ledger": ac.get("terminology_ledger", {}),
                    "phase2_gap_report": ac.get("phase2_gap_report", {}),
                },
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:300]})

    return inspect_material_package


# ---------------------------------------------------------------------------
# 3. retrieve_chunk_text
# ---------------------------------------------------------------------------

def _make_retrieve_chunk_text(ctx: SectionAuthoringContext):
    served_chunk_ids: set[str] = set()

    def retrieve_chunk_text(chunk_ids: str) -> str:
        """Retrieve full text and metadata for one or more chunk IDs from the KB.

        The agent must only use chunk_ids it received from load_authoring_context
        or inspect_material_package — never construct or guess IDs.

        Args:
            chunk_ids: JSON array of chunk_id strings, or a single chunk_id string.

        Returns JSON mapping chunk_id → {text, paper_id, paper_title, evidence_level}.
        """
        try:
            ids = json.loads(chunk_ids) if chunk_ids.strip().startswith("[") else [chunk_ids]
        except Exception:
            ids = [chunk_ids]
        ids = [i.strip() for i in ids if i.strip()]
        if not ids:
            return json.dumps({"status": "error", "error": "No chunk_ids provided"})

        graph = _build_asset_graph(ctx)
        if graph.is_empty:
            return json.dumps({
                "status": "error",
                "error": "Canonical Phase-2 asset graph is empty; retrieval fails closed.",
                "diagnostics": graph.diagnostics,
            }, ensure_ascii=False)
        requested_ids = list(ids)
        automatic_batch_expansion: List[str] = []
        if len(ids) == 1 and ids[0] in graph.chunks and not served_chunk_ids:
            ac = _read_artifact(
                ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json"
            ) or {}
            portfolio = _build_authoring_evidence_portfolio(ctx, graph, ac)
            recommended = [
                str(item)
                for item in portfolio.get("recommended_batch_chunk_ids", [])
                if str(item) in graph.chunks
            ]
            if len(recommended) > 1:
                core_limit = max(
                    4,
                    int(
                        (ctx.section_data or {}).get(
                            "authoring_core_chunk_limit", 12
                        )
                        or 12
                    ),
                )
                ids = list(dict.fromkeys([ids[0], *recommended]))[:core_limit]
                automatic_batch_expansion = [
                    item for item in ids if item not in requested_ids
                ]
        records = {chunk_id: graph.chunks[chunk_id] for chunk_id in ids if chunk_id in graph.chunks}
        missing = [i for i in ids if i not in records]
        already_retrieved = [i for i in ids if i in served_chunk_ids and i in records]
        new_records = {
            chunk_id: record
            for chunk_id, record in records.items()
            if chunk_id not in served_chunk_ids
        }
        served_chunk_ids.update(new_records)

        def excerpt(text: str, limit: int = 1800) -> tuple[str, bool]:
            normalized = repair_likely_scientific_mojibake(
                normalize_text(text)
            )
            if len(normalized) <= limit:
                return normalized, False
            return normalized[:limit].rsplit(" ", 1)[0].rstrip() + " …", True

        chunks_payload: Dict[str, Dict[str, Any]] = {}
        for chunk_id, record in new_records.items():
            text_excerpt, truncated = excerpt(record.normalized_text)
            chunks_payload[chunk_id] = {
                "chunk_id": record.chunk_id,
                "paper_id": record.paper_id,
                "paper_title": record.paper_title,
                "paper_year": record.paper_year,
                "text": text_excerpt,
                "text_is_excerpt": truncated,
                "canonical_char_count": len(record.normalized_text),
                "evidence_level": record.evidence_level,
                "source_kind": record.source_kind,
                "source_kb": record.source_kb,
            }
        allowed_assets = []
        repair_portfolio: Dict[str, Any] = {}
        if missing:
            ac = _read_artifact(
                ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json"
            ) or {}
            repair_portfolio, allowed_assets = _portfolio_repair_assets(
                ctx,
                graph,
                ac,
                max_assets=40,
            )
        return json.dumps({
            "status": "ok",
            "found": len(records),
            "newly_returned": len(new_records),
            "already_retrieved": already_retrieved,
            "missing": missing,
            "requested_ids": requested_ids,
            "automatic_batch_expansion": automatic_batch_expansion,
            "allowed_assets_after_missing_id": allowed_assets,
            "repair_batch_chunk_ids": [
                item["chunk_id"] for item in allowed_assets
            ],
            "minimum_synthesis_sources": repair_portfolio.get(
                "minimum_synthesis_sources", 0
            ),
            "available_synthesis_sources": repair_portfolio.get(
                "available_synthesis_sources", 0
            ),
            "source_kbs": list(graph.source_kbs),
            "chunks": chunks_payload,
            "note": (
                "Repeated IDs are not re-emitted because their text is already in the conversation. "
                "Returned text may be a bounded excerpt; the canonical full chunk remains in the KB "
                "and is used by deterministic validation. A first singleton request may be expanded "
                "to one high-value chunk from each recommended paper to prevent costly serial discovery."
            ),
        }, ensure_ascii=False)

    return retrieve_chunk_text


# ---------------------------------------------------------------------------
# 4. inspect_visual_assets
# ---------------------------------------------------------------------------

def _make_inspect_visual_assets(ctx: SectionAuthoringContext):
    inspection_completed = False
    cached_visual_ids: List[str] = []

    def inspect_visual_assets() -> str:
        """List all visual chunks available for this section.

        Returns visual_chunk_ids, captions, and argument types so the agent
        can reference them in submit_visual_placement.
        No arguments required.
        """
        nonlocal inspection_completed, cached_visual_ids
        try:
            if inspection_completed:
                return json.dumps({
                    "status": "ok",
                    "already_inspected": True,
                    "candidate_visual_ids": cached_visual_ids,
                    "message": (
                        "Visual details were already returned earlier in this task. "
                        "Reuse that result instead of requesting the same payload again."
                    ),
                }, ensure_ascii=False)
            graph = _build_asset_graph(ctx)
            if graph.is_empty:
                return json.dumps({
                    "status": "error",
                    "visual_assets": [],
                    "error": "Canonical Phase-2 asset graph is empty; visual inspection fails closed.",
                    "diagnostics": graph.diagnostics,
                }, ensure_ascii=False)
            query_text = " ".join([
                ctx.section_title,
                ctx.chapter_argument,
                *[
                    str(claim.get("statement") or "")
                    for claim in ctx.section_data.get("claims", [])
                    if isinstance(claim, dict)
                ],
            ]).lower()
            stopwords = {
                "this", "that", "with", "from", "into", "through", "section",
                "review", "paper", "figure", "visual", "claim", "result", "results",
                "their", "these", "those", "between", "under", "using",
            }
            query_tokens = {
                token for token in re.findall(r"[a-z][a-z0-9-]{3,}", query_text)
                if token not in stopwords
            }
            ranked: List[tuple[float, Dict[str, Any]]] = []
            for visual in graph.visuals.values():
                if not (visual.accepted and visual.relevant_or_reranked):
                    continue
                visual_text = " ".join([
                    visual.caption, visual.argument_type, visual.argument_claim,
                    visual.parent_label, visual.subfigure_label,
                ]).lower()
                visual_tokens = {
                    token for token in re.findall(r"[a-z][a-z0-9-]{3,}", visual_text)
                    if token not in stopwords
                }
                overlap = query_tokens & visual_tokens
                score = len(overlap) / max(1, len(query_tokens))
                ranked.append((score, {
                    "visual_chunk_id": visual.visual_id,
                    "paper_id": visual.paper_id,
                    "caption": visual.caption,
                    "local_image_path": visual.local_image_path,
                    "visual_argument_type": visual.argument_type,
                    "visual_argument_claim": visual.argument_claim,
                    "visual_argument_status": visual.status,
                    "chunk_kind": visual.kind,
                    "relevance_status": visual.relevance_status,
                    "placement_eligible": visual.accepted and visual.relevant_or_reranked,
                    "section_relevance_score": round(score, 4),
                    "matched_terms": sorted(overlap)[:10],
                }))
            ranked.sort(key=lambda item: (-item[0], item[1]["visual_chunk_id"]))
            total_available = len(ranked)
            visuals = [item[1] for item in ranked[:8]]
            inspection_completed = True
            cached_visual_ids = [item["visual_chunk_id"] for item in visuals]
            if not visuals:
                return json.dumps({"status": "ok", "visual_assets": [],
                                   "message": "No KB available or no visuals for this section."})
            return json.dumps({
                "status": "ok",
                "total_available": total_available,
                "returned": len(visuals),
                "visual_assets": visuals,
                "selection_note": (
                    "Candidates are capped at eight and ranked by lexical overlap with the "
                    "section argument. Placement still requires canonical validation."
                ),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:300]})

    return inspect_visual_assets


# ---------------------------------------------------------------------------
# 5. submit_argument_plan
# ---------------------------------------------------------------------------

def _make_submit_argument_plan(ctx: SectionAuthoringContext):
    def submit_argument_plan(plan_json: str) -> str:
        """Submit the paragraph-level argument plan for this section.

        Writes SECTION_ARGUMENT_PLAN.json.

        Args:
            plan_json: JSON with:
                - argument_flow: str — brief narrative of how paragraphs build the argument
                - paragraphs: list of {
                    paragraph_index, function, topic_sentence, key_claims,
                    evidence_chunk_ids, paper_ids, writing_permission, expected_word_count}
                - open_questions: list of str (optional)
        """
        # A repeated identical submission is idempotent.  A materially improved
        # plan is allowed to replace the old one after the same deterministic
        # provenance and breadth checks.  This is required for audit-driven
        # revision: prose cannot broaden if its upstream argument plan is
        # permanently frozen.
        existing_plan = _read_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json")
        try:
            data = json.loads(plan_json)
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"Invalid JSON: {exc}"})

        if not isinstance(data, dict):
            return json.dumps({"status": "error", "error": "argument plan must be a JSON object"})
        graph = _build_asset_graph(ctx)
        data, contract_normalizations = _normalize_argument_plan_contract(data, graph)
        _record_submission_normalizations(ctx, contract_normalizations)

        raw_paragraphs = data.get("paragraphs", [])
        if not isinstance(raw_paragraphs, list) or not raw_paragraphs:
            return json.dumps({"status": "error", "error": "paragraphs must be a non-empty list"})

        raw_paragraphs, permission_corrections = _normalize_argument_plan_permissions(
            ctx, raw_paragraphs
        )
        _record_permission_corrections(ctx, permission_corrections)

        validation_errors = _validate_argument_plan_data(ctx, graph, raw_paragraphs)
        if validation_errors:
            ac = _read_artifact(
                ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json"
            ) or {}
            portfolio, allowed_assets = _portfolio_repair_assets(
                ctx,
                graph,
                ac,
                max_assets=40,
            )
            compact_allowed_assets = [
                {
                    key: item.get(key)
                    for key in (
                        "chunk_id", "paper_id", "paper_title", "scope_fit",
                        "use_permission", "evidence_level", "literature_role",
                    ) if item.get(key) is not None
                }
                for item in allowed_assets[:12]
                if isinstance(item, dict)
            ]
            return json.dumps({
                "status": "rejected",
                "error": "Argument plan failed canonical asset validation; no artifact was written.",
                "rejected_items": validation_errors[:8],
                "rejected_item_count": len(validation_errors),
                "contract_normalizations": contract_normalizations,
                "permission_corrections": permission_corrections,
                "minimum_synthesis_sources": portfolio.get(
                    "minimum_synthesis_sources", 0
                ),
                "available_synthesis_sources": portfolio.get(
                    "available_synthesis_sources", 0
                ),
                "distinct_repair_papers": len({
                    item["paper_id"] for item in allowed_assets
                }),
                "repair_batch_chunk_ids": [
                    item["chunk_id"] for item in allowed_assets[:12]
                ],
                "claim_evidence_requirements": _claim_evidence_requirements(
                    ctx, graph
                ),
                "allowed_assets": compact_allowed_assets,
                "allowed_asset_count": len(allowed_assets),
                "repair_instruction": (
                    "Reuse only the exact chunk_id/paper_id pairs above. Retrieve "
                    "repair_batch_chunk_ids in one call, then assign distinct "
                    "argumentative jobs across enough papers to meet "
                    "minimum_synthesis_sources. For adjacent or contextual evidence, "
                    "follow transfer_boundary_note explicitly. Never infer a DOI, "
                    "suffix, or chunk index."
                ),
            }, ensure_ascii=False)

        paragraphs = []
        for i, p in enumerate(raw_paragraphs):
            if not isinstance(p, dict):
                continue
            paragraphs.append(ParagraphPlan(
                paragraph_index=int(p.get("paragraph_index", i)),
                function=_safe_str(p.get("function", "evidence"), 60),
                topic_sentence=_safe_str(p.get("topic_sentence", ""), 300),
                key_claims=list(p.get("key_claims", []))[:8],
                evidence_chunk_ids=list(p.get("evidence_chunk_ids", []))[:12],
                paper_ids=list(p.get("paper_ids", []))[:8],
                writing_permission=_safe_str(p.get("writing_permission", "factual_assertion"), 60),
                expected_word_count=int(p.get("expected_word_count", 0)),
            ))

        # Build evidence_coverage: role → chunk_ids from SECTION_AUTHORING_CONTEXT
        ac = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json") or {}
        all_chunk_ids_used = [cid for p in paragraphs for cid in p.evidence_chunk_ids]
        chunk_ids_by_role = ac.get("chunk_ids_by_role", {})
        evidence_coverage: Dict[str, List[str]] = {}
        for role, role_chunks in chunk_ids_by_role.items():
            used = [c for c in all_chunk_ids_used if c in role_chunks]
            if used:
                evidence_coverage[role] = used

        plan = SectionArgumentPlan(
            section_id=ctx.section_id,
            chapter_argument=ctx.chapter_argument,
            argument_flow=_safe_str(data.get("argument_flow", ""), 800),
            paragraphs=paragraphs,
            total_expected_words=sum(p.expected_word_count for p in paragraphs),
            evidence_coverage=evidence_coverage,
            open_questions=list(data.get("open_questions", []))[:10],
            created_at=_NOW(),
        )
        plan_payload = plan.model_dump()
        if existing_plan and existing_plan.get("paragraphs"):
            comparable_fields = (
                "argument_flow",
                "paragraphs",
                "total_expected_words",
                "evidence_coverage",
                "open_questions",
            )
            if all(
                existing_plan.get(field) == plan_payload.get(field)
                for field in comparable_fields
            ):
                return json.dumps({
                    "status": "already_completed",
                    "note": "The submitted argument plan is identical to the accepted plan.",
                    "paragraph_count": len(existing_plan["paragraphs"]),
                    "total_expected_words": existing_plan.get("total_expected_words", 0),
                    "artifact": "SECTION_ARGUMENT_PLAN.json",
                }, ensure_ascii=False)
            revision_index = _append_artifact_history(
                ctx.work_dir,
                "SECTION_ARGUMENT_PLAN_HISTORY.json",
                "SECTION_ARGUMENT_PLAN.json",
                existing_plan,
                reason="audit-driven argument-plan replacement",
            )
            if (ctx.work_dir / "SECTION_DRAFT_EN.md").exists():
                (ctx.work_dir / _AUDIT_STALE_MARKER).write_text(
                    "1",
                    encoding="utf-8",
                )
            status = "revised"
        else:
            revision_index = 0
            status = "ok"
        _write_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json", plan)

        return json.dumps({
            "status": status,
            "revision_index": revision_index,
            "paragraph_count": len(paragraphs),
            "total_expected_words": plan.total_expected_words,
            "evidence_roles_covered": list(evidence_coverage.keys()),
            "contract_normalizations": contract_normalizations,
            "permission_corrections": permission_corrections,
            "artifact": "SECTION_ARGUMENT_PLAN.json",
        }, ensure_ascii=False)

    return submit_argument_plan


# ---------------------------------------------------------------------------
# 6. build_evidence_packet
# ---------------------------------------------------------------------------

def _classify_blocking_uncovered(
    ctx: SectionAuthoringContext,
    uncovered_claim_ids: List[str],
) -> List[str]:
    """Return only claim IDs that block writing (load_bearing + factual_assertion paragraphs).

    interpretive_synthesis, background, and transition paragraphs may lack a
    directly-cited chunk and should not trigger an infinite evidence-gathering loop.
    """
    if not uncovered_claim_ids:
        return []
    # Build a map: claim_id → paragraph writing_permission from the argument plan
    plan = _read_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json") or {}
    permission_by_claim: dict[str, str] = {}
    for para in plan.get("paragraphs", []):
        if not isinstance(para, dict):
            continue
        perm = str(para.get("writing_permission") or "factual_assertion")
        for cid in para.get("key_claims", []):
            permission_by_claim[str(cid)] = perm

    # Build claim load_bearing flag from section data
    load_bearing_by_claim: dict[str, bool] = {}
    for claim in ctx.section_data.get("claims", []):
        if isinstance(claim, dict) and claim.get("claim_id"):
            load_bearing_by_claim[str(claim["claim_id"])] = bool(claim.get("load_bearing", True))

    blocking = []
    for cid in uncovered_claim_ids:
        perm = permission_by_claim.get(cid, "factual_assertion")
        load_bearing = load_bearing_by_claim.get(cid, True)
        # interpretive_synthesis, background, context paragraphs are never blocking
        if perm in {"interpretive_synthesis", "background", "context", "transition"}:
            continue
        # Non-load-bearing claims with factual_assertion but no chunk → evidence_gap, not blocking
        if not load_bearing:
            continue
        blocking.append(cid)
    return blocking


def _make_build_evidence_packet(ctx: SectionAuthoringContext):
    def build_evidence_packet(evidence_json: str) -> str:
        """Build and persist the evidence packet for this section.

        Writes SECTION_EVIDENCE_PACKET.json with chunk–claim mappings.

        Args:
            evidence_json: JSON with:
                - items: list of {
                    chunk_id (required), paper_id (required),
                    claim_ids (required list), writing_permission (required),
                    support_hint (optional str — keywords/phrases to focus span selection),
                    exact_spans (optional — omit to let the tool auto-select relevant sentences),
                    literature_role, scope_fit, not_usable_for (list)}
                  Do NOT copy-paste text from chunk text. Omit exact_spans and let the tool
                  select the most relevant sentences from the canonical chunk automatically.
                - uncovered_claim_ids: list of claim IDs with no supporting chunk (optional)
        """
        # Evidence packets are monotonic, revision-capable inventories.  New
        # canonical items may be merged after an audit requests broader support;
        # an identical repeat remains idempotent.
        existing_packet = _read_artifact(ctx.work_dir, "SECTION_EVIDENCE_PACKET.json")
        try:
            data = json.loads(evidence_json)
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"Invalid JSON: {exc}"})

        graph = _build_asset_graph(ctx)
        data, contract_normalizations = _normalize_evidence_packet_contract(data, ctx, graph)
        _record_submission_normalizations(ctx, contract_normalizations)

        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            return json.dumps({"status": "error", "error": "items must be a list"})
        uncovered_claim_ids = list(data.get("uncovered_claim_ids", []))[:20]
        raw_items, permission_corrections = _normalize_evidence_packet_permissions(
            ctx, raw_items
        )
        _record_permission_corrections(ctx, permission_corrections)
        validation_errors, canonical_items = _validate_evidence_items(
            ctx, graph, raw_items, uncovered_claim_ids
        )
        if validation_errors:
            return json.dumps({
                "status": "rejected",
                "error": "Evidence packet failed canonical provenance validation; no artifact was written.",
                "rejected_items": validation_errors,
                "contract_normalizations": contract_normalizations,
                "permission_corrections": permission_corrections,
            }, ensure_ascii=False)

        if existing_packet and existing_packet.get("items"):
            combined: Dict[tuple, Dict[str, Any]] = {}
            for raw in list(existing_packet.get("items", [])) + list(canonical_items):
                if not isinstance(raw, dict):
                    continue
                key = (
                    str(raw.get("chunk_id") or ""),
                    str(raw.get("writing_permission") or "factual_assertion"),
                    tuple(sorted(str(item) for item in raw.get("claim_ids", []))),
                )
                combined[key] = raw
            existing_keys = {
                (
                    str(raw.get("chunk_id") or ""),
                    str(raw.get("writing_permission") or "factual_assertion"),
                    tuple(sorted(str(item) for item in raw.get("claim_ids", []))),
                )
                for raw in existing_packet.get("items", [])
                if isinstance(raw, dict)
            }
            new_keys = set(combined)
            if (
                new_keys == existing_keys
                and list(existing_packet.get("uncovered_claim_ids", []))
                == uncovered_claim_ids
            ):
                return json.dumps({
                    "status": "already_completed",
                    "note": "No new canonical evidence item was supplied.",
                    "total_items": existing_packet.get("total_items", 0),
                    "items_by_role": existing_packet.get("items_by_role", {}),
                    "uncovered_claim_count": len(existing_packet.get("uncovered_claim_ids", [])),
                    "artifact": "SECTION_EVIDENCE_PACKET.json",
                }, ensure_ascii=False)
            canonical_items = list(combined.values())
            evidence_revision_index = _append_artifact_history(
                ctx.work_dir,
                "SECTION_EVIDENCE_PACKET_HISTORY.json",
                "SECTION_EVIDENCE_PACKET.json",
                existing_packet,
                reason="audit-driven evidence-packet extension",
            )
            evidence_status = "extended"
        else:
            evidence_revision_index = 0
            evidence_status = "ok"

        # Classify uncovered claims: only load-bearing factual claims block writing.
        blocking_uncovered = _classify_blocking_uncovered(ctx, uncovered_claim_ids)

        items = []
        role_counts: Dict[str, int] = {}
        for raw in canonical_items:
            chunk_id = _safe_str(raw.get("chunk_id", ""), 128)
            paper_id = _safe_str(raw.get("paper_id", ""), 128)
            role = _safe_str(raw.get("literature_role", ""), 60)
            items.append(EvidenceItem(
                chunk_id=chunk_id,
                paper_id=paper_id,
                paper_title=_safe_str(raw.get("paper_title", ""), 200),
                year=raw.get("year"),
                literature_role=role,
                scope_fit=_safe_str(raw.get("scope_fit", "unreviewed"), 60),
                evidence_level=_safe_str(raw.get("evidence_level", "fulltext"), 60),
                exact_spans=list(raw.get("exact_spans", []))[:6],
                exact_span_source=_safe_str(raw.get("exact_span_source", "model_provided"), 60),
                claim_ids=list(raw.get("claim_ids", []))[:8],
                writing_permission=_safe_str(raw.get("writing_permission", "factual_assertion"), 60),
                not_usable_for=list(raw.get("not_usable_for", []))[:8],
            ))
            role_counts[role] = role_counts.get(role, 0) + 1

        packet = SectionEvidencePacket(
            section_id=ctx.section_id,
            items=items,
            total_items=len(items),
            items_by_role=role_counts,
            uncovered_claim_ids=uncovered_claim_ids,
            created_at=_NOW(),
        )
        _write_artifact(ctx.work_dir, "SECTION_EVIDENCE_PACKET.json", packet)
        if (ctx.work_dir / "SECTION_DRAFT_EN.md").exists():
            (ctx.work_dir / _AUDIT_STALE_MARKER).write_text("1", encoding="utf-8")

        return json.dumps({
            "status": evidence_status,
            "revision_index": evidence_revision_index,
            "total_items": len(items),
            "items_by_role": role_counts,
            "uncovered_claim_count": len(packet.uncovered_claim_ids),
            "blocking_uncovered_count": len(blocking_uncovered),
            "blocking_uncovered_ids": blocking_uncovered,
            "contract_normalizations": contract_normalizations,
            "permission_corrections": permission_corrections,
            "note": (
                "Proceed to submit_section_draft — uncovered claims are interpretive_synthesis or non-load-bearing."
                if packet.uncovered_claim_ids and not blocking_uncovered else ""
            ),
            "artifact": "SECTION_EVIDENCE_PACKET.json",
        }, ensure_ascii=False)

    return build_evidence_packet


# ---------------------------------------------------------------------------
# 7. submit_section_draft
# ---------------------------------------------------------------------------

def _make_submit_section_draft(
    ctx: SectionAuthoringContext,
    *,
    persist_last_valid: bool = True,
):
    def submit_section_draft(draft_text: str, summary: str = "") -> str:
        """Submit the English prose draft for this section.

        Writes SECTION_DRAFT_EN.md. Called once for the initial draft, then again
        after each revision pass. Also updates SECTION_REVISION_HISTORY.json.

        Args:
            draft_text: Full English markdown text of the section draft.
                Must NOT contain fabricated citations or ungrounded numerical claims.
                Citation markers must use [REF:paper_id] format.
            summary: One-sentence summary of what this draft version addresses (optional).
        """
        if not draft_text or not draft_text.strip():
            return json.dumps({"status": "error", "error": "draft_text must not be empty"})

        text = repair_likely_scientific_mojibake(draft_text.strip())
        wc = _word_count(text)
        pc = _paragraph_count(text)

        # Load revision history
        rh_data = _read_artifact(ctx.work_dir, "SECTION_REVISION_HISTORY.json")
        if rh_data:
            try:
                rh = SectionRevisionHistory.model_validate(rh_data)
            except Exception:
                rh = SectionRevisionHistory(section_id=ctx.section_id, created_at=_NOW())
        else:
            rh = SectionRevisionHistory(section_id=ctx.section_id, created_at=_NOW())

        prev_wc = 0
        if (ctx.work_dir / "SECTION_DRAFT_EN.md").exists():
            prev_text = (ctx.work_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
            prev_wc = _word_count(prev_text)
            stage = "revision"
        else:
            stage = "initial_draft"

        rh.revisions.append(RevisionEntry(
            revision_index=len(rh.revisions),
            stage=stage,
            reason=_safe_str(summary, 300),
            word_count_before=prev_wc,
            word_count_after=wc,
            summary=_safe_str(summary, 300),
            created_at=_NOW(),
        ))
        rh.total_revisions = len(rh.revisions)
        rh.current_stage = stage

        atomic_write_text(ctx.work_dir / "SECTION_DRAFT_EN.md", text)
        _write_artifact(ctx.work_dir, "SECTION_REVISION_HISTORY.json", rh)
        last_valid = (
            _persist_last_valid_section_candidate(
                ctx,
                summary=summary,
                validation_level="syntax",
            )
            if persist_last_valid
            else {
                "saved": False,
                "reason": "deferred_until_compact_candidate_audit",
            }
        )
        # Invalidate any previous audit — run_citation_audit must be called again
        (ctx.work_dir / _AUDIT_STALE_MARKER).write_text("1", encoding="utf-8")

        return json.dumps({
            "status": "ok",
            "word_count": wc,
            "paragraph_count": pc,
            "revision_index": rh.total_revisions - 1,
            "last_valid_candidate": last_valid,
            "audit_stale": True,
            "note": "Citation audit invalidated. Call run_citation_audit before validate_authoring_package.",
            "artifacts": ["SECTION_DRAFT_EN.md", "SECTION_REVISION_HISTORY.json"],
        }, ensure_ascii=False)

    return submit_section_draft



# ---------------------------------------------------------------------------
# Citation audit — shared by run_citation_audit, validate_authoring_package,
# and try_auto_finalize.  Never calls the LLM; pure-Python deterministic.
# ---------------------------------------------------------------------------

def _split_draft_sentences(text: str) -> List[str]:
    """Split prose into auditable sentences while excluding Markdown headings."""
    prose_lines: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if re.match(r"^#{1,6}\s+\S", line):
            continue
        if re.fullmatch(r"(?:=+|-+)", line):
            continue
        prose_lines.append(raw_line)
    prose = "\n".join(prose_lines).strip()
    return [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+", prose)
        if item.strip()
    ]


_CITATION_MATCH_STOPWORDS = frozenset({
    "about", "after", "also", "because", "been", "being", "between", "could",
    "from", "have", "into", "more", "only", "other", "over", "same", "such",
    "than", "that", "their", "these", "they", "this", "through", "under", "very",
    "when", "where", "which", "while", "with", "would",
})


def _citation_match_tokens(text: str) -> set[str]:
    """Return stable content tokens for deterministic sentence/chunk matching."""
    return {
        token for token in re.findall(r"[a-z0-9]+", normalize_text(text).lower())
        if len(token) >= 3 and token not in _CITATION_MATCH_STOPWORDS
    }


def _trusted_section_tokens(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
) -> set[str]:
    """Build one broad trusted vocabulary set for obvious-fabrication checks."""

    parts: List[str] = [
        str(ctx.section_title or ""),
        str(ctx.chapter_argument or ""),
    ]
    for claim in ctx.section_data.get("claims", []):
        if not isinstance(claim, dict):
            continue
        for key in (
            "effective_statement",
            "supported_rewrite",
            "authoring_statement",
            "statement",
        ):
            if claim.get(key):
                parts.append(str(claim[key]))
                break
    for chunk in graph.chunks.values():
        parts.append(str(chunk.normalized_text or ""))
    return _citation_match_tokens(" ".join(parts))


def _resolve_citation_sentence_index(
    raw: Dict[str, Any],
    sentences: List[str],
) -> int:
    """Resolve a map entry by a unique snippet before trusting its index."""
    snippet = normalize_text(raw.get("sentence_snippet"))
    if snippet:
        snippet_lower = snippet.lower()
        matches = [
            index
            for index, sentence in enumerate(sentences)
            if snippet_lower in normalize_text(sentence).lower()
        ]
        if len(matches) == 1:
            return matches[0]
    try:
        return int(raw.get("sentence_index"))
    except (TypeError, ValueError):
        return -1


def _citation_evidence_texts(
    ctx: SectionAuthoringContext,
    sentence: str,
    known_chunks: List[Any],
) -> List[str]:
    """Select local passages without replacing canonical chunk text."""
    packet = _read_artifact(ctx.work_dir, "SECTION_EVIDENCE_PACKET.json") or {}
    spans_by_chunk: Dict[str, List[str]] = {}
    for item in packet.get("items", []):
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "")
        spans = [
            normalize_text(span)
            for span in item.get("exact_spans", [])
            if normalize_text(span)
        ]
        if spans:
            spans_by_chunk.setdefault(chunk_id, []).extend(spans)

    target_tokens = _citation_match_tokens(
        re.sub(r"\[REF:[^\]]+\]", "", sentence)
    )
    target_numbers = set(
        re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", sentence)
    )

    def _predicate_stem(token: str) -> str:
        value = token.lower()
        if value.endswith("ing") and len(value) > 5:
            return value[:-3]
        if value.endswith("ed") and len(value) > 4 and not value.endswith("eed"):
            return value[:-2]
        if value.endswith("es") and len(value) > 4:
            return value[:-2]
        if value.endswith("s") and len(value) > 3:
            return value[:-1]
        return value

    negated_target_predicates = {
        _predicate_stem(match.group(1))
        for match in re.finditer(
            r"\b(?:do|does|did|can|could|is|are|was|were|has|have|had)?\s*"
            r"(?:not|never|cannot|can't)\s+([a-z][a-z-]{2,})",
            sentence,
            re.I,
        )
    }
    result: List[str] = []
    for chunk in known_chunks:
        candidates = list(spans_by_chunk.get(chunk.chunk_id, []))
        candidates.extend(
            normalize_text(part)
            for part in re.split(r"(?<=[.!?])\s+", chunk.normalized_text)
            if normalize_text(part)
        )
        if not candidates:
            continue
        ranked = sorted(
            candidates,
            key=lambda text: (
                sum(
                    1
                    for stem in negated_target_predicates
                    if re.search(
                        rf"\b{re.escape(stem)}(?:s|es|ed|ing)?\b",
                        text,
                        re.I,
                    )
                ),
                len(
                    target_numbers
                    & set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", text))
                ),
                len(target_tokens & _citation_match_tokens(text)),
                -abs(len(_citation_match_tokens(text)) - len(target_tokens)),
            ),
            reverse=True,
        )
        selected = [ranked[0]]
        covered_tokens = target_tokens & _citation_match_tokens(ranked[0])
        covered_numbers = target_numbers & set(
            re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", ranked[0])
        )
        for candidate in ranked[1:]:
            added_tokens = (
                target_tokens & _citation_match_tokens(candidate)
            ) - covered_tokens
            added_numbers = (
                target_numbers
                & set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", candidate))
            ) - covered_numbers
            if added_numbers or len(added_tokens) >= 2:
                selected.append(candidate)
                break
        result.append(" ".join(selected))
    return result


def _auto_build_citation_map(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    sentences: List[str],
) -> List[Dict[str, Any]]:
    """Build one citation-map entry per marker-bearing prose sentence."""
    entries: List[Dict[str, Any]] = []
    for sentence_index, sentence in enumerate(sentences):
        markers = list(dict.fromkeys(_extract_ref_markers(sentence)))
        if not markers:
            continue
        paper_ids: List[str] = []
        chunk_ids: List[str] = []
        for marker in markers:
            if marker in graph.chunks:
                owner = graph.chunks[marker].paper_id
                if owner not in paper_ids:
                    paper_ids.append(owner)
                chunk_ids.append(marker)
            else:
                paper_ids.append(marker)
                chunk_ids.extend(
                    _infer_citation_chunks(ctx, graph, sentence, [marker])
                )
        entries.append({
            "sentence_index": sentence_index,
            "sentence_snippet": sentence[:120],
            "paper_ids": paper_ids,
            "chunk_ids": list(dict.fromkeys(chunk_ids)),
            "citation_type": "synthesis" if len(paper_ids) > 1 else "factual",
            "audit_note": "auto-inferred from draft markers",
        })
    return entries


def _infer_citation_chunks(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
    sentence: str,
    paper_ids: List[str],
) -> List[str]:
    """Choose one locally available supporting chunk per cited paper.

    Evidence-packet chunks are preferred, then other canonical section chunks
    are considered.  This helper is deterministic and remains subject to the
    same provenance and citation-support gates as an explicit mapping.
    """
    packet = _read_artifact(ctx.work_dir, "SECTION_EVIDENCE_PACKET.json") or {}
    preferred_by_paper: Dict[str, List[str]] = {}
    packet_text_by_chunk: Dict[str, str] = {}
    for item in packet.get("items", []):
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paper_id") or "")
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id in graph.chunks and graph.chunks[chunk_id].paper_id == paper_id:
            preferred_by_paper.setdefault(paper_id, []).append(chunk_id)
            packet_text_by_chunk[chunk_id] = " ".join(
                normalize_text(span)
                for span in item.get("exact_spans", [])
                if normalize_text(span)
            )

    sentence_tokens = _citation_match_tokens(
        re.sub(r"\[REF:[^\]]+\]", "", sentence)
    )
    selected: List[str] = []
    for paper_id in paper_ids:
        preferred = preferred_by_paper.get(paper_id, [])
        fallback = [
            chunk_id
            for chunk_id, chunk in graph.chunks.items()
            if chunk.paper_id == paper_id and chunk_id not in preferred
        ]
        candidates = preferred + fallback
        if not candidates:
            continue

        def _ranking_text(chunk_id: str) -> str:
            canonical = graph.chunks[chunk_id].normalized_text
            packet_hint = packet_text_by_chunk.get(chunk_id, "")
            return f"{packet_hint} {canonical}".strip()

        ranked = sorted(
            candidates,
            key=lambda chunk_id: (
                len(sentence_tokens & _citation_match_tokens(_ranking_text(chunk_id))),
                1 if chunk_id in preferred else 0,
                -len(graph.chunks[chunk_id].normalized_text),
            ),
            reverse=True,
        )
        selected.append(ranked[0])
    return selected


def _compute_citation_audit(
    ctx: SectionAuthoringContext,
    citations: Any,
    persist: bool = True,
) -> Dict[str, Any]:
    """Run the one canonical, fail-closed citation audit.

    The draft markers are authoritative for paper identity.  A supplied map
    can provide chunk IDs, but every explicit or inferred chunk still has to
    belong exactly to the marked paper and pass the local support gate.
    """
    try:
        draft_path = ctx.work_dir / "SECTION_DRAFT_EN.md"
        if not draft_path.exists():
            return {
                "status": "error",
                "error": "SECTION_DRAFT_EN.md not found",
                "total_citations": 0,
                "total_flags": 0,
                "blocking_flags": 0,
                "audit_passed": False,
                "flags_detail": [],
                "editorial_warnings": [],
                "revision_control": _read_revision_control(ctx),
            }
        if isinstance(citations, dict):
            citations_raw: Any = [citations]
        elif isinstance(citations, list):
            citations_raw = list(citations)
        else:
            return {
                "status": "error",
                "error": "citation map must be a JSON list or object",
                "total_citations": 0,
                "total_flags": 0,
                "blocking_flags": 0,
                "audit_passed": False,
                "flags_detail": [],
                "editorial_warnings": [],
                "revision_control": _read_revision_control(ctx),
            }
        draft_text = draft_path.read_text(encoding="utf-8")
        graph = _build_asset_graph(ctx)
        trusted_tokens = _trusted_section_tokens(ctx, graph)
        sentences = _split_draft_sentences(draft_text)

        # An empty submitted map is a request for deterministic marker recovery,
        # not permission to skip citation mapping.
        if not citations_raw:
            citations_raw = _auto_build_citation_map(ctx, graph, sentences)

        all_flags: List[AuditFlag] = []
        entries_by_index: Dict[int, List[Dict[str, Any]]] = {}
        for raw_index, raw in enumerate(citations_raw):
            if not isinstance(raw, dict):
                all_flags.append(AuditFlag(
                    flag_type="unknown_ref",
                    sentence_index=-1,
                    severity="blocking",
                    reason=f"citation_map[{raw_index}] is not an object.",
                    suggested_fix="Submit one structured citation entry per cited sentence.",
                    risk_class="mapping",
                ))
                continue
            sentence_index = _resolve_citation_sentence_index(raw, sentences)
            entries_by_index.setdefault(sentence_index, []).append(raw)

        if graph.is_empty:
            all_flags.append(AuditFlag(
                flag_type="unknown_ref",
                sentence_index=-1,
                severity="blocking",
                reason="Canonical Phase-2 asset graph is empty; citation audit fails closed.",
                suggested_fix="Provide a validated Phase-2 source ledger and KB.",
                risk_class="provenance",
            ))

        computed_entries: List[CitationEntry] = []
        valid_papers_cited: set[str] = set()
        synthesis_cues = re.compile(
            r"\b(?:collectively|together|combined|across (?:these|the)|synthesis|"
            r"converge|adjacent (?:field|domain|literature)|transfer(?:able|red)?|"
            r"by analogy|in comparison)\b",
            re.I,
        )

        for sentence_index, sentence in enumerate(sentences):
            markers = list(dict.fromkeys(_extract_ref_markers(sentence)))
            bound = entries_by_index.pop(sentence_index, [])
            if markers and len(bound) != 1:
                all_flags.append(AuditFlag(
                    flag_type="missing_citation_mapping",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason=(
                        "Every sentence containing [REF:*] markers requires exactly one "
                        "matching citation-map entry; "
                        f"found {len(bound)}."
                    ),
                    suggested_fix="Map this cited sentence to its canonical paper and chunk IDs.",
                    risk_class="mapping",
                ))
                continue
            if not markers and bound:
                all_flags.append(AuditFlag(
                    flag_type="sentence_mapping_mismatch",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason="Citation-map entry targets a sentence with no [REF:*] marker.",
                    suggested_fix="Correct sentence_index or remove the stale mapping.",
                    risk_class="mapping",
                ))
                continue
            if not markers:
                continue

            raw = bound[0]
            raw_paper_ids = raw.get("paper_ids") or []
            raw_chunk_ids = raw.get("chunk_ids") or []
            if isinstance(raw_paper_ids, str):
                raw_paper_ids = [raw_paper_ids]
            if isinstance(raw_chunk_ids, str):
                raw_chunk_ids = [raw_chunk_ids]
            paper_ids = [str(item) for item in raw_paper_ids if str(item)] or list(markers)
            chunk_ids = [str(item) for item in raw_chunk_ids if str(item)]
            if not chunk_ids:
                chunk_ids = _infer_citation_chunks(ctx, graph, sentence, paper_ids)
            snippet = normalize_text(raw.get("sentence_snippet"))
            sentence_normalized = normalize_text(sentence)
            entry_flags_before = len(all_flags)

            if snippet and snippet not in sentence_normalized:
                all_flags.append(AuditFlag(
                    flag_type="sentence_mapping_mismatch",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason="sentence_snippet is not contained in the indexed draft sentence.",
                    suggested_fix="Regenerate the citation map from the current draft.",
                    risk_class="mapping",
                ))
            if set(paper_ids) != set(markers):
                all_flags.append(AuditFlag(
                    flag_type="sentence_mapping_mismatch",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason=(
                        f"Draft markers {markers} do not match mapped paper_ids "
                        f"{paper_ids}."
                    ),
                    suggested_fix="Map every and only the [REF:*] markers in this sentence.",
                    risk_class="mapping",
                ))

            for paper_id in markers:
                if paper_id not in graph.papers:
                    all_flags.append(AuditFlag(
                        flag_type="unknown_ref",
                        sentence_snippet=sentence[:150],
                        sentence_index=sentence_index,
                        severity="blocking",
                        reason=(
                            f"[REF:{paper_id}] is absent from the canonical Phase-2 asset graph."
                        ),
                        suggested_fix="Remove the claim or cite an allowed Phase-2 source.",
                        risk_class="provenance",
                    ))
            unknown_chunks = [
                chunk_id for chunk_id in chunk_ids if chunk_id not in graph.chunks
            ]
            for chunk_id in unknown_chunks:
                all_flags.append(AuditFlag(
                    flag_type="unknown_ref",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason=(
                        f"chunk_id '{chunk_id}' is absent from the canonical Phase-2 asset graph."
                    ),
                    suggested_fix="Use a chunk returned by retrieve_chunk_text.",
                    risk_class="provenance",
                ))
            known_chunks = [
                graph.chunks[chunk_id]
                for chunk_id in chunk_ids
                if chunk_id in graph.chunks
            ]
            owner_ids = {chunk.paper_id for chunk in known_chunks}
            # This is an exact set check, not a permissive membership check:
            # every cited paper must own a mapped chunk and no other paper may
            # be smuggled into the sentence through an allowed ID pair.
            if owner_ids != set(markers):
                all_flags.append(AuditFlag(
                    flag_type="paper_chunk_mismatch",
                    sentence_snippet=sentence[:150],
                    sentence_index=sentence_index,
                    severity="blocking",
                    reason=(
                        f"Mapped chunk owners {sorted(owner_ids)} do not match cited "
                        f"papers {sorted(set(markers))}."
                    ),
                    suggested_fix="Bind each paper marker to chunk(s) owned by that same paper.",
                    risk_class="provenance",
                ))

            for chunk in known_chunks:
                if chunk.scope_fit == "out_of_scope":
                    all_flags.append(AuditFlag(
                        flag_type="scope_violation",
                        sentence_snippet=sentence[:150],
                        sentence_index=sentence_index,
                        severity="blocking",
                        reason=f"Chunk {chunk.chunk_id} is canonically out_of_scope.",
                        suggested_fix="Remove the claim or use direct/adjacent evidence.",
                        risk_class="scope_violation",
                    ))
                if chunk.scope_fit == "contextual" and not synthesis_cues.search(sentence):
                    all_flags.append(AuditFlag(
                        flag_type="scope_violation",
                        sentence_snippet=sentence[:150],
                        sentence_index=sentence_index,
                        severity="blocking",
                        reason=(
                            f"Contextual chunk {chunk.chunk_id} cannot carry a standalone "
                            "factual claim."
                        ),
                        suggested_fix=(
                            "Use the source only in explicit synthesis/context, or bind a "
                            "direct/adjacent chunk."
                        ),
                        risk_class="scope_violation",
                    ))
                conflict = _restriction_conflict(sentence, chunk.not_usable_for)
                if conflict:
                    all_flags.append(AuditFlag(
                        flag_type="scope_violation",
                        sentence_snippet=sentence[:150],
                        sentence_index=sentence_index,
                        severity="blocking",
                        reason=(
                            f"Citation violates {chunk.chunk_id} not_usable_for: {conflict}"
                        ),
                        suggested_fix="Narrow or remove the unsupported claim.",
                        risk_class="scope_violation",
                    ))

            provenance_valid = len(all_flags) == entry_flags_before and bool(known_chunks)
            if provenance_valid:
                valid_papers_cited.update(markers)

            verdict = "not_entailed"
            note = "Canonical provenance checks failed."
            if provenance_valid:
                local_evidence = _citation_evidence_texts(ctx, sentence, known_chunks)
                verdict, note, _ = assess_citation_support(
                    sentence,
                    local_evidence,
                    multi_source_synthesis=bool(
                        len(markers) > 1 and synthesis_cues.search(sentence)
                    ),
                )
                if verdict != "entailed":
                    strict = (
                        _requires_strict_citation_entailment(sentence)
                        or _citation_note_hard_failure(
                            note,
                            trusted_tokens=trusted_tokens,
                        )
                    )
                    all_flags.append(AuditFlag(
                        flag_type="overclaim",
                        sentence_snippet=sentence[:150],
                        sentence_index=sentence_index,
                        severity="blocking" if strict else "important",
                        reason=note,
                        suggested_fix=(
                            "Revise the measurement, causal claim, comparison, or "
                            "source-specific result to match the local passage."
                            if strict
                            else "Inspect this weak local match during editorial review; "
                            "retain only if it is a bounded review-level synthesis."
                        ),
                        risk_class=_citation_risk_class(sentence),
                    ))

            computed_entries.append(CitationEntry(
                sentence_index=sentence_index,
                sentence_snippet=sentence[:200],
                chunk_ids=chunk_ids[:8],
                paper_ids=paper_ids[:8],
                citation_type=(
                    "synthesis"
                    if len(markers) > 1 and synthesis_cues.search(sentence)
                    else _safe_str(raw.get("citation_type") or "factual", 40)
                ),
                entailment_verdict=verdict,
                audit_note=note[:200],
            ))

        for stale_index, stale_entries in entries_by_index.items():
            for _ in stale_entries:
                all_flags.append(AuditFlag(
                    flag_type="sentence_mapping_mismatch",
                    sentence_index=stale_index,
                    severity="blocking",
                    reason="Citation-map sentence_index is outside the current draft.",
                    suggested_fix="Regenerate the citation map from the current draft.",
                    risk_class="mapping",
                ))

        for sent in _find_uncited_measurements(draft_text):
            all_flags.append(AuditFlag(
                flag_type="uncited_fact",
                sentence_snippet=sent,
                sentence_index=-1,
                severity="blocking",
                reason="Sentence contains a measurement-like number with no [REF:*] marker.",
                suggested_fix="Add a supported citation or remove the specific value.",
                risk_class="exact_measurement",
            ))

        for snippet, risk_class in _find_uncited_high_risk_claims(draft_text):
            all_flags.append(AuditFlag(
                flag_type="uncited_high_risk_claim",
                sentence_snippet=snippet,
                sentence_index=-1,
                severity="blocking",
                reason=f"High-risk sentence ({risk_class}) lacks a [REF:*] citation.",
                suggested_fix="Add [REF:paper_id] or rephrase as bounded synthesis.",
                risk_class=risk_class,
            ))

        diversity_error = _synthesis_source_diversity_error(
            ctx,
            graph,
            valid_papers_cited,
        )
        if diversity_error:
            all_flags.append(AuditFlag(
                flag_type="insufficient_synthesis_source_diversity",
                sentence_index=-1,
                severity="important",
                reason=diversity_error,
                suggested_fix=(
                    "Use the additional audited papers already assigned in the argument "
                    "plan to broaden section-level comparison and synthesis."
                ),
                risk_class="section_level",
            ))

        blocking = [
            flag for flag in all_flags
            if flag.severity == "blocking" and not flag.resolved
        ]
        warnings = [flag for flag in all_flags if flag.severity != "blocking"]
        overclaims = [flag for flag in all_flags if flag.flag_type == "overclaim"]
        citation_flags = [
            flag for flag in all_flags
            if flag.flag_type in {
                "uncited_fact",
                "uncited_high_risk_claim",
                "unknown_ref",
                "missing_citation_mapping",
                "sentence_mapping_mismatch",
                "paper_chunk_mismatch",
                "insufficient_synthesis_source_diversity",
            }
        ]
        scope_flags = [flag for flag in all_flags if flag.flag_type == "scope_violation"]
        audit = SectionAuthoringAudit(
            section_id=ctx.section_id,
            overclaim_flags=overclaims,
            citation_flags=citation_flags,
            scope_flags=scope_flags,
            total_blocking_flags=len(blocking),
            total_flags=len(all_flags),
            audit_passed=not blocking,
            audit_summary=(
                f"PASSED: {len(all_flags)} flags, 0 blocking."
                if not blocking
                else f"FAILED: {len(blocking)} blocking flags remain."
            ),
            created_at=_NOW(),
        )
        citation_map = SectionCitationMap(
            section_id=ctx.section_id,
            citations=computed_entries,
            total_cited_sentences=len(computed_entries),
            uncited_sentences=max(0, len(sentences) - len(computed_entries)),
            papers_cited=sorted(valid_papers_cited),
            created_at=_NOW(),
        )
        revision_control = _update_revision_control(
            ctx,
            blocking,
            warnings,
            persist=persist,
        )
        if persist:
            _write_artifact(ctx.work_dir, "SECTION_CITATION_MAP.json", citation_map)
            _write_artifact(ctx.work_dir, "SECTION_AUTHORING_AUDIT.json", audit)
            stale = ctx.work_dir / _AUDIT_STALE_MARKER
            if stale.exists():
                stale.unlink()

        flag_details = [_audit_flag_dict(flag) for flag in all_flags]
        editorial_warnings = [_audit_flag_dict(flag) for flag in warnings]
        return {
            "status": "ok",
            "total_citations": len(computed_entries),
            "total_cited_sentences": len(computed_entries),
            "papers_cited": sorted(valid_papers_cited),
            "total_flags": len(all_flags),
            "blocking_flags": len(blocking),
            "audit_passed": audit.audit_passed,
            "flags_detail": flag_details,
            "editorial_warnings": editorial_warnings,
            "revision_control": revision_control,
            "minimum_synthesis_sources": _synthesis_source_requirement(ctx, graph)[0],
            "available_synthesis_sources": _synthesis_source_requirement(ctx, graph)[1],
            "used_synthesis_sources": len(valid_papers_cited),
            "citation_map": citation_map.model_dump(),
            "audit": audit.model_dump(),
        }
    except Exception as exc:
        logger.exception("_compute_citation_audit: unexpected error")
        return {
            "status": "error",
            "error": str(exc),
            "total_citations": 0,
            "total_flags": 0,
            "blocking_flags": 0,
            "audit_passed": False,
            "flags_detail": [],
            "editorial_warnings": [],
            "revision_control": _read_revision_control(ctx),
        }


# ---------------------------------------------------------------------------
# 8. run_citation_audit
# ---------------------------------------------------------------------------

def _make_run_citation_audit(ctx: SectionAuthoringContext):

    def run_citation_audit(citation_map_json: str) -> str:
        """Audit citations in the current draft.

        Pass citation_map_json as a JSON array — one entry per sentence that
        contains a [REF:*] marker.  If you pass an empty array ("[]"), entries
        are auto-inferred from the draft markers.  Prefer passing an explicit
        map so chunk ownership is verified against your evidence packet.

        Each entry:
          {"sentence_index": <int, 0-based>,
           "sentence_snippet": "<first ~15 words of the sentence>",
           "paper_ids": ["doi:xxx", ...],
           "chunk_ids": ["chunk_abc", ...],
           "citation_type": "factual"}

        Returns: total_citations, papers_cited, blocking_flags, flags_detail.
        Call this after every submit_section_draft or submit_revision.
        """
        try:
            citations_raw = json.loads(citation_map_json)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": f"Invalid citation_map_json: {exc}",
                "total_citations": 0,
                "total_flags": 0,
                "blocking_flags": 0,
                "audit_passed": False,
                "flags_detail": [],
                "editorial_warnings": [],
                "revision_control": _read_revision_control(ctx),
            })
        result = _compute_citation_audit(ctx, citations_raw, persist=True)
        if result.get("status") == "ok":
            result["artifacts"] = ["SECTION_CITATION_MAP.json", "SECTION_AUTHORING_AUDIT.json"]
            result.pop("citation_map", None)
            result.pop("audit", None)
        return json.dumps(result, ensure_ascii=False)

    return run_citation_audit


# ---------------------------------------------------------------------------
# 9. submit_revision
# ---------------------------------------------------------------------------

def _make_submit_revision(ctx: SectionAuthoringContext):
    def submit_revision(revised_text: str, flags_resolved: str = "[]", summary: str = "") -> str:
        """Submit a revised draft after addressing audit flags.

        Overwrites SECTION_DRAFT_EN.md with the revised text.
        Updates SECTION_REVISION_HISTORY.json with resolved flag count.

        Args:
            revised_text: Full revised English markdown text.
            flags_resolved: JSON array of flag types resolved in this revision (optional).
            summary: Brief description of what was changed (optional).
        """
        if not revised_text or not revised_text.strip():
            return json.dumps({"status": "error", "error": "revised_text must not be empty"})

        try:
            resolved_list = json.loads(flags_resolved) if flags_resolved.strip().startswith("[") else []
        except Exception:
            resolved_list = []

        text = repair_likely_scientific_mojibake(revised_text.strip())
        wc = _word_count(text)

        # Once the deterministic audit has identified a repeated or
        # non-improving blocker, another paraphrase is not a new experiment.
        # Retain the durable candidate and terminate the local revision loop.
        control = _read_revision_control(ctx)
        attempts = int(control.get("revision_attempts", 0) or 0)
        max_attempts = int(
            control.get("max_local_revision_attempts", _MAX_LOCAL_REVISION_ATTEMPTS)
            or _MAX_LOCAL_REVISION_ATTEMPTS
        )
        if _has_durable_section_candidate(ctx.work_dir) and (
            bool(control.get("stop_revising")) or attempts >= max_attempts
        ):
            return _write_awaiting_human_review_package(
                ctx,
                reason=str(
                    control.get("stop_reason")
                    or "max_local_revision_attempts_reached"
                ),
                control=control,
            )

        prev_wc = 0
        if (ctx.work_dir / "SECTION_DRAFT_EN.md").exists():
            prev_text = (ctx.work_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
            prev_wc = _word_count(prev_text)

        # Load audit to count remaining flags after resolution
        audit_data = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_AUDIT.json") or {}
        total_flags = audit_data.get("total_flags", 0)
        remaining_flags = max(0, total_flags - len(resolved_list))

        # Update revision history
        rh_data = _read_artifact(ctx.work_dir, "SECTION_REVISION_HISTORY.json")
        if rh_data:
            try:
                rh = SectionRevisionHistory.model_validate(rh_data)
            except Exception:
                rh = SectionRevisionHistory(section_id=ctx.section_id, created_at=_NOW())
        else:
            rh = SectionRevisionHistory(section_id=ctx.section_id, created_at=_NOW())

        rh.revisions.append(RevisionEntry(
            revision_index=len(rh.revisions),
            stage="revision",
            reason=_safe_str(summary or f"Resolved: {resolved_list}", 300),
            word_count_before=prev_wc,
            word_count_after=wc,
            flags_resolved=len(resolved_list),
            flags_remaining=remaining_flags,
            summary=_safe_str(summary, 300),
            created_at=_NOW(),
        ))
        rh.total_revisions = len(rh.revisions)
        rh.current_stage = "revision"

        atomic_write_text(ctx.work_dir / "SECTION_DRAFT_EN.md", text)
        _write_artifact(ctx.work_dir, "SECTION_REVISION_HISTORY.json", rh)
        last_valid = _persist_last_valid_section_candidate(
            ctx,
            summary=summary,
            validation_level="syntax",
        )
        control["revision_attempts"] = attempts + 1
        control["last_revision_summary"] = _safe_str(summary, 300)
        control["stop_revising"] = False
        _write_artifact(ctx.work_dir, _REVISION_CONTROL_FILENAME, control)
        # Invalidate audit — run_citation_audit must be called again after every revision
        (ctx.work_dir / _AUDIT_STALE_MARKER).write_text("1", encoding="utf-8")

        return json.dumps({
            "status": "ok",
            "word_count": wc,
            "flags_resolved": len(resolved_list),
            "flags_remaining": remaining_flags,
            "revision_index": rh.total_revisions - 1,
            "last_valid_candidate": last_valid,
            "revision_attempts": attempts + 1,
            "max_local_revision_attempts": max_attempts,
            "audit_stale": True,
            "note": "Citation audit invalidated. Call run_citation_audit before validate_authoring_package.",
            "artifacts": ["SECTION_DRAFT_EN.md", "SECTION_REVISION_HISTORY.json"],
        }, ensure_ascii=False)

    return submit_revision


# ---------------------------------------------------------------------------
# 10. submit_visual_placement
# ---------------------------------------------------------------------------

def _is_decodable_image(path: Path) -> bool:
    """Return whether a local visual is a readable image artifact."""
    try:
        from PIL import Image

        if not path.is_file():
            return False
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


_LAST_VALID_SECTION_POINTER = "LAST_VALID_SECTION_POINTER.json"
_LAST_VALID_SECTION_STATE = "LAST_VALID_SECTION_STATE.json"
_LAST_VALID_SECTION_DRAFT = "LAST_VALID_SECTION_DRAFT_EN.md"
_LAST_VALID_SECTION_FILES = (
    "SECTION_AUTHORING_CONTEXT.json",
    "SECTION_ARGUMENT_PLAN.json",
    "SECTION_EVIDENCE_PACKET.json",
    "SECTION_DRAFT_EN.md",
    "SECTION_CITATION_MAP.json",
    "SECTION_AUTHORING_AUDIT.json",
    "SECTION_AUTHORING_PACKAGE.json",
    "SECTION_REVISION_HISTORY.json",
    "SECTION_REVISION_CONTROL.json",
)


def _persist_last_valid_section_candidate(
    ctx: SectionAuthoringContext,
    *,
    summary: str = "",
    validation_level: str = "syntax",
) -> dict[str, Any]:
    """Atomically preserve the latest minimally valid authoring candidate.

    The active section files remain the live workspace.  A separately named
    candidate bundle survives runtime archival and lets the orchestrator
    restore the last non-empty draft after a later model failure.
    """

    draft_path = ctx.work_dir / "SECTION_DRAFT_EN.md"
    if not draft_path.is_file():
        return {"saved": False, "reason": "draft_missing"}
    draft = draft_path.read_text(encoding="utf-8").strip()
    if not draft or _word_count(draft) < 50 or _detect_cjk(draft):
        return {"saved": False, "reason": "draft_not_minimally_valid"}
    pointer = _read_artifact(ctx.work_dir, _LAST_VALID_SECTION_POINTER) or {}
    try:
        previous = int(pointer.get("candidate_index", -1))
    except (TypeError, ValueError):
        previous = -1
    candidate_index = previous + 1
    root = ctx.work_dir / "_last_valid"
    root.mkdir(parents=True, exist_ok=True)
    candidate_dir = root / f"candidate-{candidate_index:04d}"
    candidate_dir.mkdir(parents=False, exist_ok=False)
    copied: list[str] = []
    for name in _LAST_VALID_SECTION_FILES:
        source = ctx.work_dir / name
        if not source.is_file():
            continue
        target = candidate_dir / name
        if source.suffix.lower() == ".json":
            payload = _read_artifact(ctx.work_dir, name)
            if isinstance(payload, dict):
                _write_artifact(candidate_dir, name, payload)
            else:
                atomic_write_text(target, source.read_text(encoding="utf-8"))
        else:
            atomic_write_text(target, source.read_text(encoding="utf-8"))
        copied.append(name)
    manifest = {
        "schema_version": "r4.last_valid_section_candidate.v1",
        "section_id": ctx.section_id,
        "candidate_index": candidate_index,
        "validation_level": validation_level,
        "summary": _safe_str(summary, 300),
        "word_count": _word_count(draft),
        "artifacts": copied,
        "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "created_at": _NOW(),
    }
    _write_artifact(candidate_dir, "CANDIDATE_MANIFEST.json", manifest)
    pointer = {
        **manifest,
        "candidate_dir": str(candidate_dir.relative_to(ctx.work_dir)),
        "pointer_path": _LAST_VALID_SECTION_POINTER,
    }
    # The pointer is the commit record.  Readers either see the old complete
    # pointer or the new complete pointer, never a partially written bundle.
    _write_artifact(ctx.work_dir, _LAST_VALID_SECTION_POINTER, pointer)
    atomic_write_text(ctx.work_dir / _LAST_VALID_SECTION_DRAFT, draft)
    _write_artifact(ctx.work_dir, _LAST_VALID_SECTION_STATE, pointer)
    return {
        "saved": True,
        "candidate_index": candidate_index,
        "candidate_dir": str(candidate_dir),
        "validation_level": validation_level,
        "word_count": _word_count(draft),
    }


def _restore_last_valid_section_candidate(work_dir: Path) -> bool:
    """Restore the pointer-selected candidate after runtime archival."""

    pointer = _read_artifact(work_dir, _LAST_VALID_SECTION_POINTER) or {}
    raw_dir = str(pointer.get("candidate_dir") or "").strip()
    if not raw_dir:
        return False
    candidate_dir = (work_dir / raw_dir).resolve()
    try:
        candidate_dir.relative_to(work_dir.resolve())
    except ValueError:
        return False
    if not candidate_dir.is_dir():
        return False
    restored = False
    for name in _LAST_VALID_SECTION_FILES:
        source = candidate_dir / name
        if not source.is_file():
            continue
        target = work_dir / name
        if source.suffix.lower() == ".json":
            payload = _read_artifact(candidate_dir, name)
            if isinstance(payload, dict):
                _write_artifact(work_dir, name, payload)
        else:
            atomic_write_text(target, source.read_text(encoding="utf-8"))
        restored = True
    if restored:
        atomic_write_text(
            work_dir / _LAST_VALID_SECTION_DRAFT,
            (candidate_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8")
            if (candidate_dir / "SECTION_DRAFT_EN.md").is_file()
            else "",
        )
        _write_artifact(
            work_dir,
            "LAST_VALID_SECTION_RESTORE.json",
            {
                "schema_version": "r4.last_valid_section_restore.v1",
                "candidate_index": pointer.get("candidate_index", -1),
                "restored_at": _NOW(),
            },
        )
    return restored


def _make_submit_visual_placement(ctx: SectionAuthoringContext):
    def submit_visual_placement(placements_json: str) -> str:
        """Place only canonical, reviewed, relevant, decodable visual assets.

        Use a JSON list containing exact visual_chunk_id values returned by
        inspect_visual_assets. If that tool returned no placement-eligible
        visuals, skip this tool; a figure is optional. Never submit an object
        with an empty visual_chunk_id. An explicit empty list is accepted only
        to record that no eligible visual was placed.
        """
        try:
            raw = json.loads(placements_json)
            if isinstance(raw, dict):
                raw = [raw]
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"Invalid JSON: {exc}"})
        if not isinstance(raw, list):
            return json.dumps({"status": "error", "error": "placements_json must be a list or object"})
        graph = _build_asset_graph(ctx)
        if graph.is_empty:
            return json.dumps({
                "status": "rejected",
                "error": "Canonical Phase-2 asset graph is empty; no visual placement was written.",
            })

        placements: List[VisualPlacement] = []
        rejected: List[Dict[str, Any]] = []
        for index, raw_placement in enumerate(raw):
            if not isinstance(raw_placement, dict):
                rejected.append({"index": index, "reason": "placement must be an object"})
                continue
            visual_id = _safe_str(raw_placement.get("visual_chunk_id", ""), 128)
            paper_id = _safe_str(raw_placement.get("paper_id", ""), 128)
            requested_status = _safe_str(raw_placement.get("asset_status", ""), 60)
            requested_path = _safe_str(raw_placement.get("local_image_path", ""), 500)
            visual = graph.visuals.get(visual_id)
            reasons: List[str] = []
            if visual is None:
                reasons.append(f"unknown visual_chunk_id: {visual_id}")
            else:
                if visual.paper_id != paper_id:
                    reasons.append(
                        f"paper/visual ownership mismatch: {visual_id} belongs to {visual.paper_id}, not {paper_id}"
                    )
                if not visual.accepted:
                    reasons.append(
                        f"visual status/kind is not eligible: status={visual.status}, kind={visual.kind}"
                    )
                if not visual.relevant_or_reranked:
                    reasons.append("visual was not a relevant or reranked Phase-2 candidate")
                canonical_path = Path(visual.local_image_path) if visual.local_image_path else Path()
                if not visual.local_image_path or not _is_decodable_image(canonical_path):
                    reasons.append("canonical local_image_path is missing or not a decodable image")
                if requested_path:
                    try:
                        if Path(requested_path).resolve() != canonical_path.resolve():
                            reasons.append("caller local_image_path does not match the canonical visual path")
                    except Exception:
                        reasons.append("caller local_image_path is invalid")
            if requested_status == "approved_ai_conceptual_schematic":
                reasons.append("AI conceptual art cannot be self-approved; a reviewed canonical artifact is required")
            elif requested_status and requested_status not in {"verified_local", "auto"}:
                reasons.append(f"unsupported caller asset_status: {requested_status}")
            if reasons:
                rejected.append({"index": index, "visual_chunk_id": visual_id, "reasons": reasons})
                continue
            assert visual is not None
            placements.append(VisualPlacement(
                visual_chunk_id=visual.visual_id,
                paper_id=visual.paper_id,
                caption=visual.caption,
                placement_after_paragraph=int(raw_placement.get("placement_after_paragraph", 0)),
                argument_type=visual.argument_type,
                argument_claim=visual.argument_claim,
                asset_status="verified_local",
                local_image_path=visual.local_image_path,
                placement_rationale=_safe_str(raw_placement.get("placement_rationale", ""), 300),
            ))

        if rejected:
            return json.dumps({
                "status": "rejected",
                "error": "Visual placement failed canonical validation; no artifact was written.",
                "rejected_items": rejected,
            }, ensure_ascii=False)
        placement = SectionVisualPlacement(
            section_id=ctx.section_id,
            placements=placements,
            total_placed=len(placements),
            missing_visuals=0,
            created_at=_NOW(),
        )
        _write_artifact(ctx.work_dir, "SECTION_VISUAL_PLACEMENT.json", placement)
        return json.dumps({
            "status": "ok", "total_placed": len(placements), "missing_visuals": 0,
            "artifact": "SECTION_VISUAL_PLACEMENT.json",
        }, ensure_ascii=False)

    return submit_visual_placement


# ---------------------------------------------------------------------------
# 11. submit_section_handoff_card
# ---------------------------------------------------------------------------

def _make_submit_section_handoff_card(ctx: SectionAuthoringContext):
    def submit_section_handoff_card(handoff_json: str) -> str:
        """Submit compact English cross-section memory for this section.

        The model supplies synthesis judgments. Provenance identifiers are
        recomputed from the validated citation and visual artifacts.
        """

        try:
            raw = json.loads(handoff_json)
        except Exception as exc:
            return json.dumps(
                {"status": "error", "error": f"invalid_json: {exc}"},
                ensure_ascii=True,
            )
        if not isinstance(raw, dict):
            return json.dumps(
                {"status": "error", "error": "handoff must be an object"},
                ensure_ascii=True,
            )
        text_fields = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "section_id",
                "section_title",
                "used_paper_ids",
                "used_chunk_ids",
                "section_argument_completed",
            }
        }
        if _detect_cjk(json.dumps(text_fields, ensure_ascii=False)):
            return json.dumps(
                {"status": "error", "error": "handoff text must be English"},
                ensure_ascii=True,
            )

        citation_map = (
            _read_artifact(ctx.work_dir, "SECTION_CITATION_MAP.json") or {}
        )
        citations = citation_map.get("citations", [])
        paper_ids = sorted(
            {
                str(paper_id)
                for citation in citations
                if isinstance(citation, dict)
                for paper_id in citation.get("paper_ids", [])
                if str(paper_id).strip()
            }
        )
        chunk_ids = sorted(
            {
                str(chunk_id)
                for citation in citations
                if isinstance(citation, dict)
                for chunk_id in citation.get("chunk_ids", [])
                if str(chunk_id).strip()
            }
        )
        placement = (
            _read_artifact(ctx.work_dir, "SECTION_VISUAL_PLACEMENT.json") or {}
        )
        valid_visuals = {
            str(item.get("visual_chunk_id"))
            for item in placement.get("placements", [])
            if isinstance(item, dict)
            and str(item.get("visual_chunk_id") or "").strip()
            and str(item.get("asset_status") or "") == "verified_local"
        }
        visual_takeaways = []
        for item in raw.get("visual_takeaways", []):
            if not isinstance(item, dict):
                continue
            visual_id = str(item.get("visual_chunk_id") or "").strip()
            function = str(
                item.get("argumentative_function") or ""
            ).strip()
            if visual_id in valid_visuals and function:
                visual_takeaways.append(
                    {
                        "visual_chunk_id": visual_id,
                        "argumentative_function": function,
                    }
                )

        payload = {
            "schema_version": "section_handoff_card.v1",
            "section_id": ctx.section_id,
            "section_title": ctx.section_title,
            "section_argument_completed": bool(
                raw.get("section_argument_completed", True)
            ),
            "established_takeaways": raw.get("established_takeaways", []),
            "conditional_judgments": raw.get(
                "conditional_judgments", []
            ),
            "unresolved_tensions": raw.get("unresolved_tensions", []),
            "terms_defined": raw.get("terms_defined", []),
            "avoid_repeating": raw.get("avoid_repeating", []),
            "forward_question": str(raw.get("forward_question") or ""),
            "why_next_section_is_needed": str(
                raw.get("why_next_section_is_needed") or ""
            ),
            "visual_takeaways": visual_takeaways,
            "used_paper_ids": paper_ids,
            "used_chunk_ids": chunk_ids,
        }
        try:
            card = SectionHandoffCard.model_validate(payload)
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"invalid_handoff_contract: {exc}",
                },
                ensure_ascii=True,
            )
        if not card.established_takeaways:
            return json.dumps(
                {
                    "status": "error",
                    "error": "at least one established_takeaway is required",
                },
                ensure_ascii=True,
            )
        _write_artifact(ctx.work_dir, "SECTION_HANDOFF_CARD.json", card)
        return json.dumps(
            {
                "status": "ok",
                "artifact": "SECTION_HANDOFF_CARD.json",
                "established_takeaway_count": len(
                    card.established_takeaways
                ),
                "conditional_judgment_count": len(
                    card.conditional_judgments
                ),
                "unresolved_tension_count": len(card.unresolved_tensions),
                "verified_paper_count": len(card.used_paper_ids),
                "verified_chunk_count": len(card.used_chunk_ids),
                "verified_visual_count": len(card.visual_takeaways),
            },
            ensure_ascii=True,
        )

    return submit_section_handoff_card


# ---------------------------------------------------------------------------
# 12. request_more_literature
# ---------------------------------------------------------------------------

def _make_request_more_literature(ctx: SectionAuthoringContext):
    def request_more_literature(feedback_json: str) -> str:
        """Signal that authoring cannot proceed without additional literature.

        Writes SECTION_COVERAGE_FEEDBACK.json. Call this only when blocking
        evidence gaps make it impossible to write certain required claims.

        Args:
            feedback_json: JSON with:
                - feedback_items: list of {role, severity, description,
                    blocking_claims (list), suggested_queries (list)}
                - authoring_can_proceed: bool — whether a partial draft is possible
        """
        try:
            data = json.loads(feedback_json)
        except Exception as exc:
            return json.dumps({"status": "error", "error": f"Invalid JSON: {exc}"})

        raw_items = data.get("feedback_items", [])
        items = []
        blocking_count = 0
        for fi in raw_items:
            if not isinstance(fi, dict):
                continue
            sev = _safe_str(fi.get("severity", "important"), 20)
            if sev == "blocking":
                blocking_count += 1
            items.append(CoverageFeedbackItem(
                role=_safe_str(fi.get("role", ""), 60),
                severity=sev,
                description=_safe_str(fi.get("description", ""), 400),
                blocking_claims=list(fi.get("blocking_claims", []))[:10],
                suggested_queries=list(fi.get("suggested_queries", []))[:6],
            ))

        can_proceed = bool(data.get("authoring_can_proceed", False))
        graph = _build_asset_graph(ctx)
        required_sources, available_sources = _synthesis_source_requirement(
            ctx,
            graph,
        )
        loaded_context = (
            _read_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json")
            or {}
        )
        upstream_declares_blocking = bool(
            loaded_context.get(
                "blocking_gaps_remain",
                ctx.section_data.get("blocking_gaps_remain", False),
            )
        )
        coverage_status = str(
            loaded_context.get(
                "coverage_status",
                ctx.section_data.get("coverage_status", ""),
            )
            or ""
        ).lower()
        supplied_package_is_usable = (
            not upstream_declares_blocking
            and required_sources > 0
            and available_sources >= required_sources
            and coverage_status not in {"failed", "empty", "not_run"}
        )
        if (
            not can_proceed
            and supplied_package_is_usable
        ):
            # The author may not turn "I have not inspected enough of the
            # supplied package" into a new retrieval request.  When Phase 2
            # has already certified a plural source pool, adapt the section
            # around supported mechanisms, comparisons, and boundaries.
            stale_feedback = ctx.work_dir / "SECTION_COVERAGE_FEEDBACK.json"
            if stale_feedback.exists():
                stale_feedback.unlink()
            return json.dumps({
                "status": "rejected",
                "reason": (
                    "The supplied package is sufficient for bounded authoring and contains "
                    f"{available_sources} usable papers (minimum required: "
                    f"{required_sources}). Do not block the section because an "
                    "ideal parameter table or exact sentence is absent."
                ),
                "required_action": (
                    "Call inspect_material_package, retrieve additional valid "
                    "chunk_ids from the supplied package, and write a bounded "
                    "mechanism/route comparison. Record any unresolved exact "
                    "parameter as a non-blocking limitation."
                ),
                "authoring_can_proceed": True,
            }, ensure_ascii=False)

        feedback = SectionCoverageFeedback(
            section_id=ctx.section_id,
            state="needs_more_literature",
            feedback_items=items,
            total_blocking=blocking_count,
            authoring_can_proceed=can_proceed,
            created_at=_NOW(),
        )
        _write_artifact(ctx.work_dir, "SECTION_COVERAGE_FEEDBACK.json", feedback)

        return json.dumps({
            "status": "ok",
            "state": "needs_more_literature",
            "total_blocking": blocking_count,
            "authoring_can_proceed": can_proceed,
            "artifact": "SECTION_COVERAGE_FEEDBACK.json",
        }, ensure_ascii=False)

    return request_more_literature


# ---------------------------------------------------------------------------
# 12. validate_authoring_package
# ---------------------------------------------------------------------------

# These helpers are intentionally unconditional module-level definitions.  The
# provider's cold-runtime finalizer is invoked after a worker tool call and may
# be the first code path to validate a persisted package in a fresh process.
def _section_contract_errors(ctx: SectionAuthoringContext, draft_text: str) -> List[str]:
    contract = dict(ctx.section_data.get("section_contract") or {})
    errors: List[str] = []
    words = _word_count(draft_text)
    paragraphs = _paragraph_count(draft_text)
    try:
        word_budget = int(
            contract.get("word_budget")
            or contract.get("target_word_budget")
            or ctx.section_data.get("estimated_word_budget")
            or 0
        )
    except (TypeError, ValueError):
        word_budget = 0
    if word_budget:
        runaway_ceiling = int(word_budget * 4)
        if words < 50:
            errors.append(
                f"draft is too short ({words} words); minimum is 50"
            )
        elif words > runaway_ceiling:
            errors.append(
                f"draft exceeds the broad safety ceiling of {runaway_ceiling} words "
                f"(preferred target={word_budget}); draft has {words}"
            )
    elif words < 50:
        errors.append(f"draft is too short ({words} words); minimum is 50")
    paragraph_functions = list(contract.get("paragraph_functions") or [])
    if paragraph_functions:
        minimum = 1 if len(paragraph_functions) == 1 else max(2, len(paragraph_functions) - 1)
        if paragraphs < minimum:
            errors.append(
                f"section paragraph contract requires at least {minimum} paragraphs from "
                f"{len(paragraph_functions)} planned functions; draft has {paragraphs}"
            )
    return errors


def _visual_artifact_errors(
    ctx: SectionAuthoringContext,
    graph: CanonicalAssetGraph,
) -> List[str]:
    data = _read_artifact(ctx.work_dir, "SECTION_VISUAL_PLACEMENT.json")
    if not data:
        return []
    errors: List[str] = []
    for index, placement in enumerate(data.get("placements", [])):
        visual_id = str(placement.get("visual_chunk_id") or "")
        visual = graph.visuals.get(visual_id)
        if visual is None:
            errors.append(f"visual placement[{index}] has unknown visual ID {visual_id}")
            continue
        if visual.paper_id != str(placement.get("paper_id") or ""):
            errors.append(f"visual placement[{index}] paper ownership mismatch")
        if not visual.accepted or not visual.relevant_or_reranked:
            errors.append(f"visual placement[{index}] is no longer accepted/relevant")
        if str(placement.get("local_image_path") or "") != visual.local_image_path:
            errors.append(f"visual placement[{index}] path differs from canonical graph")
        if not visual.local_image_path or not _is_decodable_image(Path(visual.local_image_path)):
            errors.append(f"visual placement[{index}] canonical image is not decodable")
    return errors


def _write_needs_more_literature_package(ctx: SectionAuthoringContext) -> str:
    feedback = _read_artifact(ctx.work_dir, "SECTION_COVERAGE_FEEDBACK.json") or {}
    context_data = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json") or {}
    artifacts = {
        "SECTION_AUTHORING_CONTEXT": "SECTION_AUTHORING_CONTEXT.json",
        "SECTION_COVERAGE_FEEDBACK": "SECTION_COVERAGE_FEEDBACK.json",
    }
    package = SectionAuthoringPackage(
        section_id=ctx.section_id,
        section_title=ctx.section_title,
        chapter_argument=ctx.chapter_argument,
        authoring_status="needs_more_literature",
        artifacts=artifacts,
        total_flags=int(feedback.get("total_blocking", 0)),
        blocking_flags=int(feedback.get("total_blocking", 0)),
        created_at=_NOW(),
    )
    _write_artifact(ctx.work_dir, "SECTION_AUTHORING_PACKAGE.json", package)
    return (
        "VALIDATION_PASSED: Authoring stopped with an audited insufficient-material outcome. "
        f"status=needs_more_literature, coverage_status={context_data.get('coverage_status', 'unknown')}, "
        f"blocking_feedback={feedback.get('total_blocking', 0)}."
    )


def _make_validate_authoring_package(ctx: SectionAuthoringContext):
    def validate_authoring_package() -> str:
        """Recompute Phase-2 provenance, citation support, visual, and section contracts."""
        feedback = _read_artifact(ctx.work_dir, "SECTION_COVERAGE_FEEDBACK.json") or {}
        context_data = _read_artifact(ctx.work_dir, "SECTION_AUTHORING_CONTEXT.json") or {}
        graph = _build_asset_graph(ctx)
        required_sources, available_sources = _synthesis_source_requirement(
            ctx,
            graph,
        )
        certified_sufficient = (
            not bool(context_data.get("blocking_gaps_remain", False))
            and required_sources > 0
            and available_sources >= required_sources
            and str(context_data.get("coverage_status") or "").lower()
            not in {"failed", "empty", "not_run"}
        )
        if (
            feedback.get("state") == "needs_more_literature"
            and not bool(feedback.get("authoring_can_proceed", False))
            and not certified_sufficient
            and (
                bool(context_data.get("blocking_gaps_remain", False))
                or int(feedback.get("total_blocking", 0) or 0) > 0
            )
        ):
            return _write_needs_more_literature_package(ctx)

        required = [
            "SECTION_AUTHORING_CONTEXT.json", "SECTION_ARGUMENT_PLAN.json",
            "SECTION_EVIDENCE_PACKET.json", "SECTION_CITATION_MAP.json",
            "SECTION_AUTHORING_AUDIT.json", "SECTION_DRAFT_EN.md",
        ]
        missing = [name for name in required if not (ctx.work_dir / name).exists()]
        if missing:
            return f"VALIDATION_FAILED: Missing required artifacts: {missing}."
        if (ctx.work_dir / _AUDIT_STALE_MARKER).exists():
            return "VALIDATION_FAILED: Citation audit is stale after the most recent draft/revision."

        draft_text = (ctx.work_dir / "SECTION_DRAFT_EN.md").read_text(encoding="utf-8").strip()
        if not draft_text:
            return "VALIDATION_FAILED: SECTION_DRAFT_EN.md is empty."
        errors: List[str] = []
        if _detect_cjk(draft_text):
            errors.append("draft contains CJK characters; section prose must be English")
        topic_identity = (
            context_data.get("topic_identity")
            if isinstance(context_data.get("topic_identity"), dict)
            else ctx.topic_identity
        )
        if isinstance(topic_identity, dict) and topic_identity.get("valid"):
            topic_alignment = assess_topic_alignment(
                draft_text,
                topic_identity,
                strict=False,
            )
            _write_artifact(
                ctx.work_dir,
                "SECTION_TOPIC_ALIGNMENT.json",
                topic_alignment,
            )
            if topic_alignment.get("status") != "passed":
                errors.append(
                    "draft does not preserve the confirmed scientific object"
                )
        errors.extend(_section_contract_errors(ctx, draft_text))

        graph_error = _graph_ready_error(graph)
        if graph_error:
            errors.append(graph_error)
        plan_data = _read_artifact(ctx.work_dir, "SECTION_ARGUMENT_PLAN.json") or {}
        errors.extend(_validate_argument_plan_data(ctx, graph, plan_data.get("paragraphs", [])))
        evidence_data = _read_artifact(ctx.work_dir, "SECTION_EVIDENCE_PACKET.json") or {}
        evidence_errors, _ = _validate_evidence_items(
            ctx, graph, evidence_data.get("items", []), evidence_data.get("uncovered_claim_ids", [])
        )
        errors.extend(evidence_errors)
        errors.extend(_visual_artifact_errors(ctx, graph))

        stored_map = _read_artifact(ctx.work_dir, "SECTION_CITATION_MAP.json") or {}
        recomputed = _compute_citation_audit(ctx, stored_map.get("citations", []), persist=True)
        if recomputed.get("status") != "ok":
            errors.append(str(recomputed.get("error") or "citation audit recomputation failed"))
        elif int(recomputed.get("blocking_flags", 0)):
            errors.extend(
                str(item.get("reason") or item.get("type"))
                for item in recomputed.get("flags_detail", [])
                if item.get("severity") == "blocking"
            )
        control = _read_revision_control(ctx)
        if errors and _has_durable_section_candidate(ctx.work_dir) and control.get("stop_revising"):
            return _write_awaiting_human_review_package(
                ctx,
                reason=str(
                    control.get("stop_reason")
                    or "bounded_revision_convergence_reached"
                ),
                control=control,
            )
        if errors:
            return "VALIDATION_FAILED: " + " | ".join(dict.fromkeys(errors))

        citation_map = recomputed["citation_map"]
        audit = recomputed["audit"]
        artifacts: Dict[str, str] = {
            "SECTION_AUTHORING_CONTEXT": "SECTION_AUTHORING_CONTEXT.json",
            "SECTION_ARGUMENT_PLAN": "SECTION_ARGUMENT_PLAN.json",
            "SECTION_EVIDENCE_PACKET": "SECTION_EVIDENCE_PACKET.json",
            "SECTION_DRAFT_EN": "SECTION_DRAFT_EN.md",
            "SECTION_CITATION_MAP": "SECTION_CITATION_MAP.json",
            "SECTION_AUTHORING_AUDIT": "SECTION_AUTHORING_AUDIT.json",
        }
        for key, filename in (
            ("SECTION_VISUAL_PLACEMENT", "SECTION_VISUAL_PLACEMENT.json"),
            ("SECTION_REVISION_HISTORY", "SECTION_REVISION_HISTORY.json"),
            ("SECTION_ARGUMENT_PLAN_HISTORY", "SECTION_ARGUMENT_PLAN_HISTORY.json"),
            ("SECTION_EVIDENCE_PACKET_HISTORY", "SECTION_EVIDENCE_PACKET_HISTORY.json"),
            ("SECTION_COVERAGE_FEEDBACK", "SECTION_COVERAGE_FEEDBACK.json"),
            ("SECTION_HANDOFF_CARD", "SECTION_HANDOFF_CARD.json"),
        ):
            if (ctx.work_dir / filename).exists():
                artifacts[key] = filename
        package = SectionAuthoringPackage(
            section_id=ctx.section_id, section_title=ctx.section_title,
            chapter_argument=ctx.chapter_argument, authoring_status="completed",
            word_count=_word_count(draft_text), paragraph_count=_paragraph_count(draft_text),
            cited_sentences=int(citation_map.get("total_cited_sentences", 0)),
            total_flags=int(audit.get("total_flags", 0)),
            blocking_flags=int(audit.get("total_blocking_flags", 0)),
            papers_cited=list(citation_map.get("papers_cited", [])),
            artifacts=artifacts, created_at=_NOW(),
        )
        _write_artifact(ctx.work_dir, "SECTION_AUTHORING_PACKAGE.json", package)
        return (
            "VALIDATION_PASSED: Section authoring package complete. "
            f"status=completed, words={package.word_count}, cited_sentences={package.cited_sentences}, "
            f"papers_cited={len(package.papers_cited)}, flags={package.total_flags}."
        )

    return validate_authoring_package


# ---------------------------------------------------------------------------
# Provider class — assembles all 12 tools
# ---------------------------------------------------------------------------

SECTION_AUTHORING_TOOL_NAMES = [
    "load_authoring_context",
    "inspect_material_package",
    "retrieve_chunk_text",
    "inspect_visual_assets",
    "submit_argument_plan",
    "build_evidence_packet",
    "submit_section_draft",
    "run_citation_audit",
    "submit_revision",
    "submit_visual_placement",
    "submit_section_handoff_card",
    "request_more_literature",
    "validate_authoring_package",
]


class SectionAuthoringToolProvider(ToolProvider):
    """Builds all 12 section-authoring FunctionTools bound to a SectionAuthoringContext."""

    def __init__(self, ctx: SectionAuthoringContext) -> None:
        self._ctx = ctx

    def get_tools(self, work_dir: Path) -> list:
        ctx = self._ctx
        return [
            FunctionTool(_make_load_authoring_context(ctx)),
            FunctionTool(_make_inspect_material_package(ctx)),
            FunctionTool(_make_retrieve_chunk_text(ctx)),
            FunctionTool(_make_inspect_visual_assets(ctx)),
            FunctionTool(_make_submit_argument_plan(ctx)),
            FunctionTool(_make_build_evidence_packet(ctx)),
            FunctionTool(_make_submit_section_draft(ctx)),
            FunctionTool(_make_run_citation_audit(ctx)),
            FunctionTool(_make_submit_revision(ctx)),
            FunctionTool(_make_submit_visual_placement(ctx)),
            FunctionTool(_make_submit_section_handoff_card(ctx)),
            FunctionTool(_make_request_more_literature(ctx)),
            FunctionTool(_make_validate_authoring_package(ctx)),
        ]

    def get_allowed_tool_names(self) -> List[str]:
        return list(SECTION_AUTHORING_TOOL_NAMES)

    def try_auto_finalize(self) -> Optional[str]:
        """Re-audit the latest draft and finalize objective success.

        A revision deliberately invalidates its predecessor's citation audit.
        Leaving the model responsible for remembering one final audit call
        caused already-acceptable chapters to enter repeated rewrite loops.
        The provider therefore rebuilds the citation map from the current
        ``[REF:*]`` markers whenever the stale sentinel is present, then lets
        the deterministic package validator decide whether work is complete.
        """

        required = (
            "SECTION_AUTHORING_CONTEXT.json",
            "SECTION_ARGUMENT_PLAN.json",
            "SECTION_EVIDENCE_PACKET.json",
            "SECTION_DRAFT_EN.md",
        )
        if not all((self._ctx.work_dir / name).exists() for name in required):
            return None
        if (self._ctx.work_dir / _AUDIT_STALE_MARKER).exists():
            audit = _compute_citation_audit(
                self._ctx,
                [],
                persist=True,
            )
            if audit.get("status") != "ok":
                return None
        if not all(
            (self._ctx.work_dir / name).exists()
            for name in (
                "SECTION_CITATION_MAP.json",
                "SECTION_AUTHORING_AUDIT.json",
            )
        ):
            return None
        return _make_validate_authoring_package(self._ctx)()


def build_section_authoring_toolkit(ctx: SectionAuthoringContext) -> tuple:
    """Convenience: return (tools_list, tool_name_list) for a SectionAuthoringContext."""
    provider = SectionAuthoringToolProvider(ctx)
    tools = provider.get_tools(ctx.work_dir)
    return tools, provider.get_allowed_tool_names()


def backfill_authoring_package_stats(work_dir: Path, result: Any) -> None:
    """Backfill execution stats into SECTION_AUTHORING_PACKAGE.json after worker.run() returns.

    Called after ResearchWorker.run() so the manifest fields (run_id, wall_time_seconds,
    total_input_tokens, estimated_cost_usd) are available. The package is written during
    the tool call before the worker exits, so stats are not yet known at write time.
    """
    pkg_path = work_dir / "SECTION_AUTHORING_PACKAGE.json"
    if not pkg_path.exists():
        return
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        pkg["run_id"] = getattr(result, "run_id", "") or ""
        pkg["task_id"] = getattr(result, "task_id", "") or ""
        pkg["wall_time_seconds"] = getattr(result, "wall_time_seconds", 0.0) or 0.0
        pkg["total_input_tokens"] = (
            (getattr(result, "total_input_tokens", 0) or 0)
            + (getattr(result, "total_output_tokens", 0) or 0)
        )
        pkg["estimated_cost_cny"] = getattr(result, "estimated_cost_cny", 0.0) or 0.0
        pkg["estimated_cost_usd"] = getattr(result, "estimated_cost_usd", 0.0) or 0.0
        atomic_write_json(pkg_path, pkg)
    except Exception:
        logger.exception("backfill_authoring_package_stats: failed to update %s", pkg_path)
