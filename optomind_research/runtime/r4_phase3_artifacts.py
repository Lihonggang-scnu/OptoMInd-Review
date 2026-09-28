"""R4 bridge for Phase-3 argument and material artifacts.

This module is deliberately deterministic.  It does not discover papers, call
an LLM, or decide whether a scientific statement is true.  It translates the
audited Phase-3 artifacts into the compact authoring contract needed by R4:
claims, relations, visual candidates, and a per-claim judgment ledger.

The bridge exists because Phase-3 writes several useful artifacts (often one
plural JSON file per run), while the legacy authoring worker expects one
section-local context.  Keeping this normalization in one place prevents the
writer from reconstructing scientific status by guessing across files.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .r3_production_handoff import (
    R3_HANDOFF_FILENAME,
    R3HandoffValidationError,
    R3ProductionHandoff,
    R3ValidationReport,
    migrate_legacy_phase3_artifacts,
    read_r3_production_handoff,
)


def _read_json(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [value]
    return []


def _text(value: Any) -> str:
    """Normalize one legacy scalar without inventing an identifier."""

    return str(value or "").strip()


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip()
        for value in values
        if value is not None and str(value).strip()
    ))


def _resolve_existing(raw: Any, root: Path) -> Optional[Path]:
    """Resolve paths emitted by Phase-3 without assuming one cwd.

    Phase-3 manifests historically used absolute paths, project-relative paths,
    and paths relative to the run directory.  We accept all three, but only
    return an existing path; a missing reference stays an explicit diagnostic.
    """

    if not raw:
        return None
    candidate = Path(str(raw))
    options = [candidate]
    if not candidate.is_absolute():
        options.extend([root / candidate, Path.cwd() / candidate])
        project_root = Path(__file__).resolve().parents[2]
        options.append(project_root / candidate)
    for item in options:
        try:
            if item.exists():
                return item.resolve()
        except OSError:
            continue
    return None


def _claim_values(raw: Any) -> list[dict[str, Any]]:
    """Normalize list/map claim containers used by Phase-3 revisions."""

    if isinstance(raw, dict):
        if isinstance(raw.get("claims"), (dict, list)):
            return _claim_values(raw.get("claims"))
        values: list[dict[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("claim_id", str(key))
                values.append(item)
        return values
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _section_value(container: Any, section_id: str) -> dict[str, Any]:
    if isinstance(container, dict):
        value = container.get(section_id)
        return dict(value) if isinstance(value, dict) else {}
    for item in _as_list(container):
        if isinstance(item, dict) and str(item.get("section_id") or "") == section_id:
            return dict(item)
    return {}


_INTEGRITY_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "their", "this",
    "to", "with", "where", "which", "within", "under", "using", "only",
    "both", "can", "may", "than", "through", "into", "over", "after",
})


def _integrity_tokens(text: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", str(text or "").casefold())
        if token not in _INTEGRITY_STOPWORDS
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def _handoff_text_flags(text: Any) -> list[str]:
    """Detect common parser contamination without judging the science."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    lower = value.casefold()
    flags: list[str] = []
    if not value:
        return ["empty_text"]
    if (
        re.search(r"\barxiv\s*:\s*[a-z-]*\d{4}\.\d{3,5}", lower)
        or "preprint submitted" in lower
        or re.search(r"\b(?:phys\.?\s*rev\.?|j\.?\s*phys\.?|nature\s+photonic)", lower)
        and len(re.findall(r"\b(?:19|20)\d{2}\b", lower)) >= 1
    ):
        flags.append("arxiv_or_venue_header")
    if (
        re.search(r"(?:^|\s)(?:fig(?:ure)?|table|contents|chapter)\s*\.?\s*\d*\b", lower)
        or "table of contents" in lower
        or re.search(r"\b(?:figure|fig\.|table)\s+\d+\s*[:.-]", lower)
    ):
        flags.append("caption_or_table_of_contents")
    doi_like = len(re.findall(r"\b10\.\d{4,9}/\S+", lower))
    venue_like = len(re.findall(
        r"\b(?:phys\.?\s*rev|phys\.?\s*lett|j\.?\s*chem|"
        r"optics?\s*(?:express|letters)|nature\s*(?:photonics?|communications?)|"
        r"science\s+advances?)\b",
        lower,
    ))
    author_like = len(re.findall(r"\b(?:et al\.?|eds?\.?|doi|issn|vol\.?|pp?\.?\s*\d)", lower))
    # Citation-entry detection uses the original case and requires a real
    # entry boundary plus bibliographic structure: a capitalized surname
    # followed by initials, a surname/title/venue sequence, or a surname
    # directly followed by a full venue name.  A lowercase mid-sentence
    # token such as "compact, chip-scale ... nonlinear optics" carries none
    # of those markers and is never judged by a bare topic word.
    citation_entry = bool(re.search(
        r"(?:^|[.;]\s+)"
        r"[A-Z][a-z'-]{2,},\s+"
        r"(?:[A-Z]\.(?:\s*[A-Z]\.)*|et al\.?)",
        value,
    ))
    full_venue_pattern = (
        r"phys\.?\s*rev\.?|phys\.?\s*lett\.?|j\.?\s*chem\.?|"
        r"optics?\s*(?:express|letters)|"
        r"nature\s*(?:photonics?|communications?)|science\s+advances?"
    )
    surname_title_venue = bool(re.search(
        r"(?:^|[.;]\s+)"
        r"[A-Z][a-z'-]{2,},\s+"
        r"[A-Z][^,]{2,},\s+"
        r"(?:" + full_venue_pattern + r")\b",
        value,
        re.I,
    ))
    author_venue_marker = bool(re.search(
        r"(?:^|[.;]\s+)"
        r"[A-Z][a-z'-]{2,},\s+"
        r"(?:" + full_venue_pattern + r")\b",
        value,
        re.I,
    ))
    if (
        "bibliography" in lower
        or lower.startswith("references")
        or (author_like >= 2 and (doi_like or venue_like))
        or (venue_like >= 2 and len(re.findall(r"\b(?:19|20)\d{2}\b", lower)) >= 1)
        or citation_entry
        or surname_title_venue
        or author_venue_marker
    ):
        flags.append("bibliography_like")
    return list(dict.fromkeys(flags))


def _proposition_compatibility_flag(statement: Any, excerpt: Any) -> Optional[str]:
    """Flag an excerpt that is too unrelated to the authored proposition."""

    statement_tokens = _integrity_tokens(statement)
    excerpt_tokens = _integrity_tokens(excerpt)
    if len(statement_tokens) < 4 or len(excerpt_tokens) < 5:
        return None
    overlap = len(statement_tokens & excerpt_tokens) / max(1, min(len(statement_tokens), len(excerpt_tokens)))
    if overlap < 0.06:
        return "proposition_incompatible_bounded_excerpt"
    return None


def _claim_chunk_ids(claim: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize all Phase-3 support aliases into canonical ID families."""

    def collect(names: Iterable[str]) -> list[str]:
        values: list[str] = []
        for name in names:
            values.extend(_as_list(claim.get(name)))
        return _unique_strings(values)

    factual = collect(("factual_support_chunk_ids", "direct_support_chunk_ids"))
    contextual = collect(("contextual_support_chunk_ids", "context_support_chunk_ids"))
    core = collect(("core_chunk_ids", "core_text_chunk_ids"))
    supporting = collect((
        "supporting_text_chunk_ids", "supporting_chunk_ids", "support_chunk_ids",
    ))
    # ``core`` is a ranked authoring candidate view, not proof that a claim
    # was explicitly bound to that chunk.  Only explicit support families may
    # cross the R3 trust boundary.
    all_supporting = _unique_strings([*supporting, *factual, *contextual])
    return {
        "supporting": all_supporting,
        "factual": factual,
        "contextual": contextual,
        "core": core,
    }


def _select_authoring_statement(
    claim: dict[str, Any],
    fallback_claim: Optional[dict[str, Any]] = None,
) -> tuple[str, str, list[str], str]:
    """Choose the authored proposition, keeping rewrites as excerpts only."""

    fallback_claim = fallback_claim or {}
    classification = str(
        claim.get("support_classification")
        or claim.get("claim_classification")
        or ""
    ).casefold().strip()
    if classification == "open_question":
        candidates = [
            ("authoring_statement", claim.get("authoring_statement")),
            ("effective_statement", claim.get("effective_statement")),
            ("statement", claim.get("statement")),
            ("original_statement", claim.get("original_statement")),
            ("graph_statement", fallback_claim.get("statement")),
            ("graph_original_statement", fallback_claim.get("original_statement")),
        ]
    else:
        candidates = [
            ("authoring_statement", claim.get("authoring_statement")),
            ("original_statement", claim.get("original_statement")),
            ("statement", claim.get("statement")),
            ("graph_original_statement", fallback_claim.get("original_statement")),
            ("graph_statement", fallback_claim.get("statement")),
        ]
    first_nonempty: tuple[str, str] | None = None
    for source, raw in candidates:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not value:
            continue
        if first_nonempty is None:
            first_nonempty = (source, value)
        flags = _handoff_text_flags(value)
        if not any(flag in flags for flag in (
            "bibliography_like", "arxiv_or_venue_header", "caption_or_table_of_contents",
        )):
            bounded = str(
                claim.get("bounded_evidence_paraphrase")
                or claim.get("effective_statement")
                or claim.get("supported_rewrite")
                or ""
            ).strip()
            return value, source, flags, bounded
    if first_nonempty is not None:
        source, value = first_nonempty
        return value, source, _handoff_text_flags(value), str(
            claim.get("bounded_evidence_paraphrase")
            or claim.get("effective_statement")
            or claim.get("supported_rewrite")
            or ""
        ).strip()
    return "", "missing", ["empty_text"], ""


def _bounded_evidence_excerpt(claim: dict[str, Any]) -> str:
    """Return a legacy evidence rewrite only as a separately named excerpt."""

    return re.sub(r"\s+", " ", str(
        claim.get("bounded_evidence_paraphrase")
        or claim.get("effective_statement")
        or claim.get("supported_rewrite")
        or ""
    )).strip()


def _claim_paper_ids(claim: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize paper-id aliases without promoting discovery records."""

    def collect(names: Iterable[str]) -> list[str]:
        values: list[str] = []
        for name in names:
            values.extend(_as_list(claim.get(name)))
        return _unique_strings(values)

    core = collect(("core_paper_ids", "core_papers"))
    cited = collect(("citation_paper_ids", "supporting_paper_ids", "paper_ids"))
    return {"core": core, "supporting": _unique_strings([*cited, *core])}


def _claim_integrity_fields(
    claim: dict[str, Any],
    fallback_claim: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the deterministic statement/excerpt boundary for one claim."""

    statement, source, statement_flags, selected_excerpt = _select_authoring_statement(
        claim, fallback_claim
    )
    excerpt = _bounded_evidence_excerpt(claim) or selected_excerpt
    excerpt_flags = _handoff_text_flags(excerpt) if excerpt else []
    compatibility = _proposition_compatibility_flag(statement, excerpt)
    integrity_flags = list(dict.fromkeys([*statement_flags]))
    if compatibility:
        integrity_flags.append(compatibility)
    return {
        "authoring_statement": statement,
        "statement_source": source,
        "statement_integrity_flags": statement_flags,
        "bounded_evidence_paraphrase": excerpt,
        "bounded_evidence_paraphrase_flags": excerpt_flags,
        "proposition_compatibility_flag": compatibility,
        "integrity_flags": list(dict.fromkeys(integrity_flags)),
    }


def _short_text(value: Any, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] if len(text) > limit else text


def _compact_claim_for_authoring(
    claim: dict[str, Any],
    ledger_row: dict[str, Any],
) -> dict[str, Any]:
    """Build the only claim shape that should enter an R4 model turn."""

    excerpt_flags = _unique_strings(
        ledger_row.get("bounded_evidence_paraphrase_flags") or []
    )
    compatibility = str(
        ledger_row.get("proposition_compatibility_flag") or ""
    ).strip()
    excerpt = str(ledger_row.get("bounded_evidence_paraphrase") or "").strip()
    excerpt_safe = bool(excerpt and len(excerpt) <= 480 and not excerpt_flags and not compatibility)
    compact: dict[str, Any] = {
        "claim_id": str(ledger_row.get("claim_id") or claim.get("claim_id") or ""),
        "statement": _short_text(ledger_row.get("statement") or claim.get("statement"), 2200),
        "strength": str(ledger_row.get("strength") or "qualified"),
        "writing_permission": str(ledger_row.get("writing_permission") or "hedged_factual_assertion"),
        "importance": claim.get("importance", ""),
        "evidence_type": str(claim.get("evidence_type") or ""),
        "claim_kind": str(claim.get("claim_kind") or ledger_row.get("claim_kind") or ""),
        "evidence_binding_status": str(ledger_row.get("evidence_binding_status") or ""),
        "permission_status": str(ledger_row.get("permission_status") or ""),
        "claim_state": str(ledger_row.get("claim_state") or ""),
        "section_fit": str(claim.get("section_fit") or ""),
        "supporting_text_chunk_ids": list(ledger_row.get("supporting_text_chunk_ids") or []),
        "factual_support_chunk_ids": list(ledger_row.get("factual_support_chunk_ids") or []),
        "contextual_support_chunk_ids": list(ledger_row.get("contextual_support_chunk_ids") or []),
        "core_chunk_ids": list(ledger_row.get("core_chunk_ids") or []),
        "core_paper_ids": list(ledger_row.get("core_paper_ids") or []),
        "supporting_paper_ids": list(ledger_row.get("supporting_paper_ids") or []),
        "supporting_visual_chunk_ids": list(ledger_row.get("supporting_visual_chunk_ids") or [])[:12],
        "missing_evidence_components": [
            _short_text(item, 220)
            for item in (ledger_row.get("missing_evidence_components") or [])[:8]
        ],
        "statement_source": str(ledger_row.get("statement_source") or ""),
        "statement_integrity_flags": list(ledger_row.get("statement_integrity_flags") or []),
    }
    if excerpt_safe:
        compact["bounded_evidence_paraphrase"] = _short_text(excerpt, 480)
    else:
        compact["bounded_evidence_paraphrase_excluded"] = True
        compact["bounded_evidence_paraphrase_exclusion_flags"] = list(dict.fromkeys([
            *excerpt_flags,
            *([compatibility] if compatibility else []),
        ]))
    return compact


def _compact_ledger_for_authoring(ledger: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep policy decisions while avoiding a second copy of every claim."""

    result: list[dict[str, Any]] = []
    for row in ledger:
        if not isinstance(row, dict):
            continue
        result.append({
            "claim_id": str(row.get("claim_id") or ""),
            "strength": str(row.get("strength") or "qualified"),
            "support_classification": str(
                row.get("support_classification")
                or ("supported" if row.get("strength") == "established" else "qualified" if row.get("strength") == "qualified" else "open_question")
            ),
            "writing_permission": str(row.get("writing_permission") or ""),
            "policy": _short_text(row.get("policy"), 360),
            "constraints": [
                _short_text(item, 220)
                for item in (row.get("constraints") or [])[:6]
            ],
            "evidence_binding_status": str(row.get("evidence_binding_status") or ""),
            "permission_status": str(row.get("permission_status") or ""),
            "claim_state": str(row.get("claim_state") or ""),
            "missing_evidence_components": [
                _short_text(item, 220)
                for item in (row.get("missing_evidence_components") or [])[:8]
            ],
        })
    return result


def _compact_bundle_for_authoring(
    bundle: dict[str, Any],
    ledger: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Keep the synthesis controls, not the candidate-pool audit dump."""

    strength_by_id = {
        str(row.get("claim_id")): str(row.get("strength") or "qualified")
        for row in ledger if isinstance(row, dict)
    }
    assignments: list[dict[str, Any]] = []
    for raw in bundle.get("claim_category_assignments") or []:
        if not isinstance(raw, dict):
            continue
        claim_id = str(raw.get("claim_id") or "")
        assignments.append({
            "claim_id": claim_id,
            "category": str(raw.get("category") or ""),
            "strength": strength_by_id.get(claim_id, "qualified"),
            "classification": str(
                raw.get("classification")
                or raw.get("support_classification")
                or ""
            ),
            "permission_status": str(raw.get("permission_status") or ""),
            "claim_state": str(raw.get("claim_state") or ""),
            "adaptation_action": str(raw.get("adaptation_action") or ""),
        })
    audit = bundle.get("handoff_quality_audit") or {}
    compact_audit = {
        "status": str(audit.get("status") or ""),
        "live_authoring_allowed": bool(audit.get("live_authoring_allowed", False)),
        "claim_count": int(audit.get("claim_count") or 0),
        "claims_with_canonical_support_ids": int(
            audit.get("claims_with_canonical_support_ids") or 0
        ),
        "blocking_flags": list(audit.get("blocking_flags") or [])[:8],
        "warnings": list(audit.get("warnings") or [])[:12],
    }
    compact: dict[str, Any] = {
        "bundle_id": str(bundle.get("bundle_id") or ""),
        "section_id": str(bundle.get("section_id") or ""),
        "argument_task": _short_text(bundle.get("argument_task"), 1800),
        "relationship_pattern": _short_text(bundle.get("relationship_pattern"), 800),
        "paper_ids": _unique_strings(bundle.get("paper_ids") or []),
        "chunk_ids": _unique_strings(bundle.get("chunk_ids") or []),
        "established_points": [
            _short_text(item, 1000) for item in (bundle.get("established_points") or [])[:8]
        ],
        "conditional_points": [
            _short_text(item, 1000) for item in (bundle.get("conditional_points") or [])[:12]
        ],
        "conflicts_or_boundaries": [
            _short_text(item, 800)
            for item in (bundle.get("conflicts_or_boundaries") or [])[:12]
        ],
        "claim_category_assignments": assignments,
        "argument_task_coverage": bundle.get("argument_task_coverage") or {},
        "author_synthesis_space": _short_text(bundle.get("author_synthesis_space"), 1600),
        "forbidden_overclaims": [
            _short_text(item, 360)
            for item in (bundle.get("forbidden_overclaims") or [])[:12]
        ],
        "relation_evidence": list(bundle.get("relation_evidence") or [])[:12],
        "source_permission_summary": bundle.get("source_permission_summary") or {},
        "candidate_pool_ref": str(bundle.get("candidate_pool_ref") or ""),
        "candidate_pool_count": int(bundle.get("candidate_pool_count") or 0),
        "candidate_paper_count": int(bundle.get("candidate_paper_count") or 0),
        "material_status": str(bundle.get("material_status") or ""),
        "readiness_status": str(bundle.get("readiness_status") or ""),
        "status": str(bundle.get("status") or ""),
        "claim_binding_status": bundle.get("claim_binding_status") or {},
        "section_outcome": str(bundle.get("section_outcome") or ""),
        "declared_limits": [
            _short_text(item, 360) for item in (bundle.get("declared_limits") or [])[:12]
        ],
        "open_questions": list(bundle.get("open_questions") or [])[:12],
        "adaptation_actions": list(bundle.get("adaptation_actions") or [])[:12],
        "merge_recommendation": bundle.get("merge_recommendation") or {},
        "r4_handoff_allowed": bool(bundle.get("r4_handoff_allowed", False)),
        "handoff_quality_audit": compact_audit,
    }
    return compact


def _compact_coverage_atlas(atlas: dict[str, Any]) -> dict[str, Any]:
    """Expose only section-level coverage facts to the author."""

    keep = (
        "section_id", "title", "argument_task", "status", "readiness_status",
        "section_outcome", "declared_limits", "open_questions",
        "unique_papers", "unique_chunks", "needs_expansion", "role_coverage",
        "missing_roles", "required_roles", "coverage_status",
    )
    result = {key: atlas[key] for key in keep if key in atlas}
    if not result:
        result = {"section_id": str(atlas.get("section_id") or "")}
    return result


def _compact_relations(relations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in relations:
        if not isinstance(raw, dict):
            continue
        result.append({
            key: raw[key]
            for key in (
                "relation_id", "source_paper_id", "target_paper_id", "source_id",
                "target_id", "relation_type", "status", "confidence",
                "basis_chunk_ids", "basis_text_chunk_ids", "relation_basis_chunk_ids",
                "observed", "authoring_eligible",
            ) if key in raw
        })
    return result[:24]


def _fingerprint_file(path: Optional[Path], root: Path) -> Optional[dict[str, Any]]:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    try:
        relative = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        relative = str(path)
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _claim_strength(claim: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Derive the strongest language the evidence contract permits.

    This is a writing-policy decision, not a truth detector.  It is intentionally
    conservative when a Phase-3 field is absent or contradictory.
    """

    binding = str(
        claim.get("evidence_binding_status")
        or claim.get("binding_status")
        or claim.get("status")
        or ""
    ).casefold().strip()
    permission = str(claim.get("permission_status") or "").casefold().strip()
    state = str(claim.get("claim_state") or "").casefold().strip()
    fit = str(claim.get("section_fit") or "").casefold().strip()
    classification = str(
        claim.get("support_classification")
        or claim.get("claim_classification")
        or ""
    ).casefold().strip()
    missing = _unique_strings(claim.get("missing_evidence_components") or [])
    flags = " ".join(str(item).casefold() for item in claim.get("critic_flags") or [])

    if classification == "open_question":
        return (
            "open",
            "Present this as an unresolved question, limitation, or evidence gap.",
            ["open_question_marker", "no_factual_assertion", "name_missing_component"],
        )
    if classification == "qualified":
        return (
            "qualified",
            "Use bounded or hedged language and state the condition, transfer boundary, or remaining gap.",
            ["hedged_language", "no_precision_beyond_source", "preserve_boundary_or_gap"],
        )

    if binding in {"unmatched", "contextual_fallback"}:
        # No direct core match — evidence is contextual support only.
        # Never upgrade to "established" regardless of other fields.
        return (
            "qualified",
            "Evidence is contextual support only; use bounded language and do not assert direct causal or quantitative findings.",
            ["hedged_language", "contextual_support_only", "no_direct_binding"],
        )
    if fit in {"boundary", "off_scope"} or binding in {"contradicted", "off_scope"}:
        return (
            "boundary",
            "State the scope boundary or disagreement explicitly; do not present this as an established fact.",
            ["boundary_marker", "no_unqualified_causal_or_quantitative_language"],
        )
    # Phase 3's strong claim-pool path intentionally defers formal verifier
    # calls to a later explicit stage.  ``unverified`` therefore does not mean
    # that a claim has no evidence when the same record already says
    # ``supported``, is permission-bound, and carries canonical support IDs.
    # Treat that state as cautious prose (or an established statement when no
    # qualification is required), rather than silently converting it into an
    # evidence-gap-only claim.
    supporting_ids = _unique_strings(
        claim.get("supporting_text_chunk_ids")
        or claim.get("supporting_chunk_ids")
        or claim.get("factual_support_chunk_ids")
        or []
    )
    if (
        classification == "supported"
        and supporting_ids
        and permission not in {
            "discovery_only",
            "background_and_candidate_only",
            "contextual_or_qualified_support",
        }
        and state not in {"open_question", "uncertain", "contested"}
        and not missing
    ):
        if "formal_verification_deferred" in flags:
            return (
                "qualified",
                "Use bounded language while the explicit formal verification stage remains deferred.",
                ["hedged_language", "stay_within_scope", "formal_verification_deferred"],
            )
        return (
            "established",
            "The audited materials permit a direct factual statement within the recorded scope.",
            ["stay_within_scope"],
        )
    if state in {"open_question", "uncertain", "contested"} or binding in {
        "open_question", "insufficient", "unresolved", "unverified",
    }:
        return (
            "open",
            "Present this as an unresolved question, limitation, or evidence gap.",
            ["open_question_marker", "no_factual_assertion", "name_missing_component"],
        )
    if permission in {
        "discovery_only", "background_and_candidate_only", "contextual_or_qualified_support",
    } or binding in {"partial", "qualified", "conditional", "candidate"} or state in {
        "partially_grounded", "conditional", "partial",
    } or missing or any(token in flags for token in (
        "partial", "qualified", "missing", "uncertain", "unsupported", "permission",
    )):
        return (
            "qualified",
            "Use bounded or hedged language and state the condition, transfer boundary, or remaining gap.",
            ["hedged_language", "no_precision_beyond_source", "preserve_boundary_or_gap"],
        )
    if binding in {"direct", "bound", "synthesized"} and permission not in {
        "discovery_only", "background_and_candidate_only", "contextual_or_qualified_support",
    } and state not in {"open_question", "uncertain", "contested"}:
        return (
            "established",
            "The audited materials permit a direct factual statement within the recorded scope.",
            ["stay_within_scope"],
        )
    # Missing lifecycle fields are not permission to strengthen a claim.
    return (
        "qualified",
        "Phase-3 status is incomplete; use synthesis or qualified language until it is resolved.",
        ["hedged_language", "do_not_upgrade_missing_status"],
    )


def _writing_permission(strength: str) -> str:
    return {
        "established": "factual_assertion",
        "qualified": "hedged_factual_assertion",
        "boundary": "interpretive_synthesis",
        "open": "evidence_gap_only",
    }.get(strength, "interpretive_synthesis")


def build_judgment_ledger(claims: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for raw in claims:
        if not isinstance(raw, dict):
            continue
        claim = dict(raw)
        claim_id = str(claim.get("claim_id") or "").strip()
        if not claim_id:
            continue
        strength, policy, constraints = _claim_strength(claim)
        integrity = _claim_integrity_fields(claim)
        chunk_ids = _claim_chunk_ids(claim)
        paper_ids = _claim_paper_ids(claim)
        statement = integrity["authoring_statement"]
        original_statement = re.sub(
            r"\s+", " ", str(claim.get("original_statement") or statement)
        ).strip()
        if any(
            flag in _handoff_text_flags(original_statement)
            for flag in (
                "bibliography_like",
                "arxiv_or_venue_header",
                "caption_or_table_of_contents",
            )
        ):
            original_statement = statement
        ledger.append({
            "claim_id": claim_id,
            "statement": statement,
            "authoring_statement": statement,
            "statement_source": integrity["statement_source"],
            "statement_integrity_flags": integrity["statement_integrity_flags"],
            "original_statement": original_statement,
            "bounded_evidence_paraphrase": integrity["bounded_evidence_paraphrase"],
            "bounded_evidence_paraphrase_flags": integrity[
                "bounded_evidence_paraphrase_flags"
            ],
            "proposition_compatibility_flag": integrity[
                "proposition_compatibility_flag"
            ],
            "integrity_flags": integrity["integrity_flags"],
            "strength": strength,
            "support_classification": str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or ("supported" if strength == "established" else "qualified" if strength == "qualified" else "open_question")
            ),
            "writing_permission": _writing_permission(strength),
            "policy": policy,
            "constraints": constraints,
            "evidence_binding_status": str(claim.get("evidence_binding_status") or claim.get("binding_status") or ""),
            "permission_status": str(claim.get("permission_status") or ""),
            "claim_state": str(claim.get("claim_state") or ""),
            "supporting_text_chunk_ids": chunk_ids["supporting"],
            "supporting_chunk_ids": chunk_ids["supporting"],
            "factual_support_chunk_ids": chunk_ids["factual"],
            "contextual_support_chunk_ids": chunk_ids["contextual"],
            "core_chunk_ids": chunk_ids["core"],
            "core_paper_ids": paper_ids["core"],
            "supporting_paper_ids": paper_ids["supporting"],
            "supporting_visual_chunk_ids": _unique_strings(claim.get("supporting_visual_chunk_ids") or []),
            "missing_evidence_components": _unique_strings(claim.get("missing_evidence_components") or []),
            "claim_kind": str(claim.get("claim_kind") or ""),
            "effective_statement": _short_text(claim.get("effective_statement"), 2200),
            "supported_rewrite": _short_text(claim.get("supported_rewrite"), 2200),
            "superseded_supported_rewrite": _short_text(
                claim.get("superseded_supported_rewrite"), 2200
            ),
            "declared_support_chunk_ids": _unique_strings(
                claim.get("declared_support_chunk_ids") or []
            ),
            "rejected_support_chunk_ids": _unique_strings(
                claim.get("rejected_support_chunk_ids") or []
            ),
            "source_permissions": dict(claim.get("source_permissions") or {}),
            "claim_provenance": dict(claim.get("claim_provenance") or {}),
            "adaptation_action": str(claim.get("adaptation_action") or ""),
            "adaptation_recommendation": dict(
                claim.get("adaptation_recommendation") or {}
            ),
        })
    return ledger


def audit_r4_handoff_quality(
    claims: Iterable[dict[str, Any]],
    ledger: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Run a deterministic trust-boundary audit before authoring.

    Parser debris in a bounded evidence excerpt is a warning because the
    excerpt can be discarded without losing the authored proposition.  The
    same debris in the proposition itself is a blocker.  This keeps the bridge
    useful for qualified writing while preventing silent contamination.
    """

    rows = [item for item in ledger if isinstance(item, dict)]
    by_id = {str(item.get("claim_id")): item for item in rows}
    blocking_flags: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    claim_audits: list[dict[str, Any]] = []
    excluded_claim_ids: list[str] = []
    excluded_claims: list[dict[str, Any]] = []
    authorable_claim_ids: list[str] = []
    hard_flags = {
        "empty_text",
        "bibliography_like",
        "arxiv_or_venue_header",
        "caption_or_table_of_contents",
    }
    for raw in claims:
        if not isinstance(raw, dict):
            continue
        claim_id = str(raw.get("claim_id") or "").strip()
        if not claim_id:
            continue
        row = by_id.get(claim_id, {})
        statement_flags = _unique_strings(row.get("statement_integrity_flags") or [])
        excerpt_flags = _unique_strings(
            row.get("bounded_evidence_paraphrase_flags") or []
        )
        compatibility = str(row.get("proposition_compatibility_flag") or "").strip()
        support_ids = _unique_strings(
            row.get("supporting_text_chunk_ids")
            or row.get("supporting_chunk_ids")
            or []
        )
        strength = str(row.get("strength") or "qualified")
        claim_record = {
            "claim_id": claim_id,
            "strength": strength,
            "supporting_id_count": len(support_ids),
            "statement_flags": statement_flags,
            "bounded_excerpt_flags": excerpt_flags,
            "proposition_compatibility_flag": compatibility or None,
        }
        claim_audits.append(claim_record)
        claim_scope_blockers: list[str] = []
        for flag in statement_flags:
            if flag in hard_flags:
                blocking_flags.append({
                    "claim_id": claim_id,
                    "flag": f"authoring_statement:{flag}",
                })
                claim_scope_blockers.append(f"authoring_statement:{flag}")
        for flag in excerpt_flags:
            warnings.append({
                "claim_id": claim_id,
                "flag": f"bounded_evidence_paraphrase:{flag}",
            })
        if compatibility:
            warnings.append({
                "claim_id": claim_id,
                "flag": compatibility,
            })
        if strength == "established" and not support_ids:
            blocking_flags.append({
                "claim_id": claim_id,
                "flag": "established_claim_without_canonical_support_ids",
            })
            claim_scope_blockers.append(
                "established_claim_without_canonical_support_ids"
            )
        elif not support_ids:
            warnings.append({
                "claim_id": claim_id,
                "flag": "claim_without_canonical_support_ids",
            })
        if claim_scope_blockers:
            excluded_claim_ids.append(claim_id)
            excluded_claims.append({
                "claim_id": claim_id,
                "flags": list(dict.fromkeys(claim_scope_blockers)),
            })
        else:
            authorable_claim_ids.append(claim_id)

    claim_count = len(claim_audits)
    supported_count = sum(
        1 for item in claim_audits if item["supporting_id_count"] > 0
    )
    excluded_claim_ids = list(dict.fromkeys(excluded_claim_ids))
    authorable_claim_ids = list(dict.fromkeys(authorable_claim_ids))
    if claim_count and not authorable_claim_ids:
        status = "blocked"
    elif excluded_claim_ids:
        status = "pass_with_limits"
    elif warnings:
        status = "pass_with_warnings"
    else:
        status = "pass"
    return {
        "status": status,
        # Claim-scoped hard integrity flags exclude only the affected claim;
        # a section stays authorable while any other claim remains.
        "live_authoring_allowed": bool(authorable_claim_ids),
        "claim_count": claim_count,
        "claims_with_canonical_support_ids": supported_count,
        "blocking_flags": blocking_flags,
        "global_blocking_flags": [],
        "excluded_claim_ids": excluded_claim_ids,
        "excluded_claims": excluded_claims,
        "authorable_claim_ids": authorable_claim_ids,
        "warnings": warnings,
        "claim_audits": claim_audits,
        "summary": (
            f"{supported_count}/{claim_count} claims have canonical support IDs; "
            f"{len(excluded_claim_ids)} excluded claims, "
            f"{len(blocking_flags)} blocking claim records, and "
            f"{len(warnings)} warnings."
        ),
    }


def _sanitize_bundle_for_authoring(
    bundle: dict[str, Any],
    claims: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    audit: dict[str, Any],
    authorable_claim_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Remove raw rewrite fragments from the bundle consumed by the author."""

    safe = dict(bundle)
    by_id = {str(item.get("claim_id")): item for item in ledger}
    safe_assignments: list[dict[str, Any]] = []
    raw_assignments = bundle.get("claim_category_assignments") or []
    for raw in raw_assignments if isinstance(raw_assignments, list) else []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        claim_id = str(item.get("claim_id") or "")
        if (
            authorable_claim_ids is not None
            and claim_id not in authorable_claim_ids
        ):
            continue
        row = by_id.get(claim_id)
        if row:
            item["statement"] = row.get("statement", "")
            item["authoring_statement"] = row.get("authoring_statement", "")
            item["original_statement"] = row.get("original_statement", "")
            item["bounded_evidence_paraphrase"] = row.get(
                "bounded_evidence_paraphrase", ""
            )
            item["bounded_evidence_paraphrase_flags"] = row.get(
                "bounded_evidence_paraphrase_flags", []
            )
            item["statement_integrity_flags"] = row.get(
                "statement_integrity_flags", []
            )
        # These legacy names are deliberately removed from the author-facing
        # payload; the bounded excerpt has its own auditable field above.
        item.pop("effective_statement", None)
        item.pop("supported_rewrite", None)
        safe_assignments.append(item)
    if raw_assignments:
        safe["claim_category_assignments"] = safe_assignments

    established: list[str] = []
    conditional: list[str] = []
    boundaries: list[str] = []
    for row in ledger:
        statement = str(row.get("statement") or "").strip()
        if not statement:
            continue
        strength = str(row.get("strength") or "qualified")
        if strength == "established":
            established.append(statement)
        elif strength in {"boundary", "open"}:
            boundaries.append(statement)
        else:
            conditional.append(statement)
    safe["established_points"] = list(dict.fromkeys(established))
    safe["conditional_points"] = list(dict.fromkeys(conditional))
    existing_boundaries = [
        str(item).strip() for item in bundle.get("conflicts_or_boundaries") or []
        if str(item).strip()
    ]
    safe["conflicts_or_boundaries"] = list(dict.fromkeys(
        [*existing_boundaries, *boundaries]
    ))
    safe["judgment_ledger"] = ledger
    safe["handoff_quality_audit"] = audit
    safe["authoring_statement_policy"] = (
        "Use statement/authoring_statement as the authored proposition. "
        "Treat bounded_evidence_paraphrase as optional evidence context only."
    )
    return safe


@dataclass
class R4SectionArtifacts:
    section_id: str
    bundle: dict[str, Any] = field(default_factory=dict)
    raw_bundle: dict[str, Any] = field(default_factory=dict)
    coverage_atlas: dict[str, Any] = field(default_factory=dict)
    material_bindings: dict[str, Any] = field(default_factory=dict)
    claim_graph: dict[str, Any] = field(default_factory=dict)
    relation_graph: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    judgment_ledger: list[dict[str, Any]] = field(default_factory=list)
    handoff_audit: dict[str, Any] = field(default_factory=dict)
    visual_chunk_ids: list[str] = field(default_factory=list)
    visual_needs: list[dict[str, Any]] = field(default_factory=list)
    source_ledger_path: Optional[Path] = None
    overlay_path: Optional[Path] = None
    kb_paths: list[Path] = field(default_factory=list)
    artifact_refs: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    production_handoff_valid: bool = False
    production_handoff_source: str = "missing"
    production_handoff_readiness: dict[str, Any] = field(default_factory=dict)
    legacy_migration_active: bool = False
    excluded_claim_ids: list[str] = field(default_factory=list)
    authorable_claim_ids: list[str] = field(default_factory=list)

    @property
    def ready_for_authoring(self) -> bool:
        if not self.production_handoff_valid and not self.legacy_migration_active:
            return False
        status_ok = str(
            self.bundle.get("readiness_status")
            or self.bundle.get("status")
            or ""
        ).casefold() in {
            "ready_for_authoring",
            "material_ready",
            "ready",
            "ready_with_limits",
            "write_with_declared_gap",
        }
        audit = self.handoff_audit or self.bundle.get("handoff_quality_audit") or {}
        section_readiness = self.production_handoff_readiness.get("sections", {}).get(self.section_id, {})
        readiness_ok = (
            not section_readiness
            or bool(section_readiness.get("ready_for_authoring"))
        )
        # An incomplete legacy handoff is never promoted to the canonical
        # production contract. Its R4 readiness is bounded by the migrated
        # bundle and the bridge audit; the failed R3 validation remains
        # visible in both payloads.
        if (
            self.legacy_migration_active
            and self.production_handoff_source == "explicit_legacy_migration"
        ):
            readiness_ok = True
        return status_ok and readiness_ok and bool(audit.get("live_authoring_allowed", True))

    @property
    def authorable_with_limits(self) -> bool:
        """Canonical structural admission despite a scientific shortfall.

        A valid canonical R3 section may enter R4 with declared limits when
        it still owns its source ledger, has usable KB/material context, and
        retains at least one non-excluded claim.  ``needs_more_literature``
        and other advisory outcomes do not revoke this path.
        """

        if not self.production_handoff_valid:
            return False
        if not self.source_ledger_path:
            return False
        if not self.kb_paths:
            return False
        return bool(self.authorable_claim_ids)

    @property
    def admitted_for_authoring(self) -> bool:
        """Either fully ready or structurally authorable with limits."""

        return self.ready_for_authoring or self.authorable_with_limits

    def to_context_payload(self) -> dict[str, Any]:
        authorable_ids = set(self.authorable_claim_ids)
        authorable_ledger = [
            row
            for row in self.judgment_ledger
            if isinstance(row, dict)
            and str(row.get("claim_id") or "") in authorable_ids
        ]
        ledger_by_id = {
            str(row.get("claim_id")): row
            for row in authorable_ledger
            if isinstance(row, dict)
        }
        compact_claims = [
            _compact_claim_for_authoring(claim, ledger_by_id.get(
                str(claim.get("claim_id")), {}
            ))
            for claim in self.claims
            if isinstance(claim, dict)
            and str(claim.get("claim_id") or "") in authorable_ids
        ]
        return {
            "schema_version": "r4.phase3_authoring_contract.v2",
            "section_id": self.section_id,
            "claims": compact_claims,
            "relations": _compact_relations(self.relations),
            "coverage_atlas": _compact_coverage_atlas(self.coverage_atlas),
            "synthesis_bundle": _compact_bundle_for_authoring(
                self.bundle, authorable_ledger
            ),
            "judgment_ledger": _compact_ledger_for_authoring(authorable_ledger),
            "handoff_quality_audit": self.handoff_audit,
            "excluded_claims": [
                dict(item)
                for item in self.handoff_audit.get("excluded_claims") or []
                if isinstance(item, Mapping)
                and str(item.get("claim_id") or "") in self.excluded_claim_ids
            ],
            "excluded_claim_ids": self.excluded_claim_ids,
            "authorable_claim_ids": self.authorable_claim_ids,
            "production_handoff": {
                "valid": self.production_handoff_valid,
                "source": self.production_handoff_source,
                "readiness": self.production_handoff_readiness,
                "legacy_migration_active": self.legacy_migration_active,
            },
            "visual_chunk_ids": self.visual_chunk_ids,
            "visual_needs": self.visual_needs,
            "artifact_refs": self.artifact_refs,
            "diagnostics": self.diagnostics,
        }

    def to_audit_payload(self) -> dict[str, Any]:
        """Return the complete bridge payload for disk audit, never model turns."""

        return {
            "schema_version": "r4.phase3_authoring_audit.v1",
            "section_id": self.section_id,
            "claims": self.claims,
            "relations": self.relations,
            "coverage_atlas": self.coverage_atlas,
            "synthesis_bundle": self.raw_bundle or self.bundle,
            "material_bindings": self.material_bindings,
            "claim_graph": self.claim_graph,
            "relation_graph": self.relation_graph,
            "judgment_ledger": self.judgment_ledger,
            "handoff_quality_audit": self.handoff_audit,
            "production_handoff": {
                "valid": self.production_handoff_valid,
                "source": self.production_handoff_source,
                "readiness": self.production_handoff_readiness,
                "legacy_migration_active": self.legacy_migration_active,
            },
            "visual_chunk_ids": self.visual_chunk_ids,
            "visual_needs": self.visual_needs,
            "artifact_refs": self.artifact_refs,
            "diagnostics": self.diagnostics,
        }


class R4Phase3ArtifactStore:
    """Load one Phase-3 directory through the canonical R3 handoff first.

    ``allow_legacy_migration`` is deliberately opt-in.  Without it, a
    missing or incompatible ``R3_PRODUCTION_HANDOFF.json`` leaves the store's
    authoring gate closed even if old plural artifacts are present.  The old
    artifacts are not consumed on this path; callers that accept them must use
    the explicit migration constructor.
    """

    def __init__(self, root: Path, *, allow_legacy_migration: bool = False):
        self.root = Path(root).resolve()
        self.diagnostics: list[str] = []
        self.canonical_handoff: R3ProductionHandoff | None = None
        self.handoff_report: R3ValidationReport | None = None
        self.production_handoff_valid = False
        self.production_handoff_source = "missing"
        self.production_handoff_readiness: dict[str, Any] = {}
        self._legacy_preview = False

        handoff_path = self.root / R3_HANDOFF_FILENAME
        if handoff_path.exists():
            try:
                handoff, report = read_r3_production_handoff(handoff_path)
                self.canonical_handoff = handoff
                self.handoff_report = report
                if report.valid:
                    self._install_handoff(handoff, report, source="canonical")
                else:
                    self.diagnostics.append("incompatible_R3_PRODUCTION_HANDOFF.json")
                    self.diagnostics.extend(
                        f"production_handoff:{item}" for item in report.errors
                    )
                    self.production_handoff_readiness = {
                        "sections": dict(report.section_readiness),
                        "global": dict(report.global_readiness),
                        "validation": report.to_dict(),
                    }
            except Exception as exc:
                self.diagnostics.append(
                    f"invalid_R3_PRODUCTION_HANDOFF.json:{type(exc).__name__}"
                )
        else:
            self.diagnostics.append("missing_R3_PRODUCTION_HANDOFF.json")

        if not self.production_handoff_valid and allow_legacy_migration:
            try:
                migrated = migrate_legacy_phase3_artifacts(self.root)
                report = migrated.validate()
                self.canonical_handoff = migrated
                self.handoff_report = report
                if report.valid:
                    self._install_handoff(
                        migrated,
                        report,
                        source="explicit_legacy_migration",
                    )
                else:
                    # The migration is explicit, so legacy artifacts may be
                    # inspected through the same R4 policy bridge even when
                    # the old directory cannot satisfy every new R3 schema
                    # invariant. Keep the R3 report failed and retain the
                    # migration marker; this path can never become canonical.
                    self._load_legacy_views()
                    self._legacy_preview = True
                    self.production_handoff_valid = False
                    self.production_handoff_source = "explicit_legacy_migration"
                    self.production_handoff_readiness = {
                        "sections": dict(report.section_readiness),
                        "global": dict(report.global_readiness),
                        "validation": report.to_dict(),
                    }
                self.production_handoff_source = "explicit_legacy_migration"
                self.diagnostics.append("legacy_migration_explicit")
                if not report.valid:
                    self.diagnostics.append("legacy_migration_r3_validation_failed")
            except Exception as exc:
                self.diagnostics.append(
                    f"legacy_migration_failed:{type(exc).__name__}:{exc}"
                )

        if not self.production_handoff_valid and not self._legacy_preview:
            # Fail closed.  Do not let the old plural files leak into a writer
            # through FullReviewOrchestrator's material-discovery predicate.
            # Callers that explicitly accept migration must use
            # ``from_legacy``/``allow_legacy_migration=True``.
            self.coverage_atlas = {}
            self.synthesis = {}
            self.bindings = {}
            self.claim_graph = {}
            self.relation_graph = {}
            self._legacy_preview = False
            self.production_handoff_source = "closed_missing_or_incompatible"

        for name, payload in (
            ("COVERAGE_ATLAS.json", self.coverage_atlas),
            ("SYNTHESIS_BUNDLES.json", self.synthesis),
            ("MATERIAL_BINDINGS.json", self.bindings),
        ):
            if not payload:
                self.diagnostics.append(f"missing_or_invalid_{name}")

    @classmethod
    def from_legacy(cls, root: Path) -> "R4Phase3ArtifactStore":
        """Explicit compatibility constructor for pre-R3 artifact roots."""

        return cls(root, allow_legacy_migration=True)

    def require_canonical_handoff(self) -> R3ProductionHandoff:
        """Return the producer handoff or fail closed for an R4 caller.

        The explicit legacy constructor is intentionally rejected here.  A
        top-level authoring orchestrator should call this before requesting a
        section context so a migration cannot silently become production.
        """

        if (
            self.production_handoff_source != "canonical"
            or not self.production_handoff_valid
            or self.canonical_handoff is None
        ):
            if self.handoff_report is not None:
                raise R3HandoffValidationError(self.handoff_report)
            raise RuntimeError(
                "R3_PRODUCTION_HANDOFF.json is missing or incompatible; R4 is closed."
            )
        return self.canonical_handoff

    def _install_handoff(
        self,
        handoff: R3ProductionHandoff,
        report: R3ValidationReport,
        *,
        source: str,
    ) -> None:
        payload = handoff.to_dict()
        self.coverage_atlas = dict(payload.get("coverage_atlas") or {})
        self.synthesis = {"bundles": dict(payload.get("synthesis_bundles") or {})}
        self.bindings = {"sections": dict(payload.get("material_bindings") or {})}
        self.claim_graph = dict(payload.get("claim_dag") or {})
        if not self.claim_graph.get("claims"):
            self.claim_graph["claims"] = list(payload.get("claims") or [])
        self.relation_graph = dict(payload.get("relation_graph") or {"edges": []})
        self.production_handoff_valid = bool(report.valid)
        self.production_handoff_source = source
        self.production_handoff_readiness = {
            "sections": dict(report.section_readiness),
            "global": dict(report.global_readiness),
            "validation": report.to_dict(),
        }

    def _load_legacy_views(self) -> None:
        self.coverage_atlas = _read_json(self.root / "COVERAGE_ATLAS.json")
        self.synthesis = _read_json(self.root / "SYNTHESIS_BUNDLES.json")
        self.bindings = _read_json(self.root / "MATERIAL_BINDINGS.json")
        self.claim_graph = _read_json(self.root / "CLAIM_GRAPH.json")
        self.relation_graph = _read_json(self.root / "RELATION_GRAPH_MIGRATED.json")
        if not self.relation_graph:
            relation_ref = self.coverage_atlas.get("source", {}).get("relation_graph")
            relation_path = _resolve_existing(relation_ref, self.root)
            self.relation_graph = _read_json(relation_path)

    def _bundle(self, section_id: str) -> dict[str, Any]:
        return _section_value(self.synthesis.get("bundles"), section_id)

    def _atlas(self, section_id: str) -> dict[str, Any]:
        return _section_value(self.coverage_atlas.get("sections"), section_id)

    def _binding_section(self, section_id: str) -> dict[str, Any]:
        return _section_value(self.bindings.get("sections"), section_id)

    def _claims(self, section_id: str) -> list[dict[str, Any]]:
        bound_section = self._binding_section(section_id)
        bound = _claim_values(bound_section.get("claims"))
        graph_claims = [
            dict(item) for item in _as_list(self.claim_graph.get("claims"))
            if isinstance(item, dict)
            and str(item.get("section_id") or section_id) == section_id
        ]
        graph_by_id = {
            str(item.get("claim_id")): item for item in graph_claims if item.get("claim_id")
        }
        result: list[dict[str, Any]] = []
        for item in bound or graph_claims:
            graph_claim = dict(graph_by_id.get(str(item.get("claim_id")), {}))
            claim = dict(graph_claim)
            claim.update(item)
            claim.setdefault("section_id", section_id)
            integrity = _claim_integrity_fields(claim, graph_claim)
            chunk_ids = _claim_chunk_ids(claim)
            paper_ids = _claim_paper_ids(claim)
            claim.update(integrity)
            claim["statement"] = integrity["authoring_statement"]
            original_candidates = [
                claim.get("original_statement"),
                graph_claim.get("original_statement"),
            ]
            safe_original = ""
            for candidate in original_candidates:
                value = re.sub(r"\s+", " ", str(candidate or "")).strip()
                if value and not any(
                    flag in _handoff_text_flags(value)
                    for flag in (
                        "bibliography_like",
                        "arxiv_or_venue_header",
                        "caption_or_table_of_contents",
                    )
                ):
                    safe_original = value
                    break
            claim["original_statement"] = safe_original or integrity["authoring_statement"]
            # Canonical names are materialized on the normalized claim so all
            # downstream consumers see the real Phase-3 aliases identically.
            claim["supporting_text_chunk_ids"] = chunk_ids["supporting"]
            claim["supporting_chunk_ids"] = chunk_ids["supporting"]
            claim["factual_support_chunk_ids"] = chunk_ids["factual"]
            claim["contextual_support_chunk_ids"] = chunk_ids["contextual"]
            claim["core_chunk_ids"] = chunk_ids["core"]
            claim["core_paper_ids"] = paper_ids["core"]
            claim["supporting_paper_ids"] = paper_ids["supporting"]
            # Keep the adaptation fields on the full audit object.  The
            # compact model payload remains bounded, while a reviewer can
            # still reconstruct the original/effective/rewrite decision.
            claim["support_classification"] = str(
                claim.get("support_classification")
                or claim.get("claim_classification")
                or ""
            )
            claim["claim_provenance"] = dict(claim.get("claim_provenance") or {})
            claim["source_permissions"] = dict(claim.get("source_permissions") or {})
            result.append(claim)
        return result

    def _relations(self, section_id: str, paper_ids: set[str]) -> list[dict[str, Any]]:
        rows = self.relation_graph.get("edges") or self.relation_graph.get("relations") or []
        result: list[dict[str, Any]] = []
        for raw in _as_list(rows):
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source_paper_id") or raw.get("source_id") or "")
            target = str(raw.get("target_paper_id") or raw.get("target_id") or "")
            if source not in paper_ids or target not in paper_ids:
                continue
            row = dict(raw)
            row.setdefault("section_id", section_id)
            result.append(row)
        return result

    def _section_ledger_path(self, section_id: str) -> Optional[Path]:
        """Resolve the section source ledger, preferring the canonical snapshot."""

        canonical = (
            self.root
            / "coverage_snapshot"
            / "sections"
            / section_id
            / "SECTION_SOURCE_LEDGER.json"
        )
        if canonical.is_file():
            return canonical
        source = self.coverage_atlas.get("source") or {}
        ledger_ref = source.get("section_ledgers")
        ledger_path = _resolve_existing(ledger_ref, self.root)
        if ledger_path and ledger_path.is_dir():
            ledger_path = _resolve_existing(
                ledger_path / section_id / "SECTION_SOURCE_LEDGER.json",
                self.root,
            )
        if ledger_path is None:
            matches = sorted(
                self.root.glob(f"**/{section_id}/SECTION_SOURCE_LEDGER.json")
            )
            ledger_path = matches[-1] if matches else None
        return ledger_path

    def _kb_paths(self, source: Mapping[str, Any]) -> list[Path]:
        kb_paths: list[Path] = []
        for raw in source.get("shared_kb_paths") or []:
            path = _resolve_existing(raw, self.root)
            if path and path not in kb_paths:
                kb_paths.append(path)
        # The latest staging KB is useful when the shared migration does not
        # yet contain a newly materialized OA paper.
        for path in sorted(self.root.glob("**/*KB.sqlite")) + sorted(self.root.glob("**/*kb.sqlite")):
            if path not in kb_paths:
                kb_paths.append(path)
        return kb_paths

    def _paths(self, section_id: str) -> tuple[Optional[Path], Optional[Path], list[Path]]:
        if not self.production_handoff_valid and not self._legacy_preview:
            return None, None, []
        source = self.coverage_atlas.get("source") or {}
        ledger_path = self._section_ledger_path(section_id)
        if (
            self.production_handoff_valid
            and self.production_handoff_source == "canonical"
        ):
            # The canonical coverage_snapshot ledger is already section-scoped:
            # it carries the section's papers, canonical permissions, and chunk
            # IDs.  The old Phase-3 input overlay is an input filter, not an
            # authoring boundary; applying it here collapses a wide canonical
            # section to the narrow overlay papers.  Legacy migration/preview
            # keeps its historical overlay behavior.
            return ledger_path, None, self._kb_paths(source)
        overlay_ref = (source.get("overlay_paths") or {}).get(section_id)
        overlay_path = _resolve_existing(overlay_ref, self.root)
        if overlay_path is None:
            matches = sorted(
                self.root.glob(f"**/{section_id}/SECTION_ASSET_OVERLAY.json")
            )
            overlay_path = matches[-1] if matches else None
        return ledger_path, overlay_path, self._kb_paths(source)

    def section(self, section_id: str) -> R4SectionArtifacts:
        raw_bundle = self._bundle(section_id)
        bundle = dict(raw_bundle)
        if not self.production_handoff_valid and not self._legacy_preview:
            bundle = {
                "section_id": section_id,
                "status": "blocked",
                "readiness_status": "blocked",
                "r4_handoff_allowed": False,
                "production_handoff_required": True,
            }
        atlas = self._atlas(section_id)
        binding_section = self._binding_section(section_id)
        claims = self._claims(section_id)
        paper_ids = set(_unique_strings(bundle.get("paper_ids") or binding_section.get("paper_ids") or []))
        chunk_ids = set(_unique_strings(bundle.get("chunk_ids") or []))
        for claim in claims:
            paper_ids.update(_unique_strings(claim.get("citation_paper_ids") or []))
            paper_ids.update(_claim_paper_ids(claim)["supporting"])
            chunk_ids.update(_claim_chunk_ids(claim)["supporting"])
        relations = self._relations(section_id, paper_ids)
        visuals = _unique_strings(
            bundle.get("visual_chunk_ids")
            or bundle.get("visual_asset_ids")
            or binding_section.get("visual_chunk_ids")
            or []
        )
        if self.canonical_handoff is not None:
            visuals.extend(
                _text(item.get("visual_id") or item.get("visual_chunk_id"))
                for item in self.canonical_handoff.visual_bindings.get(section_id, [])
                if isinstance(item, dict)
            )
        for claim in claims:
            visuals.extend(_as_list(claim.get("supporting_visual_chunk_ids")))
        visuals = _unique_strings(visuals)
        visual_needs = []
        if self.canonical_handoff is not None:
            visual_needs = [
                dict(item)
                for item in self.canonical_handoff.visual_needs.get(section_id, [])
                if isinstance(item, dict)
            ]
        if not visual_needs:
            visual_needs = [
                dict(item)
                for item in _as_list(bundle.get("visual_needs"))
                if isinstance(item, dict)
            ]
        ledger = build_judgment_ledger(claims)
        handoff_audit = audit_r4_handoff_quality(claims, ledger)
        excluded_claim_ids = [
            str(item)
            for item in handoff_audit.get("excluded_claim_ids") or []
        ]
        authorable_claim_ids = [
            str(item)
            for item in handoff_audit.get("authorable_claim_ids") or []
        ]
        authorable_set = set(authorable_claim_ids)
        authorable_claims = [
            claim
            for claim in claims
            if str(claim.get("claim_id") or "") in authorable_set
        ]
        authorable_ledger = [
            row
            for row in ledger
            if str(row.get("claim_id") or "") in authorable_set
        ]
        if self.production_handoff_valid or self._legacy_preview:
            bundle = _sanitize_bundle_for_authoring(
                bundle,
                authorable_claims,
                authorable_ledger,
                handoff_audit,
                authorable_claim_ids=authorable_set,
            )
        source_ledger, overlay, kb_paths = self._paths(section_id)
        artifact_refs: dict[str, Any] = {}
        for name in (
            R3_HANDOFF_FILENAME,
            "COVERAGE_ATLAS.json",
            "SYNTHESIS_BUNDLES.json",
            "MATERIAL_BINDINGS.json",
            "CLAIM_GRAPH.json",
            "RELATION_GRAPH_MIGRATED.json",
        ):
            ref = _fingerprint_file(self.root / name, self.root)
            if ref:
                artifact_refs[name] = ref
        for label, path in (
            ("section_source_ledger", source_ledger),
            ("section_overlay", overlay),
        ):
            ref = _fingerprint_file(path, self.root)
            if ref:
                artifact_refs[label] = ref
        artifact_refs["kb_paths"] = [
            ref for ref in (_fingerprint_file(path, self.root) for path in kb_paths)
            if ref
        ]
        diagnostics = list(self.diagnostics)
        if not self.production_handoff_valid:
            diagnostics.append("production_handoff_gate_closed")
        section_report = (
            self.handoff_report.section_readiness.get(section_id, {})
            if self.handoff_report is not None
            else {}
        )
        if section_report and not section_report.get("ready_for_authoring"):
            diagnostics.extend(
                f"production_handoff_section:{item}"
                for item in section_report.get("blocking_reasons") or []
            )
        if not source_ledger:
            diagnostics.append("section_source_ledger_not_found")
        if not kb_paths:
            diagnostics.append("phase3_kb_not_found")
        if not claims:
            diagnostics.append("no_phase3_claims_for_section")
        return R4SectionArtifacts(
            section_id=section_id,
            bundle=bundle,
            raw_bundle=raw_bundle,
            coverage_atlas=atlas,
            material_bindings=binding_section,
            claim_graph=self.claim_graph,
            relation_graph=self.relation_graph,
            claims=claims,
            relations=relations,
            judgment_ledger=ledger,
            handoff_audit=handoff_audit,
            visual_chunk_ids=visuals,
            visual_needs=visual_needs,
            source_ledger_path=source_ledger,
            overlay_path=overlay,
            kb_paths=kb_paths,
            artifact_refs=artifact_refs,
            diagnostics=diagnostics,
            production_handoff_valid=self.production_handoff_valid,
            production_handoff_source=self.production_handoff_source,
            production_handoff_readiness=self.production_handoff_readiness,
            legacy_migration_active=self._legacy_preview,
            excluded_claim_ids=excluded_claim_ids,
            authorable_claim_ids=authorable_claim_ids,
        )
