"""Dynamic, evidence-aware review blueprint planner.

This module is intentionally not a fixed outline template.  It consumes the
current ReviewKnowledgeBase + visual-aware concept map and builds a review
blueprint from retrieved materials, concept clusters, and structural patterns
learned from review examples.

Design rule:
- No domain-specific section titles are hard-coded.
- Planning memory and concept maps are planning aids, not citable evidence.
- Every section must carry traceable concept/text/visual anchors.
- Optional LLM planning is integrated into the blueprint body, not attached as
  a sidecar that downstream agents must manually merge.
- Production is Qwen-required: Qwen authors the scientific chapter division
  (which 8-10 chapters, their boundaries, and handoffs).  Python enforces the
  8-10 range, validates division fields, normalizes safely, audits, and fails
  closed; it never invents a scientific outline in production.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from llm.qwen_chat_client import call_qwen_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONCEPT_MAP = (
    PROJECT_ROOT
    / "outputs"
    / "concept_maps"
    / "core58-hqvisual-conceptmap-v2-fields-20260703"
    / "concept_map.visual_aware.v1.json"
)
DEFAULT_KB_DIR = PROJECT_ROOT / "outputs" / "review_knowledge_base" / "core58-rkb-hqvisual-v1-20260703"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "review_blueprints" / "dynamic-blueprint-v4"
DEFAULT_REVIEW_EXAMPLE_MEMORY = (
    PROJECT_ROOT
    / "outputs"
    / "review_example_memory"
    / "review-example-structure-v1-20260703"
    / "review_example_structure_memory.json"
)
DEFAULT_PLANNER_PROMPT = PROJECT_ROOT / "prompts" / "Dynamic Review Blueprint Planner.txt"
DEFAULT_BLUEPRINT_GROUNDER_PROMPT = PROJECT_ROOT / "prompts" / "Review Blueprint Evidence Grounder.txt"
DEFAULT_REVIEW_MENTOR_PROMPT = PROJECT_ROOT / "prompts" / "Review Mentor Agent.txt"
# Project policy: claim-pool decomposition runs on the b_plus (qwen3.7-flash)
# tier.  This is audited in claim_decomposition_status.
CLAIM_DECOMPOSER_MODEL_TIER = "b_plus_model"
# Project policy: the section evidence grounder is also a planning-stage call
# and stays on qwen3.7-flash (b_plus_model), never the writing-tier model.
BLUEPRINT_GROUNDER_MODEL_TIER = "b_plus_model"

# Forced reground preserves the Qwen intellectual architecture and nothing
# else per section.  The official planner prompt treats
# candidate_search_seeds and evidence_risks as Qwen architecture fields, so
# they are preserved (never replaced by deterministic local defaults).  Every
# material/downstream-derived field (candidate pools, claims, bindings,
# digests, dossiers, grounding audits, transport menus) is stripped.
_FORCED_REGROUND_SECTION_INTELLECTUAL_KEYS = (
    "section_id",
    "title",
    "argument_role",
    "unique_contribution",
    "must_cover",
    "must_not_cover",
    "assigned_user_axes",
    "handoff_from_previous",
    "handoff_to_next",
    "key_questions",
    "claim_seeds",
    "visual_argument_goals",
    "candidate_search_seeds",
    "writing_requirements",
    "evidence_risks",
    "transition_to_next",
)
# Architecture-level Qwen fields preserved across a forced reground.
_FORCED_REGROUND_ARCHITECTURE_PRESERVED_KEYS = (
    "review_thesis",
    "narrative_strategy",
    "high_value_gap_seeds",
    "input_context",
    "_architecture_contract_warnings",
    "_planner_reused_path",
)
# Section grounder calls are bounded to a total wall-clock budget per request;
# the streaming client enforces this via its elapsed-time check.  Overridable
# with QWEN_GROUNDER_HTTP_TIMEOUT_SEC (default 180s).
BLUEPRINT_GROUNDER_HTTP_TIMEOUT_SEC = 180.0

LEGACY_TEMPLATE_TITLES = {
    "scope, stakes, and why this review is needed now",
    "physical basis: spectral heat flow and atmospheric-window constraints",
    "material and structural routes for spectral control",
    "transparent and agricultural films: par, nir, mir, and crop-temperature trade-offs",
    "benchmarking, outdoor reliability, and deployment bottlenecks",
    "frontier opportunities: adaptive, hybrid, and evidence-aware design rules",
}

STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "among",
    "and",
    "are",
    "because",
    "been",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "design",
    "does",
    "for",
    "from",
    "have",
    "how",
    "into",
    "its",
    "literature",
    "may",
    "more",
    "not",
    "optical",
    "paper",
    "papers",
    "review",
    "should",
    "study",
    "such",
    "that",
    "the",
    "their",
    "these",
    "this",
    "through",
    "what",
    "when",
    "where",
    "which",
    "with",
    "within",
    "would",
}

# ---------------------------------------------------------------------------
# Advisory section candidate-pool policy
# ---------------------------------------------------------------------------

# Each review section's body-text candidate pool has an advisory planning
# target of 150-200 distinct chunks.  This is a planning target, not a quota,
# an admission gate, or a hard cutoff: sections below the range are never
# padded with duplicates or irrelevant chunks, and sections above the range
# are retained in full and reported as above_target_range instead of being cut
# at the 200th item.
PREFERRED_SECTION_TEXT_CANDIDATE_RANGE: list[int] = [150, 200]

# Default retrieval breadth for the planning evidence pool.  The defaults are
# deliberately much wider than the former 90-total/70-served behavior so a
# rich library can usually yield roughly 150-200 distinct body chunks per
# section.  Explicit constructor/CLI limits remain available for deliberately
# constrained runs and are always reported in metadata.
DEFAULT_RETRIEVAL_LIMIT_PER_QUERY = 48
DEFAULT_RETRIEVAL_MAX_TOTAL = 800
# Visual candidate behavior is unchanged: keep the existing 80-total/70-served
# visual transport contract.
DEFAULT_VISUAL_RETRIEVAL_MAX_TOTAL = 80
DEFAULT_VISUAL_SERVED_MAX = 70

# The default served_text_limit is None = preserve every retrieved/served
# unique candidate.  Explicit positive limits constrain deliberately limited
# runs and truncate audibly.
DEFAULT_SERVED_TEXT_LIMIT: int | None = None

# Bounded raw-dossier transport for LLM payloads.  The full pool and its batch
# digest carry every retained candidate; only raw dossier context is
# selectively reopened with an explicit, audited transport budget.
DEFAULT_SECTION_RAW_DOSSIER_TRANSPORT_LIMIT = 24

# Section-specific semantic admission for the evidence grounder.
#
# Candidates are scored against the section architecture query using the
# configured material semantic vector cache (text-embedding-v4 cosine scores)
# when available.  The admission threshold is permissive and adaptive: it is a
# fraction of that section's best score plus a loose absolute floor, so a
# section with many genuinely strong candidates keeps all of them (no top-N,
# no 200-item hard cut) while clearly off-section candidates are excluded.
# When the semantic route is unavailable/fails, a section-specific lexical /
# material-card fallback applies the same relative rule with a lower floor.
SECTION_SEMANTIC_ADMISSION_RELATIVE_FLOOR = 0.72
SECTION_SEMANTIC_ADMISSION_ABSOLUTE_FLOOR = 0.45
SECTION_LEXICAL_ADMISSION_RELATIVE_FLOOR = 0.30
SECTION_LEXICAL_ADMISSION_ABSOLUTE_FLOOR = 0.10


def advisory_candidate_pool_status(count: int) -> str:
    """Classify a retained candidate count against the advisory range."""
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = 0
    low, high = PREFERRED_SECTION_TEXT_CANDIDATE_RANGE
    if count < low:
        return "below_target_range"
    if count <= high:
        return "within_target_range"
    return "above_target_range"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def compact(value: Any, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _command_knowledge_block() -> dict[str, Any]:
    """Load the versioned scientific Skill command bundle for production Qwen calls.

    The architecture-planning role consumes ONLY the ``top-review-architecture``
    manual.  Authoring, audit, and manuscript-integration manuals belong to
    their own downstream roles and are never injected here.  Skill bundles are
    command knowledge only, never scientific evidence.  M1 case-move advice is
    kept in a separate payload field; paper evidence stays the only scientific
    evidence.
    """
    from optomind_research.runtime.skill_loader import (
        get_command_skill_bundles,
        get_skill_guidance_prompt,
    )

    skills_dir = PROJECT_ROOT / "skills"
    bundles = get_command_skill_bundles(
        skills_dir, skill_ids=["top-review-architecture"]
    )
    valid = [
        bundle
        for bundle in bundles
        if bundle.name == "top-review-architecture"
        and re.match(r"^\d+\.\d+\.\d+$", bundle.version)
        and bundle.evidence_prohibition is True
    ]
    if len(valid) != 1:
        raise RuntimeError(
            "Production Qwen architecture planning requires the versioned "
            "'top-review-architecture' command-knowledge skill bundle; "
            f"received {[bundle.name for bundle in bundles] or 'none'}."
        )
    bundles = valid
    return {
        "source": "skill_guidance_api",
        "label": "command_knowledge_only_not_evidence",
        "role": "architecture_planning",
        "bundles_loaded": ["top-review-architecture"],
        "prompt_block": get_skill_guidance_prompt(
            skills_dir, skill_ids=["top-review-architecture"]
        ),
        "bundles": [
            {
                "skill": bundle.name,
                "skill_version": bundle.version,
                "role": bundle.role,
                "applicability": bundle.applicability,
                "evidence_prohibition": bundle.evidence_prohibition,
                "instructions": bundle.instructions,
            }
            for bundle in bundles
        ],
    }


_M1_FORBIDDEN_KEYS = frozenset(
    {
        "command_knowledge",
        "prompt_block",
        "skills",
        "skill_version",
        "instructions",
        "provenance",
        "bundles",
    }
)


def _m1_case_moves_payload(mentor_advice: dict[str, Any]) -> Any:
    """Return pure case-move knowledge, never command knowledge or the mentor envelope.

    Prefers ``mentor_advice['m1_case_moves']`` when present; otherwise falls
    back to the compact ``usable_intellectual_moves`` list only.
    """
    if not isinstance(mentor_advice, dict):
        return []
    value = mentor_advice.get("m1_case_moves")
    if value is None:
        value = mentor_advice.get("usable_intellectual_moves") or []
    if isinstance(value, dict):
        return {
            key: item
            for key, item in value.items()
            if key not in _M1_FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [
            item
            for item in value
            if not (
                isinstance(item, dict)
                and any(key in item for key in _M1_FORBIDDEN_KEYS)
            )
        ]
    return value


def _concrete_sibling_exclusion_errors(
    raw_sections: list[dict[str, Any]],
) -> list[str]:
    """Return errors when must_not_cover does not name a concrete sibling job.

    Every exclusion must mention a sibling chapter by section ID and share at
    least one distinctive token with that sibling's title, argument role, or
    unique contribution.  Generic self-referential text is rejected because
    Python must not silently accept an ownership boundary Qwen did not draw.
    """
    by_id: dict[str, dict[str, Any]] = {
        str(section.get("section_id") or ""): section
        for section in raw_sections
        if isinstance(section, dict)
    }
    errors: list[str] = []
    for section in raw_sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        for index, exclusion in enumerate(section.get("must_not_cover") or []):
            text = str(exclusion or "").strip()
            if not text:
                continue
            mentioned_ids = set(re.findall(r"S\d{2}", text))
            matched = False
            for sibling_id, sibling in by_id.items():
                if sibling_id == section_id:
                    continue
                if sibling_id not in mentioned_ids:
                    continue
                sibling_text = " ".join(
                    [
                        str(sibling.get("title") or ""),
                        str(sibling.get("argument_role") or ""),
                        str(sibling.get("unique_contribution") or ""),
                        " ".join(
                            str(item)
                            for item in (sibling.get("must_cover") or [])
                        ),
                    ]
                )
                shared = set(tokenize(text, limit=40)) & set(
                    tokenize(sibling_text, limit=160)
                )
                if shared:
                    matched = True
                    break
            if not matched:
                errors.append(
                    f"{section_id} must_not_cover[{index}] must name a "
                    "concrete sibling-owned responsibility: include the "
                    "sibling section ID (for example S04) plus a distinctive "
                    "word from that sibling's title, argument_role, or "
                    "unique_contribution; received "
                    f"{compact(text, 140)!r}."
                )
    return errors


def build_evidence_digest(
    chunks: list[dict[str, Any]],
    *,
    batch_size: int = 12,
    summary_limit: int = 220,
) -> dict[str, Any]:
    """Build a bounded, batch-readable view of a larger candidate pool.

    The full chunks remain available for exact source binding.  Models receive
    short material-card propositions, one compact summary per chunk, and
    batch summaries instead of the full text of every candidate at once.
    """
    records: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict) or not chunk.get("chunk_id"):
            continue
        binding = chunk.get("material_card_binding")
        binding = binding if isinstance(binding, dict) else {}
        propositions = [
            {
                "proposition_id": str(item.get("proposition_id") or ""),
                "statement": compact(item.get("statement"), summary_limit),
                "question_function": compact(item.get("question_function"), 80),
                "evidence_ceiling": compact(item.get("evidence_ceiling"), 100),
            }
            for item in (binding.get("propositions") or [])
            if isinstance(item, dict) and compact(item.get("statement"), summary_limit)
        ][:3]
        summary = " | ".join(item["statement"] for item in propositions)
        if not summary:
            summary = compact(
                chunk.get("text_preview")
                or chunk.get("normalized_text")
                or chunk.get("material_binding_search_text")
                or chunk.get("title"),
                summary_limit,
            )
        records.append(
            {
                "ref": f"T{index:03d}",
                "chunk_id": str(chunk.get("chunk_id")),
                "paper_id": str(chunk.get("paper_id") or ""),
                "title": compact(chunk.get("title"), 120),
                "summary": summary,
                "propositions": propositions,
                "source_kind": compact(chunk.get("source_kind"), 80),
                "content_depth": compact(chunk.get("content_depth"), 80),
                "use_permission": compact(chunk.get("use_permission"), 100),
            }
        )
    try:
        batch_size = max(4, int(batch_size))
    except (TypeError, ValueError):
        batch_size = 12
    batches: list[dict[str, Any]] = []
    for offset in range(0, len(records), batch_size):
        batch_records = records[offset : offset + batch_size]
        statements = [item["summary"] for item in batch_records if item.get("summary")]
        batches.append(
            {
                "batch_id": f"B{len(batches) + 1:02d}",
                "chunk_ids": [item["chunk_id"] for item in batch_records],
                "paper_ids": list(dict.fromkeys(item["paper_id"] for item in batch_records if item.get("paper_id"))),
                "summary": compact("; ".join(statements), 900),
            }
        )
    return {
        "schema_version": "review_blueprint.evidence_digest.v1",
        "strategy": "material_card_propositions_then_batched_extractive_summary",
        "batch_size": batch_size,
        "chunk_count": len(records),
        "retained_chunk_count": len(records),
        "batch_count": len(batches),
        "chunk_index": records,
        "batches": batches,
        "advisory_target_range": list(PREFERRED_SECTION_TEXT_CANDIDATE_RANGE),
        "candidate_pool_status": advisory_candidate_pool_status(len(records)),
        "hard_cut": False,
        "hard_200th_cutoff": False,
        "raw_text_policy": "Raw text stays in the evidence store and is reopened by chunk_id for exact verification; it is not bulk-injected into one model prompt.",
    }


def clean_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    if limit is not None:
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 8
        # Non-positive means unlimited.  Used by the planner's default
        # served_text_limit=None (preserve every candidate).
        if limit <= 0:
            limit = None
    out: list[str] = []
    for item in value:
        text = compact(item, 260)
        if text:
            out.append(text)
        if limit is not None and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Claim-centered evidence dossiers (bounded, deterministic input layer)
# ---------------------------------------------------------------------------

DEFAULT_DOSSIER_CONTEXT_LIMIT = 900
DEFAULT_DOSSIER_UNDERSTANDING_LIMIT = 600
DEFAULT_DOSSIER_QUOTE_LIMIT = 2000
DEFAULT_DOSSIER_PER_CLAIM_CHUNK_LIMIT = 6
# Keep the section evidence layer aligned with the planner's broad candidate
# pool.  The default is 0 = preserve every candidate in the section material
# layer; LLM payloads still use explicit, audited raw-dossier transport
# budgets (see DEFAULT_SECTION_RAW_DOSSIER_TRANSPORT_LIMIT) so batch summaries
# carry the full pool while only a bounded subset of raw context is reopened.
DEFAULT_SECTION_DOSSIER_CHUNK_LIMIT = 0


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    """Return (bounded_text, truncated). Exact quotes/claims are never bounded."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    if not text:
        return "", False
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def _chunk_record_map(
    chunk_records: dict[str, dict[str, Any]] | list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if isinstance(chunk_records, dict):
        return {
            str(chunk_id): record
            for chunk_id, record in chunk_records.items()
            if isinstance(record, dict)
        }
    if isinstance(chunk_records, list):
        return {
            str(row.get("chunk_id") or ""): row
            for row in chunk_records
            if isinstance(row, dict) and row.get("chunk_id")
        }
    return {}


def _bounded_understanding(
    propositions: list[dict[str, Any]],
    backgrounds: list[tuple[str, bool]],
    limit: int,
) -> tuple[dict[str, Any], bool]:
    """Build a bounded material-understanding block with explicit drop metadata."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    entries: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for proposition in propositions:
        statement = str(proposition.get("statement") or "").strip()
        if not statement:
            continue
        remaining = max(0, limit - used) if limit > 0 else len(statement)
        bounded, budget_cut = _bounded_text(statement, remaining)
        if not bounded:
            truncated = True
            break
        cut = bool(proposition.get("statement_truncated")) or budget_cut
        if cut:
            truncated = True
        used += len(bounded)
        entries.append({
            "kind": "proposition",
            "proposition_id": str(proposition.get("proposition_id") or ""),
            "statement": bounded,
            "question_function": str(proposition.get("question_function") or ""),
            "evidence_ceiling": str(proposition.get("evidence_ceiling") or ""),
            "statement_truncated": cut,
        })
        if limit > 0 and used >= limit:
            break
    for background, background_truncated in backgrounds:
        remaining = max(0, limit - used) if limit > 0 else len(background)
        bounded, budget_cut = _bounded_text(background, remaining)
        if not bounded:
            truncated = True
            break
        cut = background_truncated or budget_cut
        if cut:
            truncated = True
        used += len(bounded)
        entries.append({
            "kind": "background_context",
            "statement": bounded,
            "statement_truncated": cut,
        })
        if limit > 0 and used >= limit:
            break
    truncated = truncated or (
        limit > 0 and len(entries) < len(propositions) + len(backgrounds)
    )
    return {
        "entries": entries,
        "available_proposition_count": len(propositions),
        "included_proposition_count": sum(
            1 for entry in entries if entry["kind"] == "proposition"
        ),
        "available_background_count": len(backgrounds),
        "included_background_count": sum(
            1 for entry in entries if entry["kind"] == "background_context"
        ),
        "understanding_chars": used,
        "understanding_limit": limit,
        "understanding_truncated": truncated,
    }, truncated


def _evidence_source_dossier(
    chunk_id: str,
    unit: dict[str, Any] | None,
    chunk_record: dict[str, Any] | None,
    *,
    relation: dict[str, Any] | None = None,
    span: dict[str, Any] | None = None,
    context_limit: int = DEFAULT_DOSSIER_CONTEXT_LIMIT,
    understanding_limit: int = DEFAULT_DOSSIER_UNDERSTANDING_LIMIT,
    quote_limit: int = DEFAULT_DOSSIER_QUOTE_LIMIT,
) -> dict[str, Any]:
    """Build one bounded source dossier shared by claim and section layers."""
    relation = relation if isinstance(relation, dict) else {}
    span = span if isinstance(span, dict) else {}
    unit = unit if isinstance(unit, dict) else None
    chunk_record = chunk_record if isinstance(chunk_record, dict) else None

    identity: dict[str, Any] = {}
    durable: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    annotations: list[dict[str, Any]] = []
    if unit is not None:
        identity = unit.get("identity") if isinstance(unit.get("identity"), dict) else {}
        durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), dict) else {}
        card = unit.get("durable_content_card") if isinstance(unit.get("durable_content_card"), dict) else {}
        quality = card.get("content_quality") if isinstance(card.get("content_quality"), dict) else {}
        audit = unit.get("audit") if isinstance(unit.get("audit"), dict) else {}
        provenance = audit.get("source_provenance") if isinstance(audit.get("source_provenance"), dict) else {}
        annotations = [
            item for item in unit.get("query_annotations") or []
            if isinstance(item, dict)
        ]
    binding: dict[str, Any] = {}
    if chunk_record is not None:
        binding = (
            chunk_record.get("material_card_binding")
            if isinstance(chunk_record.get("material_card_binding"), dict)
            else {}
        )

    paper_id = str(
        identity.get("paper_id")
        or (chunk_record or {}).get("paper_id")
        or relation.get("paper_id")
        or span.get("paper_id")
        or ""
    )
    doi = str(identity.get("doi") or (chunk_record or {}).get("doi") or "")
    title = str(identity.get("title") or (chunk_record or {}).get("title") or "")
    locator = identity.get("locator") or (chunk_record or {}).get("locator") or {}
    if isinstance(locator, dict):
        locator = dict(locator)
    else:
        locator = {"label": compact(locator, 120)}

    exact_quote = str(
        span.get("quote")
        or span.get("exact_span")
        or span.get("verbatim_quote")
        or relation.get("exact_span")
        or relation.get("verbatim_quote")
        or relation.get("quote")
        or (chunk_record or {}).get("verbatim_quote")
        or (chunk_record or {}).get("exact_quote")
        or ""
    ).strip()
    try:
        quote_limit = max(0, int(quote_limit))
    except (TypeError, ValueError):
        quote_limit = 0
    quote_over_limit = bool(
        exact_quote and quote_limit > 0 and len(exact_quote) > quote_limit
    )

    raw_text = str(
        durable.get("raw_text")
        or durable.get("normalized_text")
        or (chunk_record or {}).get("raw_text")
        or (chunk_record or {}).get("text")
        or (chunk_record or {}).get("normalized_text")
        or ""
    )
    context_kind = "raw_source" if raw_text else "not_available"
    if not raw_text:
        preview = str((chunk_record or {}).get("text_preview") or "").strip()
        if preview:
            raw_text = preview
            context_kind = "text_preview"
    context, context_truncated = _bounded_text(raw_text, context_limit)

    permission = str(
        quality.get("evidence_ceiling")
        or provenance.get("use_permission")
        or span.get("permission_ceiling")
        or span.get("raw_use_permission")
        or relation.get("evidence_ceiling")
        or (chunk_record or {}).get("use_permission")
        or binding.get("evidence_ceiling")
        or "discovery_only"
    )
    if quality.get("evidence_ceiling"):
        permission_source = "material_content_quality"
    elif provenance.get("use_permission"):
        permission_source = "material_source_provenance"
    elif span.get("permission_ceiling") or span.get("raw_use_permission"):
        permission_source = "verified_span"
    elif (chunk_record or {}).get("use_permission"):
        permission_source = "chunk_record"
    else:
        permission_source = "default"

    limitations = clean_list(relation.get("limitations"), 6)
    if not limitations:
        limitations = clean_list(span.get("limitations"), 6)
    if not limitations:
        limitations = clean_list((chunk_record or {}).get("limitations"), 6)
    scope_fit = str(span.get("scope_fit") or relation.get("scope_fit") or "")

    propositions: list[dict[str, Any]] = []
    for annotation in annotations:
        for proposition in annotation.get("propositions") or []:
            if not isinstance(proposition, dict):
                continue
            statement, statement_truncated = _bounded_text(
                proposition.get("statement"), 260
            )
            if not statement:
                continue
            propositions.append({
                "proposition_id": str(proposition.get("proposition_id") or ""),
                "statement": statement,
                "statement_truncated": statement_truncated,
                "question_function": str(proposition.get("question_function") or ""),
                "evidence_ceiling": str(
                    (
                        proposition.get("evidence_permissions")
                        if isinstance(proposition.get("evidence_permissions"), dict)
                        else {}
                    ).get(chunk_id)
                    or proposition.get("weakest_evidence_ceiling")
                    or quality.get("evidence_ceiling")
                    or provenance.get("use_permission")
                    or "discovery_only"
                ),
            })
    if not propositions:
        for item in binding.get("propositions") or []:
            if not isinstance(item, dict):
                continue
            statement, statement_truncated = _bounded_text(item.get("statement"), 260)
            if not statement:
                continue
            propositions.append({
                "proposition_id": str(item.get("proposition_id") or ""),
                "statement": statement,
                "statement_truncated": statement_truncated,
                "question_function": str(item.get("question_function") or ""),
                "evidence_ceiling": str(item.get("evidence_ceiling") or ""),
            })
    backgrounds: list[tuple[str, bool]] = []
    for annotation in annotations:
        for background in annotation.get("background_contexts") or []:
            if isinstance(background, dict):
                statement, truncated = _bounded_text(background.get("statement"), 420)
                if statement:
                    backgrounds.append((statement, truncated))
    if not backgrounds:
        for item in binding.get("background_contexts") or []:
            if isinstance(item, dict):
                statement, truncated = _bounded_text(item.get("statement"), 420)
                if statement:
                    backgrounds.append((statement, truncated))
    material_understanding, understanding_truncated = _bounded_understanding(
        propositions, backgrounds, understanding_limit
    )

    return {
        "chunk_id": str(chunk_id or ""),
        "role": "",
        "material_unit_id": str(unit.get("unit_id") or "") if unit else "",
        "work_id": str(unit.get("work_id") or "") if unit else "",
        "material_available": unit is not None,
        "provenance_available": bool(paper_id or doi or title or locator),
        "paper_id": paper_id,
        "doi": doi,
        "title": title,
        "locator": locator,
        "relation_type": str(
            span.get("retrieval_role") or relation.get("relation_type") or ""
        ),
        "exact_quote": exact_quote,
        "quote_available": bool(exact_quote),
        "quote_chars": len(exact_quote),
        "quote_limit": quote_limit,
        "quote_over_limit": quote_over_limit,
        "context": context,
        "context_available": bool(context),
        "context_kind": context_kind if context else "not_available",
        "context_chars": len(context),
        "context_limit": context_limit,
        "context_truncated": context_truncated,
        "permission": permission,
        "permission_source": permission_source,
        "source_kind": str(
            quality.get("source_kind")
            or (chunk_record or {}).get("source_kind")
            or ""
        ),
        "content_depth": str(
            durable.get("content_depth")
            or provenance.get("content_depth")
            or (chunk_record or {}).get("content_depth")
            or ""
        ),
        "scope_fit": scope_fit,
        "limitations": limitations,
        "material_understanding": material_understanding,
    }


def build_claim_evidence_dossiers(
    claims: list[dict[str, Any]],
    material_units_by_chunk_id: dict[str, dict[str, Any]] | None = None,
    chunk_records: dict[str, dict[str, Any]] | list[dict[str, Any]] | None = None,
    *,
    per_claim_chunk_limit: int = DEFAULT_DOSSIER_PER_CLAIM_CHUNK_LIMIT,
    context_limit: int = DEFAULT_DOSSIER_CONTEXT_LIMIT,
    understanding_limit: int = DEFAULT_DOSSIER_UNDERSTANDING_LIMIT,
    quote_limit: int = DEFAULT_DOSSIER_QUOTE_LIMIT,
) -> list[dict[str, Any]]:
    """Build bounded, claim-centered evidence dossiers for planner/grounder input.

    Every dossier preserves the original claim statement and available exact
    quotes in full.  Surrounding source text and material understanding are
    bounded per claim/source with explicit limits and truncation metadata.
    Missing provenance, quotes, context, and material units are recorded as
    explicit flags instead of raising or fabricating values.
    """
    unit_map = (
        material_units_by_chunk_id
        if isinstance(material_units_by_chunk_id, dict)
        else {}
    )
    record_map = _chunk_record_map(chunk_records)
    if not isinstance(claims, list):
        claims = []
    try:
        per_claim_chunk_limit = max(0, int(per_claim_chunk_limit))
    except (TypeError, ValueError):
        per_claim_chunk_limit = DEFAULT_DOSSIER_PER_CLAIM_CHUNK_LIMIT

    dossiers: list[dict[str, Any]] = []
    for index, claim in enumerate(
        (item for item in claims if isinstance(item, dict)), start=1
    ):
        claim_id = str(claim.get("claim_id") or f"claim-{index:03d}")
        statement = str(claim.get("statement") or claim.get("claim_seed") or "").strip()
        relations = [
            item for item in (claim.get("evidence_relations") or [])
            if isinstance(item, dict)
        ]
        spans = [
            item for item in (claim.get("evidence_spans") or [])
            if isinstance(item, dict)
        ]
        source_roles: dict[str, dict[str, Any]] = {}

        def add_source(
            chunk_id: Any,
            role: str,
            relation: dict[str, Any] | None = None,
            span: dict[str, Any] | None = None,
        ) -> None:
            cid = str(chunk_id or "").strip()
            if not cid:
                return
            if cid in source_roles:
                # Enrich the first source record with later span/relation data
                # instead of dropping it (e.g. a verified span for a relation).
                existing = source_roles[cid]
                if existing.get("relation") is None and relation is not None:
                    existing["relation"] = relation
                if existing.get("span") is None and span is not None:
                    existing["span"] = span
                return
            source_roles[cid] = {"role": role, "relation": relation, "span": span}

        for relation in relations:
            add_source(relation.get("chunk_id"), "evidence_relation", relation=relation)
        for span in spans:
            add_source(span.get("chunk_id"), "evidence_span", span=span)
        for role, key in (
            ("supporting", "supporting_text_chunk_ids"),
            ("counterevidence", "counterevidence_text_chunk_ids"),
            ("boundary", "boundary_text_chunk_ids"),
            ("background", "background_text_chunk_ids"),
            ("author_reported_support", "author_reported_support_chunk_ids"),
        ):
            for cid in claim.get(key) or []:
                add_source(cid, role)

        sources: list[dict[str, Any]] = []
        excluded_chunk_ids: list[str] = []
        for cid in sorted(source_roles):
            if (
                per_claim_chunk_limit > 0
                and len(sources) >= per_claim_chunk_limit
            ):
                excluded_chunk_ids.append(cid)
                continue
            source = _evidence_source_dossier(
                cid,
                unit_map.get(cid),
                record_map.get(cid),
                relation=source_roles[cid].get("relation"),
                span=source_roles[cid].get("span"),
                context_limit=context_limit,
                understanding_limit=understanding_limit,
                quote_limit=quote_limit,
            )
            source["role"] = source_roles[cid]["role"]
            sources.append(source)

        dossiers.append({
            "dossier_id": f"claim-evidence-dossier:{claim_id}",
            "schema_version": "review_blueprint.claim_evidence_dossier.v1",
            "claim_id": claim_id,
            "original_claim": statement,
            "original_statement": str(claim.get("original_statement") or "").strip(),
            "supported_rewrite": str(claim.get("supported_rewrite") or "").strip(),
            "claim_state": str(claim.get("claim_state") or ""),
            "evidence_binding_status": str(claim.get("evidence_binding_status") or ""),
            "evidence_requirement": str(claim.get("evidence_requirement") or ""),
            "importance": str(claim.get("importance") or ""),
            "boundary_conditions": clean_list(claim.get("boundary_conditions"), 8),
            "missing_evidence_components": clean_list(
                claim.get("missing_evidence_components"), 8
            ),
            "sources": sources,
            "source_count": len(sources),
            "excluded_chunk_count": len(excluded_chunk_ids),
            "material_available": any(
                source.get("material_available") for source in sources
            ),
            "truncation": {
                "per_claim_chunk_limit": per_claim_chunk_limit,
                "excluded_chunk_ids": excluded_chunk_ids,
                "context_limit": context_limit,
                "context_truncated_sources": [
                    source["chunk_id"] for source in sources
                    if source.get("context_truncated")
                ],
                "understanding_limit": understanding_limit,
                "understanding_truncated_sources": [
                    source["chunk_id"] for source in sources
                    if (source.get("material_understanding") or {})
                    .get("understanding_truncated")
                ],
                "quote_limit": quote_limit,
                "quotes_over_limit": [
                    source["chunk_id"] for source in sources
                    if source.get("quote_over_limit")
                ],
            },
            "raw_text_policy": (
                "Raw text stays in the local evidence store; dossiers carry bounded "
                "context with explicit truncation metadata and reopen by chunk_id."
            ),
        })
    return dossiers


def build_section_evidence_material_layer(
    chunks: list[dict[str, Any]],
    material_units_by_chunk_id: dict[str, dict[str, Any]] | None = None,
    *,
    chunk_limit: int = DEFAULT_SECTION_DOSSIER_CHUNK_LIMIT,
    context_limit: int = DEFAULT_DOSSIER_CONTEXT_LIMIT,
    understanding_limit: int = DEFAULT_DOSSIER_UNDERSTANDING_LIMIT,
    quote_limit: int = DEFAULT_DOSSIER_QUOTE_LIMIT,
) -> dict[str, Any]:
    """Build a bounded section-level evidence material layer.

    This parallel layer carries the same source-dossier fields as claim
    dossiers (provenance, exact quote, context, permission, limitations, and
    material understanding) without replacing evidence_digest or the candidate
    pools.  Chunks excluded by the budget are recorded by ID instead of being
    silently dropped.
    """
    unit_map = (
        material_units_by_chunk_id
        if isinstance(material_units_by_chunk_id, dict)
        else {}
    )
    if not isinstance(chunks, list):
        chunks = []
    records = [
        row for row in chunks
        if isinstance(row, dict) and str(row.get("chunk_id") or "")
    ]
    records.sort(key=lambda row: str(row.get("chunk_id") or ""))
    try:
        chunk_limit = max(0, int(chunk_limit))
    except (TypeError, ValueError):
        chunk_limit = DEFAULT_SECTION_DOSSIER_CHUNK_LIMIT
    visible = records if chunk_limit <= 0 else records[:chunk_limit]
    excluded = records[chunk_limit:] if chunk_limit > 0 else []
    material_dossiers = [
        _evidence_source_dossier(
            str(row.get("chunk_id") or ""),
            unit_map.get(str(row.get("chunk_id") or "")),
            row,
            context_limit=context_limit,
            understanding_limit=understanding_limit,
            quote_limit=quote_limit,
        )
        for row in visible
    ]
    for dossier in material_dossiers:
        dossier["role"] = "section_candidate"
    return {
        "schema_version": "review_blueprint.evidence_material_layer.v1",
        "material_dossiers": material_dossiers,
        "material_dossier_count": len(material_dossiers),
        "excluded_chunk_count": len(excluded),
        "excluded_chunk_ids": [
            str(row.get("chunk_id") or "") for row in excluded
        ],
        "limits": {
            "chunk_limit": chunk_limit,
            "context_limit": context_limit,
            "understanding_limit": understanding_limit,
            "quote_limit": quote_limit,
        },
        "raw_text_policy": (
            "Raw text stays in the local evidence store; dossiers carry bounded "
            "context with explicit truncation metadata and reopen by chunk_id."
        ),
    }


def safe_json_parse(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    try:
        from json_repair import repair_json  # type: ignore

        value = repair_json(str(text or ""), return_objects=True)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def tokenize(value: Any, *, limit: int = 40) -> list[str]:
    text = str(value or "").lower()
    # Keep scientific symbols readable while avoiding fragile FTS syntax.
    raw = re.findall(r"[a-z][a-z0-9\-]{2,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip("-")
        if len(token) < 3 or token in STOPWORDS or token in seen:
            continue
        out.append(token)
        seen.add(token)
        if len(out) >= limit:
            break
    return out


def text_overlap_score(left: str, right: str) -> float:
    a = set(tokenize(left, limit=80))
    b = set(tokenize(right, limit=120))
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def read_sqlite_rows(db_path: Path, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return list(con.execute(sql, tuple(params)))
    finally:
        con.close()


def fts_or_like_rows(
    db_path: Path,
    *,
    fts_table: str,
    data_table: str,
    join_key: str,
    query_text: str,
    limit: int,
) -> list[sqlite3.Row]:
    """Retrieve rows from an FTS table, with a LIKE fallback.

    FTS5 query syntax is brittle for scientific strings.  This function keeps
    the retrieval layer boring and robust: OR a handful of safe tokens, then
    fall back to LIKE if the query parser refuses the expression.
    """

    tokens = tokenize(query_text, limit=10)
    if not tokens:
        return []
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        match_expr = " OR ".join(tokens)
        try:
            return list(
                con.execute(
                    f"""
                    SELECT d.*
                    FROM {fts_table} f
                    JOIN {data_table} d ON d.{join_key}=f.{join_key}
                    WHERE {fts_table} MATCH ?
                    LIMIT ?
                    """,
                    (match_expr, limit),
                )
            )
        except sqlite3.Error:
            like = "%" + "%".join(tokens[:4]) + "%"
            return list(
                con.execute(
                    f"""
                    SELECT *
                    FROM {data_table}
                    WHERE search_text LIKE ?
                    LIMIT ?
                    """,
                    (like, limit),
                )
            )
    finally:
        con.close()


@dataclass
class ConceptNode:
    node_id: str
    view_id: str
    view_name: str
    label: str
    purpose: str
    planning_value: str
    evidence_counts: dict[str, Any]
    raw: dict[str, Any]
    score: float = 0.0

    @property
    def evidence_weight(self) -> float:
        counts = self.evidence_counts or {}
        return (
            float(counts.get("paper_count") or 0) * 1.5
            + float(counts.get("text_chunk_count") or 0)
            + float(counts.get("visual_chunk_count") or 0) * 0.7
            + float(counts.get("high_utility_visual_count") or 0) * 1.2
        )

    @property
    def search_text(self) -> str:
        return " ".join([self.label, self.purpose, self.planning_value])


@dataclass
class EvidenceNetwork:
    """Tracks claim→chunk membership and identifies load-bearing chunks."""

    nodes: dict = field(default_factory=dict)        # claim_id → Claim dict
    chunk_to_claims: dict = field(default_factory=dict)  # chunk_id → list[claim_id]

    def add_claim(self, claim: dict) -> None:
        cid = claim.get("claim_id", "")
        if not cid:
            return
        self.nodes[cid] = claim
        all_chunk_ids = list(claim.get("supporting_text_chunk_ids") or []) + list(
            claim.get("supporting_visual_chunk_ids") or []
        )
        for chunk_id in all_chunk_ids:
            self.chunk_to_claims.setdefault(chunk_id, []).append(cid)
        # Mark load-bearing: chunk shared by ≥2 claims
        for chunk_id, claimers in self.chunk_to_claims.items():
            if len(claimers) >= 2:
                for shared_cid in claimers:
                    if shared_cid in self.nodes:
                        self.nodes[shared_cid]["load_bearing"] = True

    @property
    def load_bearing_chunks(self) -> set:
        return {cid for cid, claimers in self.chunk_to_claims.items() if len(claimers) >= 2}

    def to_summary(self) -> dict:
        return {
            "total_claims": len(self.nodes),
            "total_chunks_referenced": len(self.chunk_to_claims),
            "load_bearing_chunks": sorted(self.load_bearing_chunks),
            "load_bearing_claims": [
                cid for cid, c in self.nodes.items() if c.get("load_bearing")
            ],
        }


DEFAULT_ACTIVE_LIBRARY = (
    Path(__file__).resolve().parents[1]
    / "outputs"
    / "review_example_memory"
    / "final_canonical"
    / "intellectual_moves_active_by_category.json"
)

_M1_CATEGORIES = (
    "problem_reframing",
    "central_thesis",
    "taxonomy_design",
    "synthesis_moves",
    "section_progression",
    "paragraph_moves",
    "evidence_critique",
    "disagreement_handling",
    "gap_characterization",
    "figure_argument",
    "top_journal_publishability",
)


class DynamicReviewBlueprintPlanner:
    def __init__(
        self,
        concept_map_path: Path,
        output_dir: Path,
        *,
        user_question: str,
        problem_understanding: str,
        scope_definition: str,
        query_plan_path: Path | None = None,
        kb_dir: Path | None = None,
        material_units_path: Path | None = None,
        material_vectors_path: Path | None = None,
        material_embedding_model: str = "text-embedding-v4",
        review_example_memory_path: Path | None = None,
        active_library_path: Path | None = None,
        real_llm_plan: bool = True,
        enable_mentor: bool = True,
        real_llm_mentor: bool = False,
        mentor_three_party: bool = False,
        mentor_model_tier: str = "advanced_model",
        mentor_advice_path: Path | None = None,
        domain_config_path: Path | None = None,
        real_llm_claims: bool = False,
        real_llm_dag: bool = False,
        dag_candidate_mode: str | None = None,
        dag_topk_per_target: int = 5,
        dag_max_layer4_candidates: int | None = 80,
        m3_real_gap_rounds: int = 0,
        m3_real_max_claims: int = 3,
        m3_real_saturation_threshold: float = 1.5,
        m3_real_output_dir: Path | None = None,
        m3_real_metadata_db: Path | None = None,
        m3_real_topic_context: str = "",
        m3_real_max_queries: int = 3,
        m3_real_results_per_backend: int = 10,
        m3_real_top_k: int = 5,
        m3_real_from_year: int | None = None,
        m3_real_download_top_n: int = 2,
        m3_real_citation_chase_top_n: int = 2,
        m3_real_references_per_seed: int = 8,
        m3_adaptive_closure: bool = True,
        s2_first_enabled: bool = True,
        s2_literature_graph_path: Path | None = None,
        planner_prompt_path: Path = DEFAULT_PLANNER_PROMPT,
        planner_model_tier: str = "premium_model",
        planner_max_tokens: int = 9600,
        planner_architecture_path: Path | None = None,
        force_reground: bool = False,
        max_sections: int = 10,
        min_sections: int = 8,
        served_text_limit: int | None = DEFAULT_SERVED_TEXT_LIMIT,
        model_text_context_limit: int | None = None,
        evidence_batch_size: int = 12,
        retrieval_limit_per_query: int = DEFAULT_RETRIEVAL_LIMIT_PER_QUERY,
        retrieval_max_total: int = DEFAULT_RETRIEVAL_MAX_TOTAL,
        visual_retrieval_max_total: int = DEFAULT_VISUAL_RETRIEVAL_MAX_TOTAL,
    ) -> None:
        self.concept_map_path = concept_map_path
        self.output_dir = output_dir
        self.user_question = user_question
        self.problem_understanding = problem_understanding
        self.scope_definition = scope_definition
        self.query_plan_path = Path(query_plan_path) if query_plan_path else None
        self.kb_dir = kb_dir
        self.material_units_path = Path(material_units_path) if material_units_path else None
        self.material_vectors_path = Path(material_vectors_path) if material_vectors_path else None
        self.material_embedding_model = str(material_embedding_model or "text-embedding-v4")
        self.material_units: list[dict[str, Any]] = []
        self.material_units_by_chunk_id: dict[str, dict[str, Any]] = {}
        self.review_example_memory_path = review_example_memory_path
        self.active_library_path = Path(active_library_path) if active_library_path else (
            DEFAULT_ACTIVE_LIBRARY if DEFAULT_ACTIVE_LIBRARY.exists() else None
        )
        self.real_llm_plan = real_llm_plan
        self.enable_mentor = enable_mentor
        self.real_llm_mentor = real_llm_mentor
        self.mentor_three_party = bool(mentor_three_party)
        self.mentor_model_tier = mentor_model_tier
        self.mentor_advice_path = Path(mentor_advice_path) if mentor_advice_path else None
        self.domain_config_path = Path(domain_config_path) if domain_config_path else None
        self.real_llm_claims = real_llm_claims
        self.real_llm_dag = real_llm_dag
        self.dag_candidate_mode = dag_candidate_mode
        self.dag_topk_per_target = dag_topk_per_target
        self.dag_max_layer4_candidates = dag_max_layer4_candidates
        self.m3_real_gap_rounds = max(0, int(m3_real_gap_rounds))
        self.m3_real_max_claims = max(1, int(m3_real_max_claims))
        self.m3_real_saturation_threshold = float(m3_real_saturation_threshold)
        self.m3_real_output_dir = Path(m3_real_output_dir) if m3_real_output_dir else None
        self.m3_real_metadata_db = Path(m3_real_metadata_db) if m3_real_metadata_db else None
        self.m3_real_topic_context = m3_real_topic_context
        self.m3_real_max_queries = max(1, int(m3_real_max_queries))
        self.m3_real_results_per_backend = max(1, int(m3_real_results_per_backend))
        self.m3_real_top_k = max(1, int(m3_real_top_k))
        self.m3_real_from_year = int(m3_real_from_year) if m3_real_from_year else None
        self.m3_real_download_top_n = max(0, int(m3_real_download_top_n))
        self.m3_real_citation_chase_top_n = max(0, int(m3_real_citation_chase_top_n))
        self.m3_real_references_per_seed = max(1, int(m3_real_references_per_seed))
        self.m3_adaptive_closure = bool(m3_adaptive_closure)
        self.s2_first_enabled = bool(s2_first_enabled)
        self.s2_literature_graph_path = (
            Path(s2_literature_graph_path) if s2_literature_graph_path else None
        )
        self.planner_prompt_path = planner_prompt_path
        self.planner_model_tier = planner_model_tier
        self.planner_max_tokens = max(2400, int(planner_max_tokens))
        self.planner_architecture_path = Path(planner_architecture_path) if planner_architecture_path else None
        # Explicit force-reground: reuse the Qwen chapter division but rebuild
        # every section's evidence grounding/candidate pool from the current
        # material library and vector cache.  Default False keeps normal
        # checkpoint reuse unchanged.
        self.force_reground = bool(force_reground)
        # min/max are the authoritative Python-enforced chapter-count contract.
        # Qwen owns WHICH 8-10 chapters and their scientific organization;
        # Python rejects every architecture outside the range and never
        # invents a deterministic outline to repair the count.
        self.max_sections = max(4, min(12, max_sections))
        self.min_sections = max(3, min(self.max_sections, min_sections))
        # Keep a broad, reopenable section pool and expose that pool to the
        # claim planner.  The previous default exposed only 90 chunks, which
        # silently collapsed richer sections.  ``None`` preserves every
        # retrieved/served unique candidate; an explicit positive limit remains
        # available for deliberately constrained runs and is always audited.
        try:
            parsed_served_limit = int(served_text_limit)
        except (TypeError, ValueError):
            parsed_served_limit = 0
        self.served_text_limit = (
            max(14, parsed_served_limit) if parsed_served_limit > 0 else None
        )
        if model_text_context_limit is None or int(model_text_context_limit) <= 0:
            self.model_text_context_limit = self.served_text_limit
        else:
            explicit_context_limit = max(4, int(model_text_context_limit))
            self.model_text_context_limit = (
                explicit_context_limit
                if self.served_text_limit is None
                else min(self.served_text_limit, explicit_context_limit)
            )
        self.evidence_batch_size = max(4, int(evidence_batch_size))
        self.retrieval_limit_per_query = max(1, int(retrieval_limit_per_query))
        self.retrieval_max_total = max(1, int(retrieval_max_total))
        self.visual_retrieval_max_total = max(
            1, int(visual_retrieval_max_total)
        )
        self.concept_map: dict[str, Any] = {}
        self.review_example_memory: dict[str, Any] = {}
        self.active_library: dict[str, list] = {}
        self.s2_literature_graph: dict[str, Any] = {}
        self.concept_nodes: list[ConceptNode] = []
        self.db_path: Path | None = None

    def build(self) -> dict[str, Any]:
        self.concept_map = load_json(self.concept_map_path)
        self.review_example_memory = load_json(self.review_example_memory_path) if self.review_example_memory_path else {}
        self.active_library = load_json(self.active_library_path) if self.active_library_path else {}
        if self.material_units_path and self.material_units_path.exists():
            material_payload = load_json(self.material_units_path)
            self.material_units = [
                dict(unit) for unit in material_payload.get("units") or []
                if isinstance(unit, dict)
            ]
            self.material_units_by_chunk_id = {
                str((unit.get("identity") or {}).get("chunk_id") or ""): unit
                for unit in self.material_units
                if isinstance(unit.get("identity"), dict)
                and str((unit.get("identity") or {}).get("chunk_id") or "")
            }
        self.s2_literature_graph = (
            load_json(self.s2_literature_graph_path)
            if self.s2_literature_graph_path
            else {}
        )
        self.db_path = self._resolve_db_path()
        self.concept_nodes = self._load_concept_nodes()
        planning_evidence = self._build_planning_evidence()
        mentor_advice = self._build_review_mentor_advice(planning_evidence)
        if mentor_advice:
            planning_evidence["review_mentor_advice"] = self._compact_mentor_for_planner(mentor_advice)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.output_dir / "review_mentor_advice.json", mentor_advice)
        sections, planner_output, planner_used = self._resolve_architecture(
            planning_evidence
        )
        # A real architect has already chosen a question-specific narrative.
        # Re-sorting it into a universal physics/material/application template
        # would erase that reasoning.  Physics-first remains a deterministic
        # fallback only when no usable LLM architecture exists.
        sections = self._enforce_physics_first_order(sections, preserve_planner_order=planner_used)
        sections, seed_axis_coverage = self._ensure_seed_axis_coverage(sections)
        sections = self._attach_transitions(
            sections, fill_missing_handoffs=not planner_used
        )
        sections = self._attach_mentor_advice_to_sections(sections, mentor_advice)
        sections = self._attach_section_workplan_context(sections)
        # M2a: decompose each section into 3-5 claims
        sections, evidence_network = self._decompose_claims(sections)
        # M2a.5: turn each planning claim into an auditable evidence contract.
        # This ranks existing material without promoting it to final evidence.
        sections, argument_quality = self._attach_argument_quality(sections)
        # M2b: build argument DAG over M2a claims
        sections, argument_dag = self._build_argument_dag(sections)
        sections = self._attach_claim_evidence_dossiers(sections)
        planner_output_status = self._planner_output_status(planner_output, sections, planner_used)
        candidate_pool_audit = self._section_candidate_pool_audit(sections)
        planner_gap_seeds = self._planner_gap_seeds(planner_output)
        blueprint = {
            "schema_version": "dynamic_review_blueprint.v4",
            "created_at": utc_now(),
            "planning_mode": (
                "llm_required_dynamic"
                if planner_used
                else "deterministic_fallback_non_production"
            ),
            "production_blueprint": bool(planner_used),
            "non_production_fallback": not planner_used,
            "authoritative_section_count_range": [
                self.min_sections,
                self.max_sections,
            ],
            "planner_output_status": planner_output_status,
            "candidate_pool_audit": candidate_pool_audit,
            "source_concept_map": str(self.concept_map_path),
            "source_review_knowledge_base": str(self.db_path or ""),
            "source_material_units": str(self.material_units_path or ""),
            "source_review_example_memory": str(self.review_example_memory_path or ""),
            "review_example_status": {
                "enabled": bool(self.review_example_memory),
                "record_count": self.review_example_memory.get("record_count", 0),
                "usage_rule": "Structural reference only; not scientific evidence.",
                "m1_active_library_enabled": bool(self.active_library),
                "m1_active_library_path": str(self.active_library_path or ""),
            },
            "input_context": {
                "user_question": self.user_question,
                "problem_understanding": self.problem_understanding,
                "scope_definition": self.scope_definition,
                "query_plan_path": str(self.query_plan_path.resolve()) if self.query_plan_path else "",
            },
            "planning_evidence_brief": planning_evidence,
            "review_thesis": compact(planner_output.get("review_thesis"), 1200) if planner_used else self._dynamic_review_thesis(planning_evidence),
            "narrative_strategy": compact(planner_output.get("narrative_strategy"), 900)
            if planner_used
            else self._dynamic_narrative_strategy(planning_evidence),
            "review_example_structure_anchor": self._review_example_anchor(),
            "review_mentor_advice": mentor_advice,
            "quality_bar": [
                "Generate the outline from retrieved concepts, text chunks, visual chunks, and review-structure anchors; do not reuse a fixed section template.",
                "Each section must state what intellectual work it performs in the review.",
                "Each section must expose traceable concept, text, and visual anchors so later agents can bind claims to evidence.",
                "Visual assets must be selected for argument value, not merely because they are attractive figures.",
                "Supplemental searches must be capped at high-marginal-value gaps that materially change the review.",
            ],
            "sections": sections,
            "evidence_network": evidence_network.to_summary(),
            "argument_dag": argument_dag.to_dict(),
            "argument_quality": argument_quality,
            "argument_craft_recipe": self._argument_craft_recipe(sections, argument_quality),
            "evidence_material_layer": {
                "schema_version": "review_blueprint.evidence_material_layer.v1",
                "section_count": len(sections),
                "claim_dossier_count": sum(
                    len(section.get("claim_evidence_dossiers") or [])
                    for section in sections
                ),
                "material_dossier_count": sum(
                    len(
                        (section.get("evidence_material_layer") or {})
                        .get("material_dossiers") or []
                    )
                    for section in sections
                ),
                "limits": {
                    "per_claim_chunk_limit": DEFAULT_DOSSIER_PER_CLAIM_CHUNK_LIMIT,
                    "section_chunk_limit": DEFAULT_SECTION_DOSSIER_CHUNK_LIMIT,
                    "context_limit": DEFAULT_DOSSIER_CONTEXT_LIMIT,
                    "understanding_limit": DEFAULT_DOSSIER_UNDERSTANDING_LIMIT,
                    "quote_limit": DEFAULT_DOSSIER_QUOTE_LIMIT,
                },
                "raw_text_policy": (
                    "Raw text stays in the local evidence store; dossiers carry "
                    "bounded context with explicit truncation metadata."
                ),
                "layer_policy": (
                    "Parallel to candidate_evidence_digest and candidate_material_pool; "
                    "source-verification acceptance rules are unchanged."
                ),
            },
            "claim_decomposition_status": {
                "sections_processed": len(sections),
                "real_llm_claims": self.real_llm_claims,
                "real_llm_dag": self.real_llm_dag,
                "dag_candidate_mode": self.dag_candidate_mode,
                "candidate_claim_pool_enabled": bool(self.real_llm_claims),
                "sections_with_candidate_claim_pool": sum(
                    bool(section.get("candidate_claim_pool"))
                    for section in sections
                ),
                "claim_decomposer_model_tier": CLAIM_DECOMPOSER_MODEL_TIER,
                "active_library_loaded": bool(self.active_library),
                "active_library_path": str(self.active_library_path or ""),
                "mentor_enabled": bool(self.enable_mentor),
                "mentor_mode": mentor_advice.get("mode", "disabled") if isinstance(mentor_advice, dict) else "disabled",
            },
            "global_argument_map": self._global_argument_map(sections),
            "global_visual_strategy": self._global_visual_strategy(sections),
            "scope_coverage_status": {
                **(
                    planner_output.get("_scope_coverage_repair", {})
                    if isinstance(planner_output.get("_scope_coverage_repair"), dict)
                    else {}
                ),
                "seed_axis_coverage": seed_axis_coverage,
            },
            "high_value_gap_seeds": planner_gap_seeds
            if planner_used and planner_gap_seeds
            else self._dynamic_gap_seeds(planning_evidence, sections),
            "integrated_refinement_status": {
                "sidecar_refinement_removed": True,
                "reason": "Planning decisions are written directly into section titles, roles, questions, evidence anchors, and gap seeds.",
            },
        }
        if self.m3_real_gap_rounds > 0:
            blueprint = self._run_m3_real_gap_loop(blueprint)
        validation = self._validate(blueprint)
        blueprint["admission_decision"] = validation["admission_decision"]
        blueprint["admission"] = validation["admission"]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        blueprint_path = self.output_dir / "review_blueprint.v4.dynamic.json"
        validation_path = self.output_dir / "review_blueprint.validation.json"
        markdown_path = self.output_dir / "review_blueprint.v4.dynamic.md"
        write_json(blueprint_path, blueprint)
        write_json(validation_path, validation)
        write_text(markdown_path, self._markdown(blueprint, validation))
        return {
            "blueprint": blueprint,
            "validation": validation,
            "paths": {
                "blueprint": str(blueprint_path),
                "validation": str(validation_path),
                "markdown": str(markdown_path),
            },
        }

    def _attach_argument_quality(
        self, sections: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from optomind_research.runtime.blueprint_argument_quality import (
            build_claim_evidence_contracts,
        )

        return build_claim_evidence_contracts(sections, self.material_units_by_chunk_id)

    @staticmethod
    def _argument_craft_recipe(
        sections: list[dict[str, Any]], quality: dict[str, Any]
    ) -> dict[str, Any]:
        """Expose the planner's repeatable synthesis method for downstream stages."""
        return {
            "name": "claim-centered evidence triangulation",
            "steps": [
                "Preserve every user-question axis, allowing one section to carry multiple axes.",
                "Give each section one intellectual job and connect it to the next necessary question.",
                "Break the section into load-bearing and supporting claims before prose is written.",
                "Rank proposition-bound candidates by semantic fit, permission, and independent-paper diversity.",
                "Pair each important claim with a counterevidence query, boundary conditions, and a distinguishing test.",
                "Send only unresolved high-value contracts to the ordered follow-up retrieval loop.",
                "Allow the evidence verifier to narrow, reframe, or leave a claim open instead of forcing a conclusion.",
            ],
            "why_this_is_distinctive": "The output is an auditable argument plan, not an outline plus an unordered bibliography.",
            "candidate_contract_coverage": quality.get("candidate_coverage_ratio", 0.0),
            "factual_candidate_readiness": quality.get("factual_candidate_ready_ratio", 0.0),
            "section_count": len(sections),
            "writing_gate": "Do not call the blueprint writing-ready until final source-span verification binds the claims.",
        }

    def _build_review_mentor_advice(self, planning_evidence: dict[str, Any]) -> dict[str, Any]:
        """Run M1.5 mentor advice or load a precomputed advice JSON.

        The advice is planning guidance only. It must not be treated as citable
        scientific evidence.
        """
        if not self.enable_mentor:
            return {}
        if self.mentor_advice_path and self.mentor_advice_path.exists():
            loaded = load_json(self.mentor_advice_path)
            # Accept either a standalone mentor artifact or a prior blueprint
            # that embeds the same artifact.  This makes expensive mentor work
            # reusable across planner retries without manual file surgery.
            if isinstance(loaded.get("review_mentor_advice"), dict):
                loaded = dict(loaded["review_mentor_advice"])
            if loaded:
                loaded.setdefault("mode", "loaded_from_file")
                loaded.setdefault("source_path", str(self.mentor_advice_path))
                return loaded
        if not self.active_library:
            return {}
        try:
            from optomind_research.review_mentor_agent import ReviewMentorAgent

            agent = ReviewMentorAgent(
                active_library_path=self.active_library_path or DEFAULT_ACTIVE_LIBRARY,
                prompt_path=DEFAULT_REVIEW_MENTOR_PROMPT,
                model_tier=self.mentor_model_tier,
                real_llm=self.real_llm_mentor,
                use_vector_index=True,
                use_three_party=self.mentor_three_party,
                domain_config_path=self.domain_config_path,
            )
            return agent.build_advice(
                user_question=self.user_question,
                problem_understanding=self.problem_understanding,
                scope_definition=self.scope_definition,
                planning_evidence=planning_evidence,
            )
        except Exception as exc:
            return {
                "schema_version": "review_mentor_advice.v1",
                "created_at": utc_now(),
                "mode": "error",
                "mentor_summary": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _compact_mentor_for_planner(mentor_advice: dict[str, Any]) -> dict[str, Any]:
        if not mentor_advice:
            return {}
        convergence = mentor_advice.get("three_party_convergence") if isinstance(mentor_advice.get("three_party_convergence"), dict) else {}
        compacted = {
            "mentor_summary": compact(mentor_advice.get("mentor_summary"), 700),
            "usable_intellectual_moves": [
                {
                    "category": compact(x.get("category"), 80),
                    "borrowed_pattern": compact(x.get("borrowed_pattern"), 240),
                    "adaptation_for_this_review": compact(x.get("adaptation_for_this_review"), 260),
                }
                for x in (mentor_advice.get("usable_intellectual_moves") or [])[:8]
                if isinstance(x, dict)
            ],
            "m2a_claim_decomposition_advice": clean_list(mentor_advice.get("m2a_claim_decomposition_advice"), 5),
            "m2b_argument_dag_advice": clean_list(mentor_advice.get("m2b_argument_dag_advice"), 5),
            "m3_gap_resolution_advice": clean_list(mentor_advice.get("m3_gap_resolution_advice"), 5),
            "visual_argument_advice": clean_list(mentor_advice.get("visual_argument_advice"), 5),
            "quality_risks": clean_list(mentor_advice.get("quality_risks"), 5),
            "three_party_convergence": {
                "architect_proposal_summary": compact(convergence.get("architect_proposal_summary"), 700),
                "section_proposals": list(convergence.get("section_proposals") or [])[:8],
                "critic_perspective_gaps": clean_list(convergence.get("critic_perspective_gaps"), 5),
                "adopted_suggestions": clean_list(convergence.get("adopted_suggestions"), 6),
                "rejected_suggestions": clean_list(convergence.get("rejected_suggestions"), 6),
                "final_blueprint_changes": list(convergence.get("final_blueprint_changes") or [])[:8],
                "rounds_completed": int(convergence.get("rounds_completed") or 0),
            } if convergence else {},
        }
        if mentor_advice.get("m1_case_moves") is not None:
            compacted["m1_case_moves"] = _m1_case_moves_payload(mentor_advice)
        return compacted

    def _attach_mentor_advice_to_sections(
        self,
        sections: list[dict[str, Any]],
        mentor_advice: dict[str, Any],
    ) -> list[dict[str, Any]]:
        compact_advice = self._compact_mentor_for_planner(mentor_advice)
        if not compact_advice:
            return sections
        for section in sections:
            section["review_mentor_advice"] = {
                "mentor_summary": compact_advice.get("mentor_summary", ""),
                "m2a_claim_decomposition_advice": compact_advice.get("m2a_claim_decomposition_advice", []),
                "m2b_argument_dag_advice": compact_advice.get("m2b_argument_dag_advice", []),
                "m3_gap_resolution_advice": compact_advice.get("m3_gap_resolution_advice", []),
                "visual_argument_advice": compact_advice.get("visual_argument_advice", []),
                "quality_risks": compact_advice.get("quality_risks", []),
            }
        return sections

    def _run_m3_real_gap_loop(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        try:
            out_dir = self.m3_real_output_dir or (self.output_dir / "m3_real_gap_loop")
            metadata_db = self.m3_real_metadata_db or (out_dir / "m3_real_gap_metadata.sqlite")
            if self.s2_first_enabled and self.db_path:
                from optomind_research.s2_m3_gap_loop import S2M3GapLoop

                updated, report, literature_graph = S2M3GapLoop(
                    kb_sqlite=self.db_path
                ).run(
                    blueprint,
                    max_rounds=self.m3_real_gap_rounds,
                    max_claims=self.m3_real_max_claims,
                    results_per_query=self.m3_real_results_per_backend,
                    snippets_per_query=max(
                        self.m3_real_top_k, self.m3_real_results_per_backend
                    ),
                    saturation_threshold=self.m3_real_saturation_threshold,
                )
                out_dir.mkdir(parents=True, exist_ok=True)
                write_json(out_dir / "m3_real_gap_loop_report.json", report)
                write_json(
                    out_dir / "s2_literature_graph.json",
                    literature_graph.to_dict(),
                )
                updated["s2_literature_graph_context"] = {
                    "summary": literature_graph.summary(),
                    "historical_lineage": literature_graph.historical_lineage(),
                    "research_branches": literature_graph.research_branches(),
                    "path": str(out_dir / "s2_literature_graph.json"),
                }
            else:
                from optomind_research.m3_real_gap_loop import run_m3_real_gap_loop

                updated, report = run_m3_real_gap_loop(
                    blueprint,
                    output_dir=out_dir,
                    metadata_db=metadata_db,
                    max_rounds=self.m3_real_gap_rounds,
                    max_claims=self.m3_real_max_claims,
                    saturation_threshold=self.m3_real_saturation_threshold,
                    max_queries=self.m3_real_max_queries,
                    results_per_backend=self.m3_real_results_per_backend,
                    top_k=self.m3_real_top_k,
                    from_year=self.m3_real_from_year,
                    download_top_n=self.m3_real_download_top_n,
                    citation_chase_top_n=self.m3_real_citation_chase_top_n,
                    references_per_seed=self.m3_real_references_per_seed,
                    topic_context=self.m3_real_topic_context,
                    kb_sqlite=self.db_path,
                    domain_config_path=self.domain_config_path,
                    adaptive_closure=self.m3_adaptive_closure,
                )
            updated["m3_real_gap_loop_report"] = report
            updated["m3_real_gap_loop_status"] = {
                "enabled": True,
                "mode": (
                    "s2_first"
                    if self.s2_first_enabled and self.db_path
                    else "legacy_fallback"
                ),
                "report_path": str(out_dir / "m3_real_gap_loop_report.json"),
                "metadata_db": str(metadata_db),
                "kb_feedback_enabled": bool(self.db_path),
            }
            # New evidence changes claim readiness and therefore graph quality.
            # Rebuild the evidence network and DAG only when M3 actually wrote
            # chunks, avoiding an expensive no-op LLM pass.
            ingested = any(
                ((row.get("kb_ingest") or {}).get("new_chunk_ids") or [])
                for row in (report.get("round_reports") or [])
                if isinstance(row, dict)
            )
            if report.get("schema_version") == "s2_m3_gap_loop.v1":
                ingested = bool(
                    (report.get("summary") or {}).get("accepted_chunks")
                )
            closure_changed = bool(report.get("adaptive_closure"))
            if ingested or closure_changed:
                refreshed_sections, refreshed_dag = self._build_argument_dag(updated.get("sections") or [])
                updated["sections"] = refreshed_sections
                network = EvidenceNetwork()
                for section in refreshed_sections:
                    for claim in (section.get("claims") or []):
                        network.add_claim(claim)
                updated["evidence_network"] = network.to_summary()
                updated["argument_dag"] = refreshed_dag.to_dict()
                updated["m3_real_gap_loop_status"]["downstream_graph_refreshed"] = True
                updated["m3_real_gap_loop_status"]["graph_refresh_reasons"] = {
                    "new_kb_chunks": bool(ingested),
                    "adaptive_claim_closure": bool(closure_changed),
                }
            return updated
        except Exception as exc:
            blueprint["m3_real_gap_loop_status"] = {
                "enabled": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return blueprint

    def _decompose_claims(
        self, sections: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], "EvidenceNetwork"]:
        """Run M2a claim decomposition on all sections. Returns (sections, network)."""
        from optomind_research.claim_decomposer import ClaimDecomposer, DEFAULT_DECOMPOSER_PROMPT

        network = EvidenceNetwork()

        def new_decomposer() -> ClaimDecomposer:
            """Per-section instance so last_audit cannot cross-contaminate."""
            decomposer = ClaimDecomposer(
                prompt_path=DEFAULT_DECOMPOSER_PROMPT,
                model_tier=CLAIM_DECOMPOSER_MODEL_TIER,
                real_llm=self.real_llm_claims,
                claim_pool_enabled=True if self.real_llm_claims else False,
                verify_candidate_pool_claims=False,
            )
            if self.real_llm_claims:
                decomposer._load_prompt()
            return decomposer

        def decompose_one(index_and_section: tuple[int, dict[str, Any]]):
            index, section = index_and_section
            claims = new_decomposer().decompose_section(section)
            return index, claims

        claim_batches: list[list[Any]] = [None] * len(sections)
        progress_path = (
            self.output_dir / "review_blueprint_claim_pool_progress.json"
        )
        progress_created_at = utc_now()

        def write_claim_pool_progress() -> None:
            """Coordinator-thread-only progress write (never from workers)."""
            rows: list[dict[str, Any]] = []
            completed_ids: list[str] = []
            for index, section in enumerate(sections):
                if claim_batches[index] is None:
                    continue
                section_id = str(
                    section.get("section_id") or f"S{index + 1:02d}"
                )
                completed_ids.append(section_id)
                pool_audit = dict(
                    section.get("candidate_claim_pool_audit") or {}
                )
                pool = section.get("candidate_claim_pool") or {}
                stored_count = int(
                    pool_audit.get("stored_pool_count")
                    or len(pool.get("claims") or [])
                )
                rows.append({
                    "section_id": section_id,
                    "stored_pool_claim_count": stored_count,
                    "final_selected_claim_count": len(claim_batches[index]),
                    "candidate_pool_audit": pool_audit,
                    "candidate_claim_pool_shortlist_audit": dict(
                        section.get("candidate_claim_pool_shortlist_audit")
                        or {}
                    ),
                })
            write_json(progress_path, {
                "created_at": progress_created_at,
                "updated_at": utc_now(),
                "total_sections": len(sections),
                "completed_count": len(rows),
                "remaining_count": len(sections) - len(rows),
                "completed_section_ids": completed_ids,
                "sections": rows,
            })

        write_claim_pool_progress()
        if self.real_llm_claims and len(sections) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(sections))) as pool:
                futures = [
                    pool.submit(decompose_one, (index, section))
                    for index, section in enumerate(sections)
                ]
                for future in as_completed(futures):
                    index, claims = future.result()
                    claim_batches[index] = claims
                    write_claim_pool_progress()
        else:
            for index, section in enumerate(sections):
                _, claims = decompose_one((index, section))
                claim_batches[index] = claims
                write_claim_pool_progress()

        for section, claims in zip(sections, claim_batches):
            claim_dicts = [c.to_dict() for c in claims]
            section["claims"] = claim_dicts
            for c in claim_dicts:
                network.add_claim(c)
        return sections, network

    def _build_argument_dag(
        self, sections: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], "ArgumentDAG"]:
        """Run M2b: build an ArgumentDAG over all M2a claims. Returns (sections, dag)."""
        from optomind_research.argument_dag_builder import ArgumentDAGBuilder

        # Build section_meta for enriched LLM payloads
        section_meta: dict[str, dict] = {}
        for s in sections:
            text_chunks = s.get("candidate_text_chunks") or []
            visual_chunks = s.get("candidate_visual_chunks") or []
            section_meta[s["section_id"]] = {
                "title": s.get("title", ""),
                "argument_role": s.get("argument_role", ""),
                "review_mentor_advice": s.get("review_mentor_advice", {}),
                "text_chunk_map": {
                    c.get("chunk_id", ""): c.get("text_preview", "")
                    for c in text_chunks if isinstance(c, dict) and c.get("chunk_id")
                },
                "text_chunk_meta": {
                    c.get("chunk_id", ""): dict(c)
                    for c in text_chunks if isinstance(c, dict) and c.get("chunk_id")
                },
                "visual_chunk_map": {
                    v.get("chunk_id", ""): v
                    for v in visual_chunks if isinstance(v, dict) and v.get("chunk_id")
                },
            }

        all_claims: list[dict[str, Any]] = []
        for s in sections:
            for c in (s.get("claims") or []):
                c.setdefault("section_id", s["section_id"])
                all_claims.append(c)
        section_order = [s["section_id"] for s in sections]

        builder = ArgumentDAGBuilder(
            real_llm=self.real_llm_dag,
            model_tier="advanced_model",
            candidate_mode=self.dag_candidate_mode,
            topk_per_target=self.dag_topk_per_target,
            max_layer4_candidates=self.dag_max_layer4_candidates,
        )
        dag = builder.build(
            all_claims,
            section_order,
            section_meta=section_meta,
            enable_scope_check=True,
            enable_readiness_check=True,
            scope_definition=" ".join(
                x for x in (self.user_question, self.problem_understanding, self.scope_definition) if x
            ),
        )
        dag.propagate_saturation()
        for s in sections:
            for c in (s.get("claims") or []):
                if c["claim_id"] in dag.claims_registry:
                    c.update(dag.claims_registry[c["claim_id"]])
        return sections, dag

    def _attach_claim_evidence_dossiers(
        self, sections: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach bounded per-claim dossiers and a section material layer.

        This is a parallel input layer for verified blueprint expression: it
        preserves the original claim, source provenance, exact quotes, bounded
        context, permission ceilings, scope limitations, and material
        understanding without altering evidence_digest, candidate pools, or
        source-verification acceptance rules.
        """
        for section in sections:
            claims = [
                claim for claim in (section.get("claims") or [])
                if isinstance(claim, dict)
            ]
            chunk_records = [
                row for row in (section.get("candidate_text_chunks") or [])
                if isinstance(row, dict)
            ]
            section["claim_evidence_dossiers"] = build_claim_evidence_dossiers(
                claims,
                self.material_units_by_chunk_id,
                chunk_records=chunk_records,
            )
            section["evidence_material_layer"] = build_section_evidence_material_layer(
                chunk_records,
                self.material_units_by_chunk_id,
            )
        return sections

    def _planner_output_status(self, planner_output: dict[str, Any], sections: list[dict[str, Any]], planner_used: bool) -> dict[str, Any]:
        raw_sections = planner_output.get("sections") if isinstance(planner_output.get("sections"), list) else []
        deterministic_completion = 0
        llm_grounded = 0
        architecture_contract_warnings = list(
            planner_output.get("_architecture_contract_warnings") or []
        )
        for section in sections:
            planner = (section.get("generated_from") or {}).get("planner")
            if planner == "llm_integrated_dynamic":
                llm_grounded += 1
            if planner == "deterministic_completion_after_partial_llm":
                deterministic_completion += 1
        count_ok = (
            self.min_sections <= len(sections) <= self.max_sections
        )
        admission_decision = (
            "admit" if (planner_used and count_ok) else "reject"
        )
        return {
            "real_llm_requested": self.real_llm_plan,
            "non_production_fallback": not self.real_llm_plan,
            "admission_decision": admission_decision,
            "admission": {
                "decision": admission_decision,
                "production": bool(planner_used),
                "non_production_deterministic": not planner_used,
                "qwen_architecture_present": bool(planner_used),
                "section_count": len(sections),
                "section_count_range": [
                    self.min_sections,
                    self.max_sections,
                ],
                "note": (
                    "Final S7-S9 gating must read blueprint.admission_decision "
                    "after build-time validation; this status is the "
                    "pre-validation signal."
                ),
            },
            "recommended_section_range": (
                f"{self.min_sections}-{self.max_sections}"
            ),
            "enforced_section_range": [self.min_sections, self.max_sections],
            "qwen_departed_from_recommended_range": bool(
                sections and not (
                    self.min_sections <= len(sections) <= self.max_sections
                )
            ),
            "qwen_outside_enforced_range": bool(
                sections and not (
                    self.min_sections <= len(sections) <= self.max_sections
                )
            ),
            "architecture_contract_warnings": (
                architecture_contract_warnings
            ),
            "architecture_contract_warning_count": len(
                architecture_contract_warnings
            ),
            "schema_alias_repairs": list(
                planner_output.get("_schema_alias_repairs") or []
            ),
            "reused_grounded_architecture": bool(
                planner_output.get("_reused_grounded_architecture")
            ),
            "raw_llm_sections": len(raw_sections),
            "llm_grounded_sections": llm_grounded,
            "deterministic_completion_sections": deterministic_completion,
            "llm_used_in_blueprint": planner_used,
            "architecture_attempt_count": int(planner_output.get("_planner_attempt_count") or 0),
            "architecture_reused_path": planner_output.get("_planner_reused_path", ""),
            "evidence_grounding": planner_output.get("_grounding_summary", {}),
            "scope_coverage_repair": planner_output.get("_scope_coverage_repair", {}),
            "note": (
                "The intellectual architecture is generated first; candidate "
                "knowledge-base assets are attached in a separate audited "
                "grounding stage."
                if self.real_llm_plan
                else (
                    "Explicit real_llm_plan=False deterministic fallback: "
                    "NON-PRODUCTION. Production requires a real Qwen "
                    "architecture plan; no deterministic completion is "
                    "allowed in the real path."
                )
            ),
        }

    def _resolve_db_path(self) -> Path | None:
        candidates: list[Path] = []
        if self.kb_dir:
            candidates.append(Path(self.kb_dir) / "review_knowledge_base.sqlite")
            candidates.append(Path(self.kb_dir))
        source_kb_dir = self.concept_map.get("source_kb_dir")
        if source_kb_dir:
            candidates.append(Path(source_kb_dir) / "review_knowledge_base.sqlite")
        source_sqlite = self.concept_map.get("source_sqlite")
        if source_sqlite:
            candidates.append(Path(source_sqlite))
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _load_concept_nodes(self) -> list[ConceptNode]:
        nodes: list[ConceptNode] = []
        for view in self.concept_map.get("views", []) if isinstance(self.concept_map.get("views"), list) else []:
            view_id = str(view.get("view_id") or "")
            view_name = str(view.get("name") or view_id)
            for raw in view.get("nodes", []) if isinstance(view.get("nodes"), list) else []:
                if not isinstance(raw, dict):
                    continue
                nodes.append(
                    ConceptNode(
                        node_id=str(raw.get("node_id") or ""),
                        view_id=view_id,
                        view_name=view_name,
                        label=compact(raw.get("label"), 180),
                        purpose=compact(raw.get("purpose"), 260),
                        planning_value=compact(raw.get("planning_value"), 360),
                        evidence_counts=raw.get("evidence_counts") if isinstance(raw.get("evidence_counts"), dict) else {},
                        raw=raw,
                    )
                )
        return nodes

    def _topic_text(self) -> str:
        return " ".join([self.user_question, self.problem_understanding, self.scope_definition])

    def _build_planning_evidence(self) -> dict[str, Any]:
        topic_text = self._topic_text()
        topic_terms = tokenize(topic_text, limit=36)
        ranked_nodes = self._rank_nodes(topic_text)
        selected_nodes = ranked_nodes[:42]
        dynamic_queries = self._dynamic_queries(topic_text, selected_nodes)
        retrieved_text = self._retrieve_text_chunks(
            dynamic_queries,
            limit_per_query=self.retrieval_limit_per_query,
            max_total=self.retrieval_max_total,
        )
        vector_retrieved, semantic_usage = self._retrieve_material_vectors(
            dynamic_queries,
            limit_per_query=self.retrieval_limit_per_query,
            max_total=self.retrieval_max_total,
        )
        material_retrieved = self._retrieve_material_units(
            dynamic_queries,
            limit_per_query=self.retrieval_limit_per_query,
            max_total=self.retrieval_max_total,
        )
        # Semantic cache is the preferred local material route. Lexical JSON
        # scan remains a deterministic fallback for units not found by the
        # vector query or when the embedding endpoint is temporarily down.
        seen_text_ids = set()
        merged_text: list[dict[str, Any]] = []
        for row in vector_retrieved + material_retrieved + retrieved_text:
            cid = str(row.get("chunk_id") or "")
            if cid and cid not in seen_text_ids:
                seen_text_ids.add(cid)
                merged_text.append(row)
        retrieved_text = merged_text
        evidence_digest = build_evidence_digest(
            retrieved_text, batch_size=self.evidence_batch_size
        )
        retrieved_visual = self._retrieve_visual_chunks(
            dynamic_queries,
            limit_per_query=8,
            max_total=self.visual_retrieval_max_total,
        )
        clusters = self._cluster_nodes(selected_nodes, retrieved_text, retrieved_visual)
        graph_context: dict[str, Any] = {}
        if self.s2_literature_graph:
            graph_context = {
                "summary": self.s2_literature_graph.get("summary", {}),
                "historical_lineage": self.s2_literature_graph.get(
                    "historical_lineage", {}
                ),
                "research_branches": (
                    self.s2_literature_graph.get("research_branches") or []
                )[:12],
                "usage_rule": (
                    "Use exact citation edges for historical development and "
                    "semantic recommendation edges for discovery only."
                ),
            }
        return {
            "topic_terms": topic_terms,
            "dynamic_queries": dynamic_queries,
            "selected_concept_nodes": [self._node_brief(n) for n in selected_nodes[:30]],
            "retrieved_text_chunks": retrieved_text,
            "evidence_digest": evidence_digest,
            "retrieved_visual_chunks": retrieved_visual[:DEFAULT_VISUAL_SERVED_MAX],
            "cluster_candidates": clusters,
            "s2_literature_graph_context": graph_context,
            "coverage": {
                "concept_nodes_available": len(self.concept_nodes),
                "concept_nodes_selected": len(selected_nodes),
                "text_chunks_retrieved": len(retrieved_text),
                "material_units_retrieved": len(material_retrieved),
                "semantic_vector_units_retrieved": len(vector_retrieved),
                "semantic_retrieval_usage": semantic_usage,
                "retrieval_sources": {
                    "sqlite_fts": len([row for row in retrieved_text if row.get("retrieval_source") == "sqlite_fts"]),
                    "material_vector_cache": len([row for row in retrieved_text if row.get("retrieval_source") == "material_vector_cache"]),
                    "material_unit_cache": len([row for row in retrieved_text if row.get("retrieval_source") == "material_unit_cache"]),
                },
                "visual_chunks_retrieved": len(retrieved_visual),
                "clusters": len(clusters),
                "paper_ids_seen": len({x.get("paper_id") for x in retrieved_text + retrieved_visual if x.get("paper_id")}),
                "retrieval_breadth": {
                    "limit_per_query": self.retrieval_limit_per_query,
                    "max_total": self.retrieval_max_total,
                    "visual_max_total": self.visual_retrieval_max_total,
                    "visual_served_max": DEFAULT_VISUAL_SERVED_MAX,
                    "advisory_target_range": list(PREFERRED_SECTION_TEXT_CANDIDATE_RANGE),
                    "candidate_pool_status": advisory_candidate_pool_status(
                        len(retrieved_text)
                    ),
                    "policy": (
                        "Advisory planning target; production preserves every "
                        "retrieved unique candidate (no 70/90 default cut)."
                    ),
                },
            },
            "retrieval_note": "Retrieved chunks are planning anchors. Final claims still require evidence binding to original text/caption/source.",
        }

    def _retrieve_material_units(
        self, queries: list[str], *, limit_per_query: int, max_total: int
    ) -> list[dict[str, Any]]:
        """Retrieve durable material units as a local, no-network fallback/overlay."""
        if not self.material_units:
            return []
        ranked: list[tuple[float, dict[str, Any], str]] = []
        for query in queries:
            for unit in self.material_units:
                identity = unit.get("identity") if isinstance(unit.get("identity"), dict) else {}
                durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), dict) else {}
                haystack = " ".join(
                    [str(identity.get("title") or ""), str(durable.get("normalized_text") or durable.get("raw_text") or "")]
                )
                score = text_overlap_score(query, haystack)
                if score <= 0:
                    continue
                ranked.append((score, unit, query))
        ranked.sort(key=lambda row: (-row[0], str((row[1].get("identity") or {}).get("chunk_id") or "")))
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, unit, query in ranked:
            identity = unit.get("identity") if isinstance(unit.get("identity"), dict) else {}
            durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), dict) else {}
            chunk_id = str(identity.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            out.append(self._attach_material_binding({
                "chunk_id": chunk_id,
                "paper_id": identity.get("paper_id", ""),
                "source_paper_id": identity.get("paper_id", ""),
                "doi": identity.get("doi", ""),
                "title": compact(identity.get("title"), 180),
                "section_path": compact(durable.get("section_path"), 120),
                "text_preview": compact(durable.get("normalized_text") or durable.get("raw_text"), 1800),
                "retrieval_query": compact(query, 180),
                "retrieval_source": "material_unit_cache",
                "match_score": round(score, 4),
            }, unit))
            if len(out) >= max_total:
                break
        return out

    def _retrieve_material_vectors(
        self, queries: list[str], *, limit_per_query: int, max_total: int
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Retrieve by local semantic vectors, with one bounded embedding call."""
        if not self.material_vectors_path or not self.material_vectors_path.exists() or not self.material_units:
            return [], {"input_tokens": 0, "request_count": 0, "status": "disabled"}
        try:
            from optomind_research.runtime.material_semantic_cache import (
                MaterialSemanticCache, dashscope_embedder
            )
            usage: dict[str, int] = {"input_tokens": 0, "request_count": 0}
            vectors = dashscope_embedder(
                list(queries), model=self.material_embedding_model, batch_size=min(10, max(1, len(queries))),
                usage_accumulator=usage, max_retries=1,
            )
            unit_by_id = {str(unit.get("unit_id") or ""): unit for unit in self.material_units}
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            with MaterialSemanticCache(self.material_vectors_path) as cache:
                for query, vector in zip(queries, vectors):
                    for hit in cache.search(vector, top_k=limit_per_query, embedding_model=self.material_embedding_model):
                        unit = unit_by_id.get(str(hit.get("unit_id") or ""))
                        if not unit:
                            continue
                        identity = unit.get("identity") if isinstance(unit.get("identity"), dict) else {}
                        durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), dict) else {}
                        cid = str(identity.get("chunk_id") or "")
                        if not cid or cid in seen:
                            continue
                        seen.add(cid)
                        out.append(self._attach_material_binding({
                            "chunk_id": cid,
                            "paper_id": identity.get("paper_id", ""),
                            "source_paper_id": identity.get("paper_id", ""),
                            "doi": identity.get("doi", ""),
                            "title": compact(identity.get("title"), 180),
                            "section_path": compact(durable.get("section_path"), 120),
                            "text_preview": compact(durable.get("normalized_text") or durable.get("raw_text"), 1800),
                            "retrieval_query": compact(query, 180),
                            "retrieval_source": "material_vector_cache",
                            "match_score": float(hit.get("score") or 0.0),
                        }, unit))
                        if len(out) >= max_total:
                            return out, {**usage, "status": "ok"}
            return out, {**usage, "status": "ok"}
        except Exception as exc:
            return [], {"input_tokens": 0, "request_count": 0, "status": f"failed:{type(exc).__name__}"}

    def _attach_material_binding(
        self,
        row: dict[str, Any],
        unit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach durable provenance, permissions, and proposition bindings."""
        chunk_id = str(row.get("chunk_id") or "")
        unit = unit or self.material_units_by_chunk_id.get(chunk_id)
        if not isinstance(unit, dict):
            return row
        durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), dict) else {}
        card = unit.get("durable_content_card") if isinstance(unit.get("durable_content_card"), dict) else {}
        quality = card.get("content_quality") if isinstance(card.get("content_quality"), dict) else {}
        audit = unit.get("audit") if isinstance(unit.get("audit"), dict) else {}
        provenance = audit.get("source_provenance") if isinstance(audit.get("source_provenance"), dict) else {}
        annotations = [
            item for item in unit.get("query_annotations") or []
            if isinstance(item, dict)
        ]
        propositions: list[dict[str, Any]] = []
        axes: list[dict[str, Any]] = []
        backgrounds: list[dict[str, Any]] = []
        for annotation in annotations:
            for proposition in annotation.get("propositions") or []:
                if not isinstance(proposition, dict):
                    continue
                propositions.append({
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": compact(proposition.get("statement"), 520),
                    "proposition_kind": str(proposition.get("proposition_kind") or ""),
                    "stance": str(proposition.get("stance") or ""),
                    "question_function": str(proposition.get("question_function") or ""),
                    "evidence_ceiling": str(
                        (proposition.get("evidence_permissions") or {}).get(chunk_id)
                        or proposition.get("weakest_evidence_ceiling")
                        or quality.get("evidence_ceiling")
                        or provenance.get("use_permission")
                        or "discovery_only"
                    ),
                })
            for assignment in annotation.get("seed_axis_assignments") or []:
                if isinstance(assignment, dict):
                    axes.append({
                        "axis_id": str(assignment.get("axis_id") or ""),
                        "fit": str(assignment.get("fit") or ""),
                        "question_function": str(assignment.get("question_function") or ""),
                    })
            for background in annotation.get("background_contexts") or []:
                if isinstance(background, dict):
                    backgrounds.append({"statement": compact(background.get("statement"), 420)})
        permission = str(
            quality.get("evidence_ceiling")
            or provenance.get("use_permission")
            or "discovery_only"
        )
        row = dict(row)
        row.update({
            "material_unit_id": str(unit.get("unit_id") or ""),
            "material_work_id": str(unit.get("work_id") or ""),
            "content_depth": str(durable.get("content_depth") or provenance.get("content_depth") or ""),
            "source_kind": str(quality.get("source_kind") or ""),
            "use_permission": permission,
            "context_complete": bool(quality.get("context_complete", provenance.get("context_complete", False))),
            "factual_support_allowed": permission == "factual_support",
            "material_card_binding": {
                "bound": bool(annotations),
                "query_ids": list(dict.fromkeys(
                    str(annotation.get("query_id") or "")
                    for annotation in annotations
                    if annotation.get("query_id")
                )),
                "question_relevance": list(dict.fromkeys(
                    str(annotation.get("question_relevance") or "")
                    for annotation in annotations
                    if annotation.get("question_relevance")
                )),
                "paper_functions": list(dict.fromkeys(
                    str(role)
                    for annotation in annotations
                    for role in annotation.get("paper_functions") or []
                    if role
                )),
                "axis_assignments": axes,
                "propositions": propositions,
                "background_contexts": backgrounds,
            },
            "material_binding_search_text": " ".join(
                [
                    *(str(item.get("statement") or "") for item in propositions),
                    *(str(item.get("axis_id") or "") for item in axes),
                    *(str(item.get("statement") or "") for item in backgrounds),
                ]
            ),
        })
        return row

    def _rank_nodes(self, topic_text: str) -> list[ConceptNode]:
        ranked: list[ConceptNode] = []
        for node in self.concept_nodes:
            lexical = text_overlap_score(topic_text, node.search_text)
            topic_bonus = 0.0
            low_topic = topic_text.lower()
            for token in tokenize(node.label, limit=10):
                if token in low_topic:
                    topic_bonus += 0.18
            evidence = math.log1p(max(0.0, node.evidence_weight)) / 8.0
            node.score = lexical * 2.3 + topic_bonus + evidence
            ranked.append(node)
        ranked.sort(key=lambda n: (n.score, n.evidence_weight, n.label), reverse=True)
        return ranked

    def _ensure_seed_axis_coverage(
        self, sections: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Keep every user-question seed axis visible without forcing sections.

        A missing axis may be attached as a subsection concern when the
        section language or selected material supports that assignment. If no
        section matches, the axis remains an explicit gap rather than being
        silently declared covered.
        """
        axis_nodes: dict[str, ConceptNode] = {}
        for node in self.concept_nodes:
            if node.node_id == f"material-axis:{node.view_id}":
                axis_nodes[node.view_id] = node
        if not axis_nodes:
            return sections, {
                "expected_axis_ids": [],
                "covered_axis_ids": [],
                "added_axis_ids": [],
                "missing_axis_ids": [],
                "status": "no_seed_axis_catalog",
            }

        axis_token_frequency = Counter(
            token
            for axis_node in axis_nodes.values()
            for token in set(tokenize(axis_node.label, limit=80))
        )

        out = [dict(section) for section in sections]
        for section in out:
            assignments = [
                dict(item)
                for item in (section.get("axis_assignments") or [])
                if isinstance(item, dict) and item.get("axis_id")
            ]
            known = {str(item.get("axis_id")) for item in assignments}
            for node in section.get("concept_map_nodes") or []:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("node_id") or "")
                if not node_id.startswith("material-axis:"):
                    continue
                axis_id = node_id.removeprefix("material-axis:")
                if axis_id not in known:
                    assignments.append({
                        "axis_id": axis_id,
                        "label": compact(node.get("label"), 180),
                        "assignment_basis": "concept_map_axis",
                    })
            section["axis_assignments"] = assignments
        initially_covered = {
            str(node.get("node_id") or "").removeprefix("material-axis:")
            for section in out
            for node in section.get("concept_map_nodes") or []
            if isinstance(node, dict)
            and str(node.get("node_id") or "").startswith("material-axis:")
        }
        added: list[dict[str, str]] = []
        missing: list[dict[str, str]] = []
        for axis_id, node in axis_nodes.items():
            if axis_id in initially_covered:
                continue
            distinctive_axis_tokens = {
                token
                for token in tokenize(node.label, limit=80)
                if axis_token_frequency[token] <= 1
            }
            scored: list[tuple[float, int, int, int]] = []
            for index, section in enumerate(out):
                section_text = " ".join(
                    [
                        str(section.get("title") or ""),
                        str(section.get("argument_role") or ""),
                        " ".join(clean_list(section.get("key_questions"), 8)),
                        " ".join(clean_list(section.get("candidate_search_seeds"), 8)),
                    ]
                )
                material_hits = sum(
                    str(assignment.get("axis_id") or "") == axis_id
                    for row in section.get("candidate_text_chunks") or []
                    if isinstance(row, dict)
                    for assignment in (
                        (row.get("material_card_binding") or {}).get("axis_assignments") or []
                    )
                    if isinstance(assignment, dict)
                )
                section_tokens = set(tokenize(section_text, limit=160))
                distinctive_hits = len(distinctive_axis_tokens & section_tokens)
                score = text_overlap_score(node.search_text, section_text)
                if distinctive_hits:
                    score += 0.5 * distinctive_hits / math.sqrt(
                        max(1, len(distinctive_axis_tokens))
                    )
                score += min(0.75, material_hits * 0.15)
                # Candidate material is an independent signal.  Do not let a
                # previous claim seed or risk statement decide axis ownership;
                # use the actual title/preview/proposition text instead.
                material_semantic_score = 0.0
                for row in section.get("candidate_text_chunks") or []:
                    if not isinstance(row, dict):
                        continue
                    binding = row.get("material_card_binding") if isinstance(row.get("material_card_binding"), dict) else {}
                    proposition_text = " ".join(
                        compact(item.get("statement"), 420)
                        for item in (binding.get("propositions") or [])
                        if isinstance(item, dict)
                    )
                    material_semantic_score += text_overlap_score(
                        node.label,
                        " ".join(
                            [
                                str(row.get("title") or ""),
                                str(row.get("text_preview") or ""),
                                proposition_text,
                            ]
                        ),
                    )
                score += min(1.0, material_semantic_score * 2.5)
                scored.append((score, material_hits, distinctive_hits, index))
            scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            best_score, material_hits, distinctive_hits, best_index = (
                scored[0] if scored else (0.0, 0, 0, -1)
            )
            if best_index < 0 or (
                material_hits <= 0
                and distinctive_hits <= 0
                and best_score < 0.12
            ):
                missing.append({"axis_id": axis_id, "label": node.label})
                continue
            section = out[best_index]
            concept_nodes = [
                dict(item) for item in section.get("concept_map_nodes") or []
                if isinstance(item, dict)
            ]
            concept_nodes.append(self._node_brief(node))
            section["concept_map_nodes"] = concept_nodes
            axis_assignments = [
                dict(item)
                for item in (section.get("axis_assignments") or [])
                if isinstance(item, dict) and item.get("axis_id")
            ]
            if axis_id not in {str(item.get("axis_id")) for item in axis_assignments}:
                axis_assignments.append({
                    "axis_id": axis_id,
                    "label": compact(node.label, 180),
                    "assignment_basis": "seed_axis_coverage",
                })
            section["axis_assignments"] = axis_assignments
            generated_from = dict(section.get("generated_from") or {})
            generated_from.setdefault("seed_axis_coverage_added", []).append(axis_id)
            section["generated_from"] = generated_from
            if material_hits <= 0:
                risks = clean_list(section.get("evidence_risks"), 8)
                risks.append(
                    compact(
                        f"User-requested axis '{node.label}' is represented in the outline but still needs directly assigned evidence.",
                        520,
                    )
                )
                section["evidence_risks"] = list(dict.fromkeys(risks))[:8]
            added.append({
                "axis_id": axis_id,
                "section_id": str(section.get("section_id") or ""),
                "basis": (
                    "material_axis_assignment"
                    if material_hits
                    else "distinctive_axis_term_match"
                    if distinctive_hits
                    else "section_semantic_match"
                ),
            })

        covered = initially_covered | {item["axis_id"] for item in added}
        evidence_backed = {
            str(assignment.get("axis_id") or "")
            for section in out
            for row in section.get("candidate_text_chunks") or []
            if isinstance(row, dict)
            for assignment in (
                (row.get("material_card_binding") or {}).get("axis_assignments") or []
            )
            if isinstance(assignment, dict)
            and str(assignment.get("axis_id") or "") in axis_nodes
        }
        evidence_gap_axis_ids = sorted(set(axis_nodes) - evidence_backed)
        return out, {
            "expected_axis_ids": sorted(axis_nodes),
            "initially_covered_axis_ids": sorted(initially_covered),
            "covered_axis_ids": sorted(covered),
            "evidence_backed_axis_ids": sorted(evidence_backed),
            "evidence_gap_axis_ids": evidence_gap_axis_ids,
            "added_axes": added,
            "missing_axes": missing,
            "missing_axis_ids": [item["axis_id"] for item in missing],
            "status": (
                "outline_incomplete"
                if missing
                else "outline_complete_with_evidence_gaps"
                if evidence_gap_axis_ids
                else "outline_and_candidate_evidence_complete"
            ),
        }

    def _dynamic_queries(self, topic_text: str, nodes: list[ConceptNode]) -> list[str]:
        queries: list[str] = []
        base = compact(topic_text, 220)
        if base:
            queries.append(base)
        for node in nodes[:16]:
            q = compact(" ".join([node.label, node.purpose or node.planning_value]), 180)
            if q:
                queries.append(q)
        # Deduplicate while preserving order and keep the plan focused.
        out: list[str] = []
        seen: set[str] = set()
        for q in queries:
            key = " ".join(tokenize(q, limit=12))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= 14:
                break
        return out

    def _retrieve_text_chunks(self, queries: list[str], *, limit_per_query: int, max_total: int) -> list[dict[str, Any]]:
        if not self.db_path:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        reached_limit = False
        for query in queries:
            rows = fts_or_like_rows(
                self.db_path,
                fts_table="text_chunk_fts",
                data_table="text_chunks",
                join_key="chunk_id",
                query_text=query,
                limit=limit_per_query,
            )
            for row in rows:
                cid = row["chunk_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(
                    self._attach_material_binding({
                        "chunk_id": cid,
                        "paper_id": row["paper_id"],
                        "source_paper_id": row["paper_id"],
                        "doi": row["doi"] if "doi" in row.keys() else "",
                        "title": compact(row["title"], 180),
                        "section_path": compact(row["section_path"], 120),
                        "text_preview": compact(row["text"], 1800),
                        "retrieval_query": compact(query, 180),
                        "retrieval_source": "sqlite_fts",
                    })
                )
                if len(out) >= max_total:
                    reached_limit = True
                    break
            if reached_limit:
                break
        paper_ids = list(dict.fromkeys(str(row.get("paper_id")) for row in out if row.get("paper_id")))
        if paper_ids and self.db_path:
            con = sqlite3.connect(self.db_path)
            try:
                placeholders = ",".join("?" for _ in paper_ids)
                year_map = {
                    str(pid): year
                    for pid, year in con.execute(
                        f"SELECT paper_id,year FROM papers WHERE paper_id IN ({placeholders})",
                        paper_ids,
                    )
                }
            finally:
                con.close()
            for row in out:
                row["publication_year"] = year_map.get(str(row.get("paper_id")))
        return out

    def _retrieve_visual_chunks(self, queries: list[str], *, limit_per_query: int, max_total: int) -> list[dict[str, Any]]:
        if not self.db_path:
            return []
        # The current material cache may be text-only. Do not let the planner
        # crash merely because the optional visual tables have not been built;
        # visual retrieval is an additive route and an empty result is valid.
        try:
            with sqlite3.connect(self.db_path) as probe:
                tables = {
                    str(row[0])
                    for row in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    )
                }
            if not {"visual_chunks", "visual_chunk_fts"}.issubset(tables):
                return []
        except sqlite3.Error:
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query in queries:
            rows = fts_or_like_rows(
                self.db_path,
                fts_table="visual_chunk_fts",
                data_table="visual_chunks",
                join_key="chunk_id",
                query_text=query,
                limit=limit_per_query,
            )
            for row in rows:
                cid = row["chunk_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                raw_json = safe_json_parse(row["raw_json"]) if "raw_json" in row.keys() else {}
                path = row["local_image_path"]
                out.append(
                    {
                        "chunk_id": cid,
                        "paper_id": row["paper_id"],
                        "title": compact(row["title"], 180),
                        "chunk_kind": row["chunk_kind"],
                        "parent_label": row["parent_label"],
                        "subfigure_label": row["subfigure_label"],
                        "visual_role": row["visual_role"],
                        "review_utility": row["review_utility"],
                        "local_image_path": path,
                        "caption_preview": compact(row["caption"], 360),
                        "best_use_in_review": compact(raw_json.get("best_use_in_review"), 260),
                        "direct_use_candidate": raw_json.get("direct_use_candidate", ""),
                        "redraw_recommendation": raw_json.get("redraw_recommendation", ""),
                        "quality_flags": raw_json.get("quality_flags", []),
                        "retrieval_query": compact(query, 180),
                    }
                )
                if len(out) >= max_total:
                    return out
        return out

    def _cluster_nodes(
        self,
        nodes: list[ConceptNode],
        text_chunks: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_view: dict[str, list[ConceptNode]] = defaultdict(list)
        for node in nodes:
            by_view[node.view_id].append(node)
        clusters: list[dict[str, Any]] = []
        for view_id, view_nodes in by_view.items():
            # Visual-argument nodes are important for selecting figures, but a
            # review should not create a major scientific section named after a
            # generic visual type such as "graph" or "schematic".
            if view_id == "visual_argument_view":
                continue
            view_nodes = sorted(view_nodes, key=lambda n: (n.score, n.evidence_weight), reverse=True)[:8]
            if not view_nodes:
                continue
            view_name = view_nodes[0].view_name
            labels = [n.label for n in view_nodes[:5] if n.label]
            node_text = " ".join(n.search_text for n in view_nodes)
            text_matches = self._rank_items_by_overlap(
                node_text, text_chunks, "text_preview", limit=self.served_text_limit
            )
            visual_matches = self._rank_items_by_overlap(node_text, visual_chunks, "caption_preview", limit=24)
            score = sum(n.score for n in view_nodes[:5]) + len(text_matches) * 0.15 + len(visual_matches) * 0.18
            clusters.append(
                {
                    "cluster_id": f"cluster:{view_id}",
                    "view_id": view_id,
                    "view_name": view_name,
                    "score": round(score, 4),
                    "central_labels": labels,
                    "planning_value": self._cluster_planning_value(view_name, labels),
                    "node_ids": [n.node_id for n in view_nodes],
                    "text_chunk_ids": [x["chunk_id"] for x in text_matches],
                    "visual_chunk_ids": [x["chunk_id"] for x in visual_matches],
                    "evidence_counts": {
                        "nodes": len(view_nodes),
                        "text_chunks": len(text_matches),
                        "visual_chunks": len(visual_matches),
                        "papers": len({x.get("paper_id") for x in text_matches + visual_matches if x.get("paper_id")}),
                    },
                }
            )
        clusters.sort(key=lambda x: (x["score"], x["evidence_counts"]["papers"]), reverse=True)
        selected = clusters[: self.max_sections]
        if len(selected) < self.min_sections:
            selected = clusters[: self.min_sections]
        return selected

    def _cluster_planning_value(self, view_name: str, labels: list[str]) -> str:
        label_text = "; ".join(labels[:4])
        return (
            f"This cluster comes from {view_name}. It should help the review decide how to synthesize {label_text} "
            "without treating papers as an unordered bibliography."
        )

    def _rank_items_by_overlap(
        self,
        anchor_text: str,
        items: list[dict[str, Any]],
        field: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        ranked = []
        for item in items:
            score = text_overlap_score(anchor_text, " ".join([str(item.get(field, "")), str(item.get("title", ""))]))
            if score > 0:
                ranked.append((score, item))
        ranked.sort(key=lambda x: x[0], reverse=True)
        # None means unlimited (planner default preserves every candidate).
        return [dict(item, match_score=round(score, 4)) for score, item in ranked[:limit]]

    def _node_brief(self, node: ConceptNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "view_id": node.view_id,
            "view_name": node.view_name,
            "label": node.label,
            "planning_value": node.planning_value,
            "evidence_counts": node.evidence_counts,
            "score": round(node.score, 4),
        }

    def _build_dynamic_sections(self, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        clusters = evidence.get("cluster_candidates") if isinstance(evidence.get("cluster_candidates"), list) else []
        node_by_id = {n.node_id: n for n in self.concept_nodes}
        text_by_id = {x["chunk_id"]: x for x in evidence.get("retrieved_text_chunks", []) if isinstance(x, dict)}
        visual_by_id = {x["chunk_id"]: x for x in evidence.get("retrieved_visual_chunks", []) if isinstance(x, dict)}
        for idx, cluster in enumerate(clusters[: self.max_sections], start=1):
            nodes = [node_by_id[nid] for nid in cluster.get("node_ids", []) if nid in node_by_id][:8]
            texts = [
                text_by_id[cid]
                for cid in cluster.get("text_chunk_ids", [])
                if cid in text_by_id
            ]
            visuals = [
                visual_by_id[cid]
                for cid in cluster.get("visual_chunk_ids", [])
                if cid in visual_by_id
            ][:24]
            if not nodes:
                continue
            section_id = f"S{idx:02d}"
            sections.append(self._dynamic_section(section_id, cluster, nodes, texts, visuals))
        return sections

    def _dynamic_section(
        self,
        section_id: str,
        cluster: dict[str, Any],
        nodes: list[ConceptNode],
        text_chunks: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        labels = [n.label for n in nodes if n.label]
        view_name = compact(cluster.get("view_name"), 80)
        title = self._dynamic_title(view_name, labels)
        argument_role = self._dynamic_argument_role(view_name, labels)
        key_questions = self._dynamic_questions(view_name, labels)
        visual_slots = self._visual_slots_for_section(section_id, labels, visual_chunks)
        served_text_chunks = (
            text_chunks
            if self.served_text_limit is None
            else text_chunks[: self.served_text_limit]
        )
        claim_candidates = self._claim_candidates(
            labels, served_text_chunks, visual_chunks
        )
        evidence_digest = build_evidence_digest(
            text_chunks, batch_size=self.evidence_batch_size
        )
        axis_assignments = [
            {
                "axis_id": str(node.node_id).removeprefix("material-axis:"),
                "label": compact(node.label, 180),
                "assignment_basis": "concept_map_axis",
            }
            for node in nodes
            if str(node.node_id).startswith("material-axis:")
        ]
        for seed in claim_candidates:
            if isinstance(seed, dict):
                seed.setdefault("axis_assignments", [dict(item) for item in axis_assignments])
        return {
            "section_id": section_id,
            "title": title,
            "argument_role": argument_role,
            "key_questions": key_questions,
            # T4: required claim kinds derived from section evidence profile
            "required_claim_kinds": self._infer_required_claim_kinds(
                cluster, served_text_chunks, visual_chunks
            ),
            # T4: transitions (transition_from_previous filled by _attach_transitions)
            "transition_from_previous": "",
            "transition_to_next": "",
            "unique_contribution": self._novel_contribution(
                view_name, labels, {}
            ),
            "must_cover": [],
            "must_not_cover": [],
            "assigned_user_axes": [],
            "handoff_from_previous": "",
            "handoff_to_next": "",
            # T4: expected visual argument types for this section
            "expected_visual_arguments": self._expected_visual_arguments(labels, visual_chunks),
            # T4: what this section uniquely contributes to the review argument
            "novel_contribution_to_review": self._novel_contribution(view_name, labels, cluster),
            # T4: scope guardrails to prevent section drift
            "scope_guardrails": self._scope_guardrails(
                cluster, served_text_chunks
            ),
            "concept_map_nodes": [self._node_brief(n) for n in nodes],
            "candidate_text_chunks": served_text_chunks,
            "candidate_text_context": evidence_digest.get("chunk_index", [])[: self.model_text_context_limit],
            "candidate_text_chunk_ids": [
                x.get("chunk_id") for x in served_text_chunks
            ],
            "candidate_evidence_digest": evidence_digest,
            "candidate_text_model_policy": {
                "raw_text_in_default_model_prompt": False,
                "digest_field": "candidate_evidence_digest",
                "reopen_by": "chunk_id",
                "batch_size": self.evidence_batch_size,
            },
            "candidate_material_pool": self._candidate_material_pool(
                candidate_chunk_ids=[
                    x.get("chunk_id") for x in text_chunks if x.get("chunk_id")
                ],
                candidate_paper_ids=list(dict.fromkeys(
                    str(x.get("paper_id") or "")
                    for x in text_chunks
                    if x.get("paper_id")
                )),
                served_chunk_ids=[
                    x.get("chunk_id")
                    for x in served_text_chunks
                    if x.get("chunk_id")
                ],
                model_context_chunk_ids=[
                    x.get("chunk_id")
                    for x in served_text_chunks[: self.model_text_context_limit]
                    if x.get("chunk_id")
                ],
                compression_policy=(
                    "Every retained candidate is summarized in "
                    "candidate_evidence_digest batches and stays reopenable by "
                    "chunk_id; downstream planning reads batch summaries plus "
                    "a bounded raw-dossier layer. No hard 200th-item cut."
                ),
            ),
            "candidate_visual_chunks": visual_chunks,
            "visual_argument_slots": visual_slots,
            "axis_assignments": axis_assignments,
            "argument_structure": {
                "composition_mode": "multi_axis_claim_centered",
                "required_relation_roles": [
                    "support", "counterevidence", "boundary_condition",
                    "background_context", "open_gap",
                ],
                "writing_sequence": [
                    "state_claim", "bind_positive_support", "surface_counterevidence",
                    "state_boundary_conditions", "close_with_scope_or_gap",
                ],
            },
            "claim_graph_seed": {
                "central_claim_candidates": claim_candidates,
                "relation_types_to_check": ["support", "contrast", "refine", "boundary_condition", "open_gap"],
                "relation_contract": {
                    "required_roles": [
                        "support", "counterevidence", "boundary_condition",
                        "background_context", "open_gap",
                    ],
                    "role_binding_rule": (
                        "Each load-bearing claim needs positive support and an explicit audit of "
                        "counterevidence or boundary conditions; background and unresolved gaps "
                        "remain separate from supporting evidence."
                    ),
                },
                "claim_binding_rule": "A later evidence binder must attach each claim to exact text or caption spans before final writing.",
            },
            "review_example_archetype_anchor": self._archetype_anchor_for_section(title, view_name),
            "candidate_search_seeds": self._section_search_seeds(labels, title),
            "writing_requirements": [
                "Explain the section's argument before listing papers.",
                "Group studies by mechanism, metric, material route, method, application, or controversy as appropriate.",
                "Use text and visual anchors as planning inputs; do not claim unsupported numbers without later evidence binding.",
                "If evidence is thin or contradictory, mark it as a review gap rather than smoothing it over.",
            ],
            "evidence_risks": self._dynamic_evidence_risks(
                cluster, served_text_chunks, visual_chunks
            ),
            "generated_from": {
                "cluster_id": cluster.get("cluster_id"),
                "view_id": cluster.get("view_id"),
                "central_labels": labels[:6],
                "dynamic_not_template": True,
            },
        }

    def _dynamic_title(self, view_name: str, labels: list[str]) -> str:
        cleaned = [self._human_label(x) for x in labels if x][:3]
        view_name = self._human_label(view_name)
        view_name = re.sub(r"\s+view$", "", view_name, flags=re.I)
        if not cleaned:
            return compact(view_name or "Evidence cluster", 120)
        if len(cleaned) == 1:
            return compact(f"{view_name}: {cleaned[0]}", 180)
        return compact(f"{view_name}: {cleaned[0]} and related evidence", 180)

    def _human_label(self, value: str) -> str:
        text = compact(value, 160).replace("_", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if text.islower() and len(text) > 3:
            return text.replace("power", "power").capitalize()
        return text

    def _dynamic_argument_role(self, view_name: str, labels: list[str]) -> str:
        view_name = self._human_label(view_name)
        label_text = "; ".join(self._human_label(x) for x in labels[:4])
        return compact(
            f"Use this section to turn the {view_name} evidence cluster into a review-level argument about {label_text}. "
            "It should clarify why this cluster matters, what it explains, and what later sections must compare or challenge.",
            520,
        )

    def _dynamic_questions(self, view_name: str, labels: list[str]) -> list[str]:
        qs = []
        view_name = self._human_label(view_name)
        for label in labels[:3]:
            qs.append(f"What does the literature actually establish about {self._human_label(label)}, and under which assumptions or test conditions?")
        qs.append(f"How should the {view_name} cluster reshape the review's structure rather than merely add another topic?")
        qs.append("Which claims require exact text/caption evidence before they can appear in the final review?")
        return qs[:5]

    def _visual_slots_for_section(
        self,
        section_id: str,
        labels: list[str],
        visual_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        slots: list[dict[str, Any]] = []
        if not visual_chunks:
            return slots
        role_counter = Counter(str(v.get("visual_role") or "unknown") for v in visual_chunks)
        top_roles = [role for role, _ in role_counter.most_common(4) if role]
        goals = []
        if labels:
            goals.append(f"Explain or compare evidence around {labels[0]}")
        if len(labels) >= 2:
            goals.append(f"Show why {labels[0]} must be discussed together with {labels[1]}")
        goals.append("Reserve a visual evidence slot for a high-value figure, subfigure, or redraw candidate")
        used: set[str] = set()
        for idx, goal in enumerate(goals[:3], start=1):
            ranked = self._rank_visuals_for_goal(goal, visual_chunks, used, limit=4)
            used.update(str(v.get("chunk_id") or "") for v in ranked)
            slots.append(
                {
                    "slot_id": f"{section_id}-V{idx:02d}",
                    "goal": compact(goal, 180),
                    "purpose": self._visual_slot_purpose(goal, ranked),
                    "preferred_visual_roles": top_roles,
                    "candidate_visual_chunks": ranked,
                    "usage_rule": "Use only after checking the caption, nearby text, image crop quality, and original source path.",
                }
            )
        return slots

    def _rank_visuals_for_goal(
        self,
        goal: str,
        visual_chunks: list[dict[str, Any]],
        used: set[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        ranked = []
        for visual in visual_chunks:
            cid = str(visual.get("chunk_id") or "")
            if cid in used:
                continue
            score = text_overlap_score(goal, " ".join([str(visual.get("caption_preview", "")), str(visual.get("best_use_in_review", "")), str(visual.get("title", ""))]))
            if str(visual.get("review_utility", "")).lower() == "high":
                score += 0.3
            if str(visual.get("direct_use_candidate", "")).lower() == "yes":
                score += 0.1
            path = str(visual.get("local_image_path") or "")
            if path and not Path(path).exists():
                score -= 1.0
            ranked.append((score, visual))
        ranked.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, visual in ranked:
            item = dict(visual)
            item["goal_match_score"] = round(score, 4)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def _visual_slot_purpose(self, goal: str, visuals: list[dict[str, Any]]) -> str:
        roles = ", ".join(sorted({str(v.get("visual_role") or "unknown") for v in visuals})[:5])
        return compact(
            f"Use this slot to make the argument visually checkable for: {goal}. Candidate visual roles: {roles or 'none'}.",
            360,
        )

    def _claim_candidates(
        self,
        labels: list[str],
        text_chunks: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for idx, label in enumerate(labels[:4], start=1):
            matching_texts = self._rank_items_by_overlap(label, text_chunks, "text_preview", limit=3)
            matching_visuals = self._rank_items_by_overlap(label, visual_chunks, "caption_preview", limit=3)
            claims.append(
                {
                    "claim_seed_id": f"C{idx:02d}",
                    "claim_seed": compact(f"The literature cluster suggests that {label} is a key organizing dimension, but the exact claim must be bound to source text before writing.", 320),
                    "supporting_text_chunk_ids": [x.get("chunk_id") for x in matching_texts],
                    "supporting_visual_chunk_ids": [x.get("chunk_id") for x in matching_visuals],
                    "relation_roles": [
                        "support", "counterevidence", "boundary_condition",
                        "background_context", "open_gap",
                    ],
                    "counterevidence_query": compact(
                        f"{label} contradictory findings limitations alternative explanation", 260
                    ),
                    "boundary_conditions": [
                        "Check operating regime, geometry, assumptions, and measurement or fabrication conditions before generalizing."
                    ],
                    "status": "planning_seed_not_final_claim",
                }
            )
        return claims

    def _section_search_seeds(self, labels: list[str], title: str) -> list[str]:
        seeds = [title] + [f"{label} review evidence benchmark" for label in labels[:4]]
        out: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            key = " ".join(tokenize(seed, limit=12))
            if key and key not in seen:
                out.append(compact(seed, 180))
                seen.add(key)
        return out[:5]

    # ---------------------------------------------------------------------- #
    # T4: New section schema helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _infer_required_claim_kinds(
        cluster: dict[str, Any],
        text_chunks: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> list[str]:
        """Infer which claim_kind types are expected in this section.

        Uses heuristics on cluster view_id, evidence density, and visual presence
        to suggest appropriate claim kinds. The claim decomposer should aim to
        produce at least one claim of each required kind.
        """
        view_id = str(cluster.get("view_id") or "").lower()
        kinds: list[str] = []
        # Every section should have at least one direct fact or mechanism claim
        if "mechanism" in view_id or "physical" in view_id or "process" in view_id:
            kinds.append("mechanism_synthesis")
        if "material" in view_id or "structure" in view_id or "design" in view_id:
            kinds.append("direct_fact")
        if "benchmark" in view_id or "performance" in view_id or "comparison" in view_id:
            kinds.append("quantitative_comparison")
        if "gap" in view_id or "frontier" in view_id or "future" in view_id:
            kinds.append("absence_or_neglect")
            kinds.append("frontier_uncertainty")
        if "method" in view_id or "measurement" in view_id or "characterization" in view_id:
            kinds.append("methodological_critique")
        # Default baseline
        if not kinds:
            kinds = ["direct_fact", "mechanism_synthesis"]
        # Add recommendation if visual evidence is present
        if visual_chunks and "normative_recommendation" not in kinds:
            pass  # visual alone doesn't imply normative
        # If we have many papers, corpus_prevalence is appropriate
        counts = cluster.get("evidence_counts") if isinstance(cluster.get("evidence_counts"), dict) else {}
        if counts.get("papers", 0) >= 8:
            if "corpus_prevalence" not in kinds:
                kinds.append("corpus_prevalence")
        return list(dict.fromkeys(kinds))[:4]  # dedup, max 4

    @staticmethod
    def _expected_visual_arguments(
        labels: list[str],
        visual_chunks: list[dict[str, Any]],
    ) -> list[str]:
        """Describe the expected visual argument types for this section."""
        args: list[str] = []
        if not visual_chunks:
            args.append("No visual candidates retrieved; section may rely on text-only synthesis.")
            return args
        role_counter: dict[str, int] = {}
        for v in visual_chunks:
            role = str(v.get("visual_role") or "unknown")
            role_counter[role] = role_counter.get(role, 0) + 1
        for role, count in sorted(role_counter.items(), key=lambda x: -x[1])[:3]:
            if role and role != "unknown":
                args.append(f"Use {role} visual(s) to support the {role}-type argument ({count} candidate(s) available).")
        if labels:
            args.append(f"Prioritize figures that directly compare or mechanize {labels[0]}.")
        return args[:3]

    @staticmethod
    def _novel_contribution(
        view_name: str, labels: list[str], cluster: dict[str, Any]
    ) -> str:
        """State what this section uniquely contributes to the review argument."""
        view_name = re.sub(r"\s+view$", "", str(view_name or "").strip(), flags=re.I)
        label_text = "; ".join(labels[:3]) if labels else "the identified evidence cluster"
        counts = cluster.get("evidence_counts") if isinstance(cluster.get("evidence_counts"), dict) else {}
        paper_count = counts.get("papers", 0)
        density = "high-density" if paper_count >= 6 else "focused"
        return compact(
            f"This section contributes a {density} synthesis of {view_name} evidence around {label_text}. "
            "Unlike adjacent sections, it should resolve a specific intellectual tension or establish "
            "a comparison that later sections can reference as established ground truth.",
            600,
        )

    @staticmethod
    def _scope_guardrails(
        cluster: dict[str, Any],
        text_chunks: list[dict[str, Any]],
    ) -> list[str]:
        """Return constraints that prevent this section from drifting out of scope."""
        guardrails: list[str] = [
            "Only discuss studies whose primary contribution is within this section's evidence cluster.",
            "Do not repeat mechanisms or benchmarks that belong to adjacent sections.",
        ]
        counts = cluster.get("evidence_counts") if isinstance(cluster.get("evidence_counts"), dict) else {}
        if counts.get("papers", 0) < 4:
            guardrails.append("Evidence is sparse; limit the section to establishing known facts rather than claiming consensus.")
        if len(text_chunks) < 5:
            guardrails.append("Fewer than 5 text anchors retrieved; focus on what the existing chunks actually establish.")
        return guardrails

    def _dynamic_evidence_risks(
        self,
        cluster: dict[str, Any],
        text_chunks: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> list[str]:
        risks = ["Concept nodes and retrieved chunks are planning aids; final claims need exact source-span binding."]
        counts = cluster.get("evidence_counts") if isinstance(cluster.get("evidence_counts"), dict) else {}
        if counts.get("papers", 0) < 3:
            risks.append("This cluster has low paper diversity; avoid treating it as field consensus.")
        if len(visual_chunks) > len(text_chunks) * 2 and visual_chunks:
            risks.append("Visual evidence is abundant relative to text anchors; verify captions and nearby text before using it as evidence.")
        if len(text_chunks) > len(visual_chunks) * 3 and text_chunks:
            risks.append("Text evidence dominates; do not force a visual argument if no useful figure exists.")
        if not visual_chunks:
            risks.append("No visual candidates were retrieved for this section; use text-only synthesis unless later retrieval finds figures.")
        return risks

    def _archetype_anchor_for_section(self, title: str, view_name: str) -> dict[str, Any]:
        memory = self.review_example_memory
        if not memory:
            return {}
        archetypes = [x for x in memory.get("section_archetypes", []) if isinstance(x, dict)]
        query = f"{title} {view_name}".lower()
        best: dict[str, Any] | None = None
        best_score = 0.0
        for item in archetypes:
            text = " ".join(str(item.get(k, "")) for k in ["archetype", "purpose", "typical_position", "useful_for_blueprint"]).lower()
            score = text_overlap_score(query, text)
            if score > best_score:
                best_score = score
                best = item
        if not best:
            return {}
        return {
            "matched_archetype": compact(best.get("archetype"), 120),
            "purpose": compact(best.get("purpose"), 220),
            "useful_for_blueprint": compact(best.get("useful_for_blueprint"), 220),
            "match_score": round(best_score, 4),
        }

    def _review_example_anchor(self) -> dict[str, Any]:
        # The raw M1 moves contain scientific examples from their source
        # domains.  They must not be dumped into the planner prompt.  M1.5
        # ReviewMentorAgent performs the abstraction/selection step first.
        lib = self.active_library
        if lib:
            paper_ids = {
                str(item.get("source_paper_id"))
                for cat in _M1_CATEGORIES
                for item in (lib.get(cat) or [])
                if isinstance(item, dict) and item.get("source_paper_id")
            }
            return {
                "source": "m1_active_library",
                "active_papers": len(paper_ids),
                "category_counts": {cat: len(lib.get(cat) or []) for cat in _M1_CATEGORIES},
                "usage_rule": "Raw source-domain moves are withheld here; use review_mentor_advice for abstract structural guidance only.",
            }
        # Fallback: legacy review_example_memory format
        memory = self.review_example_memory
        if not memory:
            return {}
        moves_legacy = memory.get("intellectual_moves") if isinstance(memory.get("intellectual_moves"), dict) else {}
        return {
            "source": "legacy_review_example_memory",
            "global_patterns": clean_list(memory.get("global_patterns"), 8),
            "outline_templates": [
                {
                    "template_name": compact(x.get("template_name"), 120),
                    "best_for": compact(x.get("best_for"), 180),
                    "section_sequence": clean_list(x.get("section_sequence"), 10),
                }
                for x in (memory.get("outline_templates") if isinstance(memory.get("outline_templates"), list) else [])[:5]
                if isinstance(x, dict)
            ],
            "critic_questions": clean_list(memory.get("critic_questions_for_blueprint"), 10),
            "anti_patterns": clean_list(memory.get("anti_patterns"), 8),
            "intellectual_moves": {
                cat: clean_list(moves_legacy.get(cat), 6)
                for cat in _M1_CATEGORIES
            },
            "usage_rule": "Use this as a structural reference for planning, not as scientific evidence.",
        }

    def _dynamic_review_thesis(self, evidence: dict[str, Any]) -> str:
        clusters = evidence.get("cluster_candidates") if isinstance(evidence.get("cluster_candidates"), list) else []
        labels: list[str] = []
        for cluster in clusters[:4]:
            labels.extend(cluster.get("central_labels", [])[:2])
        label_text = "; ".join(dict.fromkeys(labels).keys())
        base = compact(self.problem_understanding or self.user_question, 420)
        return compact(
            f"This review should answer the user's question by organizing the selected literature around evidence-derived dimensions: {label_text}. "
            f"The central task is not to list papers, but to explain how these dimensions reshape the field's mechanisms, methods, applications, limits, and next research questions. Context: {base}",
            1200,
        )

    def _dynamic_narrative_strategy(self, evidence: dict[str, Any]) -> str:
        clusters = evidence.get("cluster_candidates") if isinstance(evidence.get("cluster_candidates"), list) else []
        cluster_names = [compact(c.get("view_name"), 80) for c in clusters[:6]]
        return compact(
            "Dynamic synthesis from retrieved clusters: "
            + " -> ".join([x for x in cluster_names if x])
            + ". The exact section order should be justified by argumentative progression, not by a fixed template.",
            800,
        )

    def _resolve_architecture(
        self,
        evidence: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
        """Resolve the blueprint architecture from the Qwen-required or
        explicit deterministic path.

        Production behavior is Qwen-first and Qwen-required: an empty or
        structurally invalid real LLM plan (including any chapter count
        outside 8-10) raises a clear failure and never falls back to
        ``_build_dynamic_sections`` or deterministic completion. Deterministic
        planning is available only through an explicit ``real_llm_plan=False``
        path and is labeled as a non-production fallback.
        """

        if self.real_llm_plan:
            if (self.min_sections, self.max_sections) != (8, 10):
                raise ValueError(
                    "Production blueprint planning enforces an authoritative "
                    f"8-10 chapter contract; configured range is "
                    f"[{self.min_sections}, {self.max_sections}]."
                )
            planner_output = self._production_architecture(evidence)
            sections = self._sections_from_llm_plan(
                planner_output,
                evidence,
                allow_deterministic_completion=False,
            )
            return sections, planner_output, True
        sections = self._build_dynamic_sections(evidence)
        return sections, {}, False

    def _strip_stale_grounding_state(
        self, architecture: dict[str, Any]
    ) -> dict[str, Any]:
        """Rebuild the grounder request from a grounded checkpoint OR a final
        review blueprint artifact using an explicit intellectual whitelist.

        The Qwen intellectual contract is preserved exactly: section
        id/title/role/contribution/coverage/axes/handoffs/questions/claim
        seeds/visual goals/candidate search seeds/writing requirements/
        evidence risks/transitions, plus architecture review_thesis,
        narrative_strategy, high_value_gap_seeds, and input context.  All
        material/downstream-derived state (candidate pools, claim pools,
        claims, bindings, digests, dossiers, grounding audits, transport
        menus, generated/rematerialization fields) is removed so the new
        candidate inventory can only reflect the currently configured library.
        """
        sections: list[dict[str, Any]] = []
        for raw in architecture.get("sections") or []:
            section: dict[str, Any] = {}
            if isinstance(raw, dict):
                for key in _FORCED_REGROUND_SECTION_INTELLECTUAL_KEYS:
                    if key in raw:
                        section[key] = raw[key]
            sections.append(section)
        stripped: dict[str, Any] = {"sections": sections}
        for key in _FORCED_REGROUND_ARCHITECTURE_PRESERVED_KEYS:
            if key in architecture:
                stripped[key] = architecture[key]
        stripped["_forced_reground"] = {
            "requested": True,
            "source_checkpoint": (
                str(self.planner_architecture_path.resolve())
                if self.planner_architecture_path
                else ""
            ),
            "policy": (
                "Preserve the Qwen intellectual chapter division (including "
                "candidate_search_seeds, evidence_risks, and "
                "high_value_gap_seeds); rebuild all section evidence "
                "grounding, candidate pools, claim pools, and claim bindings "
                "from the currently configured material library and vector "
                "cache."
            ),
        }
        return stripped

    def _production_architecture(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Return one authoritative real architecture or fail closed."""

        if self.planner_architecture_path and self.planner_architecture_path.exists():
            loaded_architecture = safe_json_parse(
                self.planner_architecture_path.read_text(encoding="utf-8")
            )
            if not loaded_architecture:
                raise ValueError(
                    f"Reusable planner architecture could not be parsed: "
                    f"{self.planner_architecture_path}"
                )
            raw_sections = (
                loaded_architecture.get("sections")
                if isinstance(loaded_architecture, dict)
                else None
            )
            if not isinstance(raw_sections, list):
                raise ValueError(
                    "Reusable planner architecture has no sections list"
                )
            if not raw_sections:
                raise ValueError(
                    "Reusable planner architecture has an empty sections list"
                )
            self._repair_schema_aliases(loaded_architecture)
            self._require_review_thesis(loaded_architecture)
            warnings = self._validate_raw_section_division(raw_sections)
            loaded_architecture["_architecture_contract_warnings"] = warnings
            loaded_architecture["_planner_reused_path"] = str(
                self.planner_architecture_path.resolve()
            )
            if self.force_reground:
                loaded_architecture = self._strip_stale_grounding_state(
                    loaded_architecture
                )
                grounded = self._ground_blueprint_architecture(
                    loaded_architecture, evidence
                )
                grounded["_reused_grounded_architecture"] = False
                grounded["_architecture_contract_warnings"] = list(warnings)
                return grounded
            if self._is_complete_grounded_architecture(
                loaded_architecture
            ):
                loaded_architecture["_reused_grounded_architecture"] = True
                return loaded_architecture
            grounded = self._ground_blueprint_architecture(
                loaded_architecture, evidence
            )
            grounded["_reused_grounded_architecture"] = False
            grounded["_architecture_contract_warnings"] = list(warnings)
            return grounded
        planner_output = self._llm_plan_blueprint(evidence)
        raw_sections = (
            planner_output.get("sections")
            if isinstance(planner_output, dict)
            else None
        )
        if (
            not isinstance(planner_output, dict)
            or not isinstance(raw_sections, list)
            or not raw_sections
        ):
            raise ValueError(
                "real_llm_plan=True but the Qwen architecture plan is empty, "
                "invalid, or structurally unusable; refusing deterministic "
                "fallback"
            )
        return planner_output

    def _validate_raw_section_division(
        self,
        raw_sections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate the Qwen-authored chapter division; fail closed on gaps.

        Hard failures cover: the authoritative 8-10 chapter-count range,
        section objects, unique non-empty section IDs, non-empty
        title/argument_role/unique_contribution, non-empty
        must_cover/must_not_cover/assigned_user_axes, and non-empty
        handoff_from_previous/handoff_to_next.  Handoffs are scientific
        organization owned by Qwen; Python never invents them and never accepts
        an architecture that omits them.  Every must_not_cover item must name a
        concrete sibling-owned responsibility (sibling section ID plus a
        distinctive token from that sibling's title/argument_role/
        unique_contribution).

        Returns an empty list on success (no advisory warnings remain for
        required division fields).
        """

        range_is_authoritative = (
            self.min_sections,
            self.max_sections,
        ) == (8, 10)
        if range_is_authoritative and not (
            self.min_sections <= len(raw_sections) <= self.max_sections
        ):
            raise ValueError(
                f"Qwen architecture must contain {self.min_sections}-"
                f"{self.max_sections} chapters; received {len(raw_sections)}. "
                "Python refuses to invent a deterministic outline for any "
                "other count."
            )
        scalar_fields = (
            "section_id",
            "title",
            "argument_role",
            "unique_contribution",
            "handoff_from_previous",
            "handoff_to_next",
        )
        list_fields = (
            "must_cover",
            "must_not_cover",
            "assigned_user_axes",
        )
        warnings: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_sections):
            if not isinstance(raw, dict):
                raise ValueError(
                    f"architecture section {index} is not an object"
                )
            for field in scalar_fields:
                value = str(raw.get(field) or "").strip()
                if not value:
                    raise ValueError(
                        f"architecture section {index} missing non-empty "
                        f"field {field}"
                    )
            for field in list_fields:
                values = raw.get(field)
                if not isinstance(values, list) or not any(
                    str(item).strip() for item in values
                ):
                    raise ValueError(
                        f"architecture section {index} missing non-empty "
                        f"field {field}"
                    )
            section_id = str(raw.get("section_id") or "").strip()
            if section_id in seen_ids:
                raise ValueError(
                    f"duplicate architecture section_id {section_id!r}"
                )
            seen_ids.add(section_id)
        if range_is_authoritative:
            concrete_errors = _concrete_sibling_exclusion_errors(raw_sections)
            if concrete_errors:
                raise ValueError(
                    "Qwen architecture must_not_cover entries must name concrete "
                    "sibling-owned responsibilities: "
                    + "; ".join(concrete_errors)
                )
        return warnings

    @staticmethod
    def _normalize_schema_key(value: Any) -> str:
        """Lowercase alphanumeric key for conservative similarity checks."""

        return re.sub(
            r"[^a-z0-9]",
            "",
            str(value or "").lower(),
        )

    def _repair_schema_aliases(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Recover a missing canonical ``review_thesis`` from a typo key.

        Generic, conservative repair: when the canonical key is missing/empty,
        inspect unknown top-level string fields and accept only one
        unambiguous high-similarity candidate.  The original key is kept, the
        repair is recorded in ``_schema_alias_repairs``, and no scientific text
        is synthesized.
        """

        repairs = list(payload.get("_schema_alias_repairs") or [])
        payload["_schema_alias_repairs"] = repairs
        if str(payload.get("review_thesis") or "").strip():
            return payload
        target = self._normalize_schema_key("review_thesis")
        candidates: list[tuple[float, str, str]] = []
        for key, value in payload.items():
            if key == "review_thesis" or str(key).startswith("_"):
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            similarity = difflib.SequenceMatcher(
                None,
                self._normalize_schema_key(key),
                target,
            ).ratio()
            if similarity >= 0.85:
                candidates.append((similarity, str(key), str(value).strip()))
        if len(candidates) == 1:
            similarity, source_key, text = candidates[0]
            payload["review_thesis"] = text
            repairs.append(
                {
                    "source_key": source_key,
                    "target_key": "review_thesis",
                    "method": "normalized_key_similarity",
                    "similarity": round(similarity, 6),
                }
            )
        elif len(candidates) > 1:
            repairs.append(
                {
                    "source_key": None,
                    "target_key": "review_thesis",
                    "method": "normalized_key_similarity",
                    "status": "ambiguous",
                    "candidate_keys": [key for _, key, _ in candidates],
                }
            )
        payload["_schema_alias_repairs"] = repairs
        return payload

    @staticmethod
    def _require_review_thesis(payload: dict[str, Any]) -> None:
        """Fail closed when no canonical thesis exists after alias repair."""

        if not str(payload.get("review_thesis") or "").strip():
            raise ValueError(
                "real_llm_plan=True architecture has no non-empty canonical "
                "review_thesis and no safe schema alias; raw output is "
                "preserved"
            )

    def _is_complete_grounded_architecture(
        self,
        payload: dict[str, Any],
    ) -> bool:
        """Conservatively detect a fully grounded architecture.

        A reused architecture is already grounded only when the grounding
        summary matches the section count and every section carries a
        completed grounding status plus a non-empty candidate material pool
        and text ids.  Incomplete/raw architectures still use the grounder.
        """

        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            return False
        summary = payload.get("_grounding_summary") or {}
        if not isinstance(summary, dict):
            return False
        if int(summary.get("sections") or 0) != len(sections):
            return False
        for section in sections:
            if not isinstance(section, dict):
                return False
            if not str(section.get("_grounding_status") or "").strip():
                return False
            text_ids = section.get("text_chunk_ids") or []
            if not isinstance(text_ids, list) or not text_ids:
                return False
            pool = section.get("candidate_material_pool") or {}
            if not isinstance(pool, dict):
                return False
            served = pool.get("served_chunk_ids") or []
            if not isinstance(served, list) or not served:
                return False
        return True

    def _attach_section_workplan_context(
        self,
        sections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Package the global division of labor onto every section.

        This is deterministic local packaging of the Qwen architecture
        decision: it formats ``full_section_workplan``,
        ``current_section_boundary_contract``, and
        ``sibling_section_responsibilities`` from the already-normalized
        sections.  It never invents, reorders, or replans sections.
        """

        workplan = [
            {
                "section_id": str(section.get("section_id") or ""),
                "title": compact(section.get("title"), 180),
                "argument_role": compact(section.get("argument_role"), 520),
                "unique_contribution": compact(
                    section.get("unique_contribution")
                    or section.get("novel_contribution_to_review"),
                    600,
                ),
                "must_cover": clean_list(section.get("must_cover"), 12),
                "must_not_cover": clean_list(section.get("must_not_cover"), 12),
                "assigned_user_axes": clean_list(
                    section.get("assigned_user_axes"), 8
                ),
                "key_questions": clean_list(section.get("key_questions"), 6),
                "handoff_from_previous": compact(
                    section.get("handoff_from_previous"), 420
                ),
                "handoff_to_next": compact(
                    section.get("handoff_to_next")
                    or section.get("transition_to_next"),
                    420,
                ),
            }
            for section in sections
        ]
        for section in sections:
            section_id = str(section.get("section_id") or "")
            boundary = {
                "section_id": section_id,
                "title": compact(section.get("title"), 180),
                "argument_role": compact(section.get("argument_role"), 520),
                "unique_contribution": compact(
                    section.get("unique_contribution")
                    or section.get("novel_contribution_to_review"),
                    600,
                ),
                "must_cover": clean_list(section.get("must_cover"), 12),
                "must_not_cover": clean_list(section.get("must_not_cover"), 12),
                "assigned_user_axes": clean_list(
                    section.get("assigned_user_axes"), 8
                ),
                "key_questions": clean_list(section.get("key_questions"), 6),
                "handoff_from_previous": compact(
                    section.get("handoff_from_previous"), 420
                ),
                "handoff_to_next": compact(
                    section.get("handoff_to_next")
                    or section.get("transition_to_next"),
                    420,
                ),
            }
            section["current_section_boundary_contract"] = boundary
            section["sibling_section_responsibilities"] = [
                row
                for row in workplan
                if str(row.get("section_id") or "") != section_id
            ]
            section["full_section_workplan"] = list(workplan)
        return sections

    @staticmethod
    def _chunk_batch_map(digest: Mapping[str, Any]) -> dict[str, str]:
        """Map every digest chunk_id to its evidence batch_id."""

        batch_map: dict[str, str] = {}
        for batch in digest.get("batches") or []:
            if not isinstance(batch, dict):
                continue
            batch_id = str(batch.get("batch_id") or "")
            for chunk_id in batch.get("chunk_ids") or []:
                chunk_id = str(chunk_id or "")
                if chunk_id and batch_id:
                    batch_map[chunk_id] = batch_id
        return batch_map

    def _candidate_material_pool(
        self,
        *,
        candidate_chunk_ids: list[str],
        candidate_paper_ids: list[str],
        served_chunk_ids: list[str],
        model_context_chunk_ids: list[str],
        compression_policy: str,
    ) -> dict[str, Any]:
        """Build the audited section candidate pool contract.

        ``candidate_chunk_ids`` is the full retained/reopenable pool.
        ``served_chunk_ids`` is what this stage actively serves; it may be a
        deliberately constrained subset, and any such truncation is reported
        explicitly.  The 150-200 range is advisory only.
        """
        retained_count = len(candidate_chunk_ids)
        served_count = len(served_chunk_ids)
        return {
            "schema_version": "review_blueprint.candidate_material_pool.v1",
            "candidate_chunk_ids": list(candidate_chunk_ids),
            "candidate_paper_ids": list(candidate_paper_ids),
            "served_chunk_ids": list(served_chunk_ids),
            "served_limit": self.served_text_limit,
            "model_context_chunk_ids": list(model_context_chunk_ids),
            "model_context_limit": self.model_text_context_limit,
            "preferred_section_text_candidate_range": list(
                PREFERRED_SECTION_TEXT_CANDIDATE_RANGE
            ),
            "advisory_target_policy": (
                "planning_target_not_quota_or_admission_gate"
            ),
            "retained_candidate_count": retained_count,
            "served_candidate_count": served_count,
            "candidate_pool_status": advisory_candidate_pool_status(
                retained_count
            ),
            "hard_cut": False,
            "hard_200th_cutoff": False,
            "explicit_limit_applied": self.served_text_limit is not None,
            "explicit_limit_truncated": bool(
                self.served_text_limit is not None
                and served_count < retained_count
            ),
            "compression_policy": compression_policy,
            "all_candidates_visible_to_grounder": True,
            "visible_candidate_count": served_count,
        }

    def _section_candidate_pool_audit(
        self, sections: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Summarize every section's advisory pool status for the blueprint."""
        status_counts: Counter[str] = Counter()
        above: list[str] = []
        below: list[str] = []
        retained_total = 0
        served_total = 0
        for section in sections:
            pool = section.get("candidate_material_pool") or {}
            if not isinstance(pool, dict):
                continue
            retained = int(pool.get("retained_candidate_count") or 0)
            served = int(pool.get("served_candidate_count") or 0)
            status = str(pool.get("candidate_pool_status") or "")
            retained_total += retained
            served_total += served
            if status:
                status_counts[status] += 1
            section_id = str(section.get("section_id") or "")
            if status == "above_target_range":
                above.append(section_id)
            elif status == "below_target_range":
                below.append(section_id)
        return {
            "preferred_section_text_candidate_range": list(
                PREFERRED_SECTION_TEXT_CANDIDATE_RANGE
            ),
            "policy": (
                "Advisory planning target; not a quota, admission gate, or "
                "hard cutoff.  Sections above the range retain every candidate."
            ),
            "sections_audited": len(sections),
            "status_counts": dict(status_counts),
            "above_target_range_sections": above,
            "below_target_range_sections": below,
            "retained_candidate_total": retained_total,
            "served_candidate_total": served_total,
        }

    def _select_raw_dossier_representatives(
        self,
        chunks: list[dict[str, Any]],
        batch_map: Mapping[str, str],
        *,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Deterministically select at most ``limit`` raw source dossiers.

        This is transport packaging only: it never chooses chapters or claims.
        Selection spans the full ranked chunk list and is diverse across
        digest batches and distinct papers, with ranked fill for the remainder.
        The full candidate summaries/propositions remain visible in
        ``evidence_digest``; only raw dossier context is bounded here.
        """

        limit = max(1, int(limit))
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        selected_papers: set[str] = set()

        def add(chunk: dict[str, Any]) -> bool:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id or chunk_id in selected_ids:
                return False
            selected.append(chunk)
            selected_ids.add(chunk_id)
            paper_id = str(chunk.get("paper_id") or "").strip()
            if paper_id:
                selected_papers.add(paper_id)
            return True

        # Pass 1: one representative per digest batch, in batch order.
        for batch_id in dict.fromkeys(batch_map.values()):
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id") or "")
                if batch_map.get(chunk_id) == batch_id and add(chunk):
                    break
            if len(selected) >= limit:
                break
        # Pass 2: distinct papers in full ranked order.
        if len(selected) < limit:
            for chunk in chunks:
                paper_id = str(chunk.get("paper_id") or "").strip()
                if paper_id and paper_id not in selected_papers:
                    add(chunk)
                if len(selected) >= limit:
                    break
        # Pass 3: ranked fill for the remainder.
        if len(selected) < limit:
            for chunk in chunks:
                add(chunk)
                if len(selected) >= limit:
                    break
        metadata = {
            "total_candidate_count": len(chunks),
            "served_raw_dossier_count": len(selected),
            "selection_strategy": "diverse_ranked_batch_paper_sample",
            "omitted_count": max(0, len(chunks) - len(selected)),
            "raw_dossier_limit": limit,
        }
        return selected, metadata

    def _llm_plan_blueprint(self, evidence: dict[str, Any]) -> dict[str, Any]:
        if not self.planner_prompt_path.exists():
            return {}
        prompt = self.planner_prompt_path.read_text(encoding="utf-8")
        command_knowledge = _command_knowledge_block()
        prompt = (
            prompt.strip()
            + "\n\n"
            + "[COMMAND_KNOWLEDGE (versioned skill guidance; not scientific "
            "evidence)]\n"
            + command_knowledge["prompt_block"]
        )
        compact_evidence = self._compact_evidence_for_llm(evidence)
        all_chunks = [
            row
            for row in evidence.get("retrieved_text_chunks") or []
            if isinstance(row, dict) and str(row.get("chunk_id") or "")
        ]
        batch_map = self._chunk_batch_map(
            compact_evidence.get("evidence_digest") or {}
        )
        selected_chunks, serving_metadata = (
            self._select_raw_dossier_representatives(
                all_chunks, batch_map, limit=20
            )
        )
        material_layer = build_section_evidence_material_layer(
            selected_chunks,
            self.material_units_by_chunk_id,
            chunk_limit=20,
        )
        material_layer["raw_dossier_serving"] = serving_metadata
        # Show-then-Pick: give LLM the full menu of available anchor IDs so it can
        # reference them correctly. Without this, LLM invents IDs → sections are dropped.
        payload = {
            "input_context": {
                "user_question": self.user_question,
                "problem_understanding": self.problem_understanding,
                "scope_definition": self.scope_definition,
            },
            "review_example_structure_memory": self._review_example_anchor(),
            "evidence_landscape": compact_evidence,
            "evidence_material_layer": material_layer,
            "command_knowledge": command_knowledge,
            "m1_case_moves": (
                _m1_case_moves_payload(
                    compact_evidence.get("review_mentor_advice") or {}
                )
            ),
            "rules": {
                "section_count": f"{self.min_sections}-{self.max_sections}",
                "section_count_is_recommendation_only": False,
                "section_count_is_hard_contract": True,
                "authoritative_section_count_range": [
                    self.min_sections,
                    self.max_sections,
                ],
                "chapter_count_owner": (
                    "Qwen owns which 8-10 chapters and their scientific "
                    "organization; Python enforces the range and rejects any "
                    "other count without deterministic repair"
                ),
                "final_section_count_owner": "Qwen planner (which chapters); Python (range 8-10)",
                "preferred_section_text_candidate_range": list(
                    PREFERRED_SECTION_TEXT_CANDIDATE_RANGE
                ),
                "section_text_candidate_policy": (
                    "advisory_target_not_quota_or_admission_gate"
                ),
                "no_hard_200th_cut": True,
                "architecture_stage_must_not_output_asset_ids": True,
                "max_high_value_gap_seeds": 5,
                "language": "English only",
            },
        }
        attempts: list[dict[str, Any]] = []
        try:
            previous_timeout = os.environ.get("QWEN_HTTP_TIMEOUT_SEC")
            previous_max_keys = os.environ.get("QWEN_MAX_KEY_CANDIDATES")
            os.environ["QWEN_HTTP_TIMEOUT_SEC"] = os.environ.get("QWEN_PLANNER_HTTP_TIMEOUT_SEC", "240")
            os.environ["QWEN_MAX_KEY_CANDIDATES"] = os.environ.get("QWEN_PLANNER_MAX_KEY_CANDIDATES", "2")

            # Use the requested top-tier route once; the chat client applies
            # the configured model fallback internally.  Bounding one compact
            # architecture call prevents a stalled endpoint from blocking the
            # whole review workflow for tens of minutes.
            attempt_tiers = [self.planner_model_tier]
            for attempt_index, model_tier in enumerate(attempt_tiers, 1):
                result = call_qwen_chat(
                    "DynamicReviewBlueprintPlannerAgent",
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier=model_tier,
                    temperature=0.2,
                    max_tokens=self.planner_max_tokens,
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    force_mock=False,
                    max_retries=0,
                    stream=True,
                )
                raw = str(result.get("content") or "")
                try:
                    strict_value = json.loads(raw)
                    strict_json_valid = isinstance(strict_value, dict)
                except Exception:
                    strict_value = {}
                    strict_json_valid = False
                parsed = strict_value if strict_json_valid else safe_json_parse(raw)
                raw_sections = parsed.get("sections") if isinstance(parsed, dict) else None
                validation_diagnostics: dict[str, Any] = {
                    "handoff_warnings": [],
                    "core_division": "pending",
                }
                attempt_record = {
                    "attempt": attempt_index,
                    "model_tier": model_tier,
                    "usable": False,
                    "raw_chars": len(raw),
                    "section_count": (
                        len(raw_sections)
                        if isinstance(raw_sections, list)
                        else 0
                    ),
                    "strict_json_valid": strict_json_valid,
                    "repaired_from_partial_json": (
                        bool(parsed) and not strict_json_valid
                    ),
                    "usage": result.get("_llm_usage", {}),
                    "raw_preview": compact(raw, 500),
                    "validation_diagnostics": validation_diagnostics,
                }
                attempts.append(attempt_record)
                # Preserve the paid raw response and call/usage record before
                # contract validation so a hard failure never loses them.
                self.output_dir.mkdir(parents=True, exist_ok=True)
                (
                    self.output_dir / "raw_review_architecture_output.json.txt"
                ).write_text(raw, encoding="utf-8")
                write_json(
                    self.output_dir / "dynamic_planner_attempts.json",
                    {"created_at": utc_now(), "attempts": attempts},
                )
                architecture_warnings: list[dict[str, Any]] = []
                if isinstance(parsed, dict) and isinstance(raw_sections, list):
                    try:
                        self._repair_schema_aliases(parsed)
                        self._require_review_thesis(parsed)
                        architecture_warnings = (
                            self._validate_raw_section_division(raw_sections)
                        )
                    except ValueError as exc:
                        validation_diagnostics["core_division"] = "invalid"
                        validation_diagnostics["error"] = str(exc)
                        attempt_record["validation_diagnostics"] = (
                            validation_diagnostics
                        )
                        write_json(
                            self.output_dir
                            / "dynamic_planner_attempts.json",
                            {
                                "created_at": utc_now(),
                                "attempts": attempts,
                            },
                        )
                        raise
                    validation_diagnostics["handoff_warnings"] = (
                        architecture_warnings
                    )
                    validation_diagnostics["core_division"] = "valid"
                    parsed["_architecture_contract_warnings"] = (
                        architecture_warnings
                    )
                    attempt_record["validation_diagnostics"] = (
                        validation_diagnostics
                    )
                    write_json(
                        self.output_dir / "dynamic_planner_attempts.json",
                        {"created_at": utc_now(), "attempts": attempts},
                    )
                structurally_usable = (
                    isinstance(parsed, dict)
                    and isinstance(raw_sections, list)
                    and bool(raw_sections)
                )
                attempt_record["usable"] = structurally_usable
                if structurally_usable:
                    # The Qwen global planner owns the scientific division of
                    # labor.  Local code must not append mentor-proposed
                    # sections to a real architecture.
                    parsed["_scope_coverage_repair"] = {
                        "enabled": False,
                        "added_sections": [],
                        "remaining_capacity": max(
                            0, self.max_sections - len(raw_sections)
                        ),
                        "status": (
                            "global_architecture_owns_section_division"
                        ),
                    }
                    self.output_dir.mkdir(parents=True, exist_ok=True)
                    (self.output_dir / "raw_review_architecture_output.json.txt").write_text(raw, encoding="utf-8")
                    write_json(
                        self.output_dir / "dynamic_planner_attempts.json",
                        {"created_at": utc_now(), "attempts": attempts},
                    )
                    parsed["_llm_usage"] = result.get("_llm_usage", {})
                    parsed["_planner_attempt_count"] = attempt_index
                    parsed["_architecture_json_repaired"] = not strict_json_valid
                    grounded = self._ground_blueprint_architecture(parsed, evidence)
                    grounded["_architecture_contract_warnings"] = list(
                        architecture_warnings
                    )
                    (self.output_dir / "raw_dynamic_planner_output.json.txt").write_text(
                        json.dumps(grounded, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    return grounded

            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "raw_dynamic_planner_output.json.txt").write_text(
                str(attempts[-1].get("raw_preview") if attempts else ""), encoding="utf-8"
            )
            write_json(
                self.output_dir / "dynamic_planner_attempts.json",
                {"created_at": utc_now(), "attempts": attempts},
            )
            return {}
        except ValueError:
            # Qwen-required architecture contract violations (missing division
            # fields, missing handoffs, or a chapter count outside 8-10) are
            # hard failures and must not be swallowed into an empty planner
            # output or repaired with a deterministic outline.
            raise
        except Exception as exc:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                self.output_dir / "dynamic_planner_error.json",
                {"error": type(exc).__name__, "message": str(exc), "created_at": utc_now(), "attempts": attempts},
            )
            return {}
        finally:
            if "previous_timeout" in locals():
                if previous_timeout is None:
                    os.environ.pop("QWEN_HTTP_TIMEOUT_SEC", None)
                else:
                    os.environ["QWEN_HTTP_TIMEOUT_SEC"] = previous_timeout
            if "previous_max_keys" in locals():
                if previous_max_keys is None:
                    os.environ.pop("QWEN_MAX_KEY_CANDIDATES", None)
                else:
                    os.environ["QWEN_MAX_KEY_CANDIDATES"] = previous_max_keys

    def _merge_missing_mentor_sections(
        self,
        architecture: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore user-required dimensions dropped after mentor convergence.

        The mentor is a structural teacher, not scientific evidence.  Its
        section proposals are therefore used only as an intent-coverage
        contract.  A proposal is appended when it contains distinctive terms
        from the user request and no current section substantially covers it.
        """
        out = dict(architecture)
        current = [dict(x) for x in (out.get("sections") or []) if isinstance(x, dict)]
        convergence = (
            ((evidence.get("review_mentor_advice") or {}).get("three_party_convergence") or {})
            if isinstance(evidence.get("review_mentor_advice"), dict)
            else {}
        )
        proposals = [
            dict(x) for x in (convergence.get("section_proposals") or [])
            if isinstance(x, dict) and (x.get("title") or x.get("argument_role"))
        ]
        if not current or not proposals:
            out["_scope_coverage_repair"] = {
                "enabled": bool(proposals),
                "added_sections": [],
                "remaining_capacity": max(0, self.max_sections - len(current)),
            }
            return out

        generic = {
            "survey", "research", "background", "significance", "current", "identify",
            "develop", "review", "expert", "level", "critical", "literature", "field",
            "passive", "daytime", "radiative", "cooling", "section", "directions",
        }
        user_tokens = set(tokenize(self._topic_text(), limit=100)) - generic

        def section_text(section: dict[str, Any]) -> str:
            questions = " ".join(clean_list(section.get("key_questions"), 6))
            return " ".join([
                str(section.get("title") or ""),
                str(section.get("argument_role") or ""),
                questions,
            ])

        current_token_sets = [set(tokenize(section_text(x), limit=100)) for x in current]
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for proposal in proposals:
            proposal_tokens = set(tokenize(section_text(proposal), limit=100))
            distinctive = proposal_tokens & user_tokens
            redundancy = max(
                (len(proposal_tokens & existing) / max(1, len(proposal_tokens)) for existing in current_token_sets),
                default=0.0,
            )
            if len(distinctive) < 2 or redundancy >= 0.32:
                skipped.append({
                    "title": compact(proposal.get("title"), 180),
                    "reason": "already_covered_or_not_user_distinctive",
                    "distinctive_terms": sorted(distinctive)[:12],
                    "redundancy": round(redundancy, 3),
                })
                continue
            if len(current) >= self.max_sections:
                skipped.append({
                    "title": compact(proposal.get("title"), 180),
                    "reason": "section_capacity_reached",
                    "distinctive_terms": sorted(distinctive)[:12],
                    "redundancy": round(redundancy, 3),
                })
                continue
            current.append(proposal)
            current_token_sets.append(proposal_tokens)
            added.append({
                "title": compact(proposal.get("title"), 180),
                "distinctive_terms": sorted(distinctive)[:12],
                "source": "three_party_mentor_intent_contract",
            })

        for idx, section in enumerate(current, 1):
            section["section_id"] = f"S{idx:02d}"
        out["sections"] = current
        out["_scope_coverage_repair"] = {
            "enabled": True,
            "added_sections": added,
            "skipped_proposals": skipped,
            "remaining_capacity": max(0, self.max_sections - len(current)),
            "status": "repaired" if added else "no_repair_needed",
        }
        return out

    @staticmethod
    def _architecture_claim_seeds(section: dict[str, Any]) -> list[dict[str, Any]]:
        seeds = section.get("claim_seeds") if isinstance(section.get("claim_seeds"), list) else []
        out: list[dict[str, Any]] = []
        for item in seeds[:4]:
            if isinstance(item, dict):
                statement = compact(item.get("claim_seed"), 420)
                relation = compact(item.get("relation_to_section"), 40) or "support"
                relation_roles = clean_list(item.get("relation_roles") or [relation], 8)
                counterevidence_query = compact(item.get("counterevidence_query"), 260)
                boundary_conditions = clean_list(item.get("boundary_conditions"), 6)
                axis_assignments = [
                    dict(axis) for axis in (item.get("axis_assignments") or [])
                    if isinstance(axis, dict) and axis.get("axis_id")
                ][:6]
                counterevidence_ids = clean_list(item.get("counterevidence_text_chunk_ids"), 4)
                boundary_ids = clean_list(item.get("boundary_text_chunk_ids"), 4)
                background_ids = clean_list(item.get("background_text_chunk_ids"), 4)
            else:
                statement = compact(item, 420)
                relation = "support"
                relation_roles = [relation]
                counterevidence_query = ""
                boundary_conditions = []
                axis_assignments = []
                counterevidence_ids = []
                boundary_ids = []
                background_ids = []
            if statement:
                out.append(
                    {
                        "claim_seed": statement,
                        "relation_to_section": relation,
                        "relation_roles": relation_roles,
                        "counterevidence_query": counterevidence_query,
                        "boundary_conditions": boundary_conditions,
                        "axis_assignments": axis_assignments,
                        "counterevidence_text_chunk_ids": counterevidence_ids,
                        "boundary_text_chunk_ids": boundary_ids,
                        "background_text_chunk_ids": background_ids,
                    }
                )
        return out

    def _section_architecture_query(self, section: dict[str, Any]) -> str:
        claims = " ".join(x["claim_seed"] for x in self._architecture_claim_seeds(section))
        visual_goals = section.get("visual_argument_goals") if isinstance(section.get("visual_argument_goals"), list) else []
        visual_text = " ".join(
            compact(x.get("goal") if isinstance(x, dict) else x, 180) for x in visual_goals[:2]
        )
        return " ".join(
            [
                compact(section.get("title"), 180),
                compact(section.get("argument_role"), 520),
                " ".join(clean_list(section.get("key_questions"), 3)),
                claims,
                visual_text,
            ]
        )

    def _section_semantic_text_scores(
        self, query: str
    ) -> tuple[dict[str, float], dict[str, Any]]:
        """Score section candidates via the configured semantic vector cache.

        Returns (chunk_id -> text-embedding-v4 cosine score, audit).  No
        scores means the semantic route was unavailable or failed; callers
        must use the audited lexical fallback instead of including everything.
        """
        if not str(query or "").strip():
            return {}, {"route": "unavailable", "reason": "empty_section_query"}
        if (
            not self.material_vectors_path
            or not self.material_vectors_path.exists()
            or not self.material_units
        ):
            return {}, {
                "route": "unavailable",
                "reason": "no_material_semantic_cache",
            }
        try:
            from optomind_research.runtime.material_semantic_cache import (
                MaterialSemanticCache,
                dashscope_embedder,
            )

            usage: dict[str, int] = {"input_tokens": 0, "request_count": 0}
            vectors = dashscope_embedder(
                [str(query).strip()],
                model=self.material_embedding_model,
                usage_accumulator=usage,
                max_retries=1,
            )
            if not vectors:
                return {}, {
                    "route": "semantic_failed",
                    "reason": "empty_embedding",
                    "usage": dict(usage),
                }
            chunk_id_by_unit_id = {
                # Material units store unit_id at the top level, exactly as
                # _retrieve_material_vectors keys its unit_by_id map.  Reading
                # identity.unit_id would silently score zero units.
                str(unit.get("unit_id") or ""): str(
                    (unit.get("identity") or {}).get("chunk_id") or ""
                )
                for unit in self.material_units
            }
            scores: dict[str, float] = {}
            with MaterialSemanticCache(self.material_vectors_path) as cache:
                cache_units = cache.count()
                hits = cache.search(
                    vectors[0],
                    top_k=max(1, len(self.material_units)),
                    embedding_model=self.material_embedding_model,
                )
                for hit in hits:
                    chunk_id = chunk_id_by_unit_id.get(
                        str(hit.get("unit_id") or "")
                    )
                    if chunk_id:
                        scores[chunk_id] = float(hit.get("score") or 0.0)
            return scores, {
                "route": "material_semantic_cache",
                "model": self.material_embedding_model,
                "query_chars": len(str(query).strip()),
                "scored_units": len(scores),
                "cache_units": cache_units,
                "usage": dict(usage),
                "error": "",
            }
        except Exception as exc:
            return {}, {
                "route": "semantic_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _score_distribution_buckets(
        scores: list[float], threshold: float
    ) -> dict[str, int]:
        buckets = {
            "admitted_ge_0_70": 0,
            "admitted_0_50_to_0_70": 0,
            "admitted_under_0_50": 0,
            "excluded_below_threshold": 0,
        }
        for score in scores:
            if score < threshold:
                buckets["excluded_below_threshold"] += 1
            elif score >= 0.70:
                buckets["admitted_ge_0_70"] += 1
            elif score >= 0.50:
                buckets["admitted_0_50_to_0_70"] += 1
            else:
                buckets["admitted_under_0_50"] += 1
        return buckets

    def _materialize_semantic_candidate_rows(
        self, scores: dict[str, float]
    ) -> list[dict[str, Any]]:
        """Build full candidate rows from the durable material library.

        The global planning inventory (all_text) is not a gate: every cached
        unit with a chunk_id is materialized with its identity, durable text,
        material-card binding, and admission score.
        """
        unit_by_chunk_id = {
            str((unit.get("identity") or {}).get("chunk_id") or ""): unit
            for unit in self.material_units
        }
        rows: list[dict[str, Any]] = []
        for chunk_id, score in scores.items():
            unit = unit_by_chunk_id.get(str(chunk_id or ""))
            if not unit:
                continue
            identity = (
                unit.get("identity")
                if isinstance(unit.get("identity"), dict)
                else {}
            )
            durable = (
                unit.get("durable_content")
                if isinstance(unit.get("durable_content"), dict)
                else {}
            )
            row = self._attach_material_binding(
                {
                    "chunk_id": str(chunk_id),
                    "paper_id": identity.get("paper_id", ""),
                    "source_paper_id": identity.get("paper_id", ""),
                    "doi": identity.get("doi", ""),
                    "title": compact(identity.get("title"), 180),
                    "section_path": compact(durable.get("section_path"), 120),
                    "text_preview": compact(
                        durable.get("normalized_text")
                        or durable.get("raw_text"),
                        1800,
                    ),
                    "retrieval_query": "section_semantic_admission",
                    "retrieval_source": "material_semantic_cache",
                },
                unit,
            )
            row["admission_score"] = round(float(score), 4)
            row["match_score"] = round(float(score), 4)
            row["admission_route"] = "material_semantic_cache"
            rows.append(row)
        return rows

    def _lexical_score_rows(
        self,
        query: str,
        items: list[dict[str, Any]],
    ) -> list[tuple[float, dict[str, Any]]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("chunk_id"):
                continue
            candidate_text = " ".join(
                compact(item.get(field), 420)
                for field in (
                    "title",
                    "section_path",
                    "text_preview",
                    "material_binding_search_text",
                )
            )
            scored.append(
                (text_overlap_score(query, candidate_text), item)
            )
        return scored

    def _apply_admission_threshold(
        self,
        scored_rows: list[tuple[float, dict[str, Any]]],
        *,
        relative_floor: float,
        absolute_floor: float,
        route: str,
        anchor_scores: list[float] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Apply the adaptive + rank-200 anchored admission threshold.

        threshold = max(absolute_floor, best*relative_floor,
        score_at_rank_200).  This is not a slice/top-N: every candidate with
        score >= threshold is retained, including exact-score ties beyond rank
        200.  Sparse sections stay below the advisory target and are never
        padded.
        """
        positive = [
            (float(score), row)
            for score, row in scored_rows
            if float(score) > 0.0
        ]
        if not positive:
            return [], {
                "route": route,
                "reason": "no_positive_scores",
                "best_score": 0.0,
                "threshold": 0.0,
                "threshold_components": {
                    "absolute_floor": absolute_floor,
                    "relative_floor_value": 0.0,
                    "target_anchor_score": 0.0,
                },
                "target_anchor_score": 0.0,
                "scored_count": 0,
                "candidate_count": 0,
                "admitted_count": 0,
                "excluded_count": 0,
                "zero_or_negative_count": max(
                    0, len(scored_rows)
                ),
                "tie_extension_count": 0,
                "advisory_status": advisory_candidate_pool_status(0),
                "policy": (
                    "semantic score floor: max(absolute, best*relative, "
                    "score at rank 200); every score >= threshold retained "
                    "including ties; no top-N and no 200-item slice; sparse "
                    "sections are not padded"
                ),
                "score_distribution": self._score_distribution_buckets(
                    [], 0.0
                ),
            }
        best_score = max(score for score, _row in positive)
        if anchor_scores is not None:
            score_values = sorted(
                (float(value) for value in anchor_scores), reverse=True
            )
        else:
            score_values = sorted(
                (score for score, _row in positive), reverse=True
            )
        target_anchor_score = (
            score_values[199]
            if len(score_values) >= 200
            else (score_values[-1] if score_values else 0.0)
        )
        relative_value = best_score * relative_floor
        threshold = max(
            absolute_floor, relative_value, target_anchor_score
        )
        admitted = [
            (score, row)
            for score, row in positive
            if score >= threshold
        ]
        admitted.sort(
            key=lambda pair: (
                -pair[0],
                str(pair[1].get("chunk_id") or ""),
            )
        )
        admitted_rows: list[dict[str, Any]] = []
        for score, row in admitted:
            materialized = dict(row)
            materialized["admission_score"] = round(score, 4)
            materialized["match_score"] = round(score, 4)
            materialized["admission_route"] = route
            admitted_rows.append(materialized)
        tie_extension_count = (
            max(0, len(admitted_rows) - 200)
            if threshold == target_anchor_score
            else 0
        )
        all_scores = [score for score, _row in positive]
        return admitted_rows, {
            "route": route,
            "best_score": round(best_score, 4),
            "threshold": round(threshold, 4),
            "threshold_components": {
                "absolute_floor": absolute_floor,
                "relative_floor_value": round(relative_value, 4),
                "target_anchor_score": round(target_anchor_score, 4),
            },
            "target_anchor_score": round(target_anchor_score, 4),
            "anchor_score_rank": 200,
            "relative_floor": relative_floor,
            "absolute_floor": absolute_floor,
            "scored_count": len(score_values),
            "candidate_count": len(positive),
            "admitted_count": len(admitted_rows),
            "excluded_count": len(positive) - len(admitted_rows),
            "zero_or_negative_count": max(
                0, len(scored_rows) - len(positive)
            ),
            "tie_extension_count": tie_extension_count,
            "advisory_status": advisory_candidate_pool_status(
                len(admitted_rows)
            ),
            "policy": (
                "semantic score floor: max(absolute, best*relative, "
                "score at rank 200); every score >= threshold retained "
                "including ties; no top-N and no 200-item slice; sparse "
                "sections are not padded"
            ),
            "score_distribution": self._score_distribution_buckets(
                all_scores, threshold
            ),
        }

    def _admit_section_text_candidates(
        self,
        query: str,
        all_text: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Admit ordinary candidates from the full material library.

        Semantic rows are materialized directly from the complete
        material_units/cache hits for the section query; membership in the
        global planning inventory (all_text) is not a gate.  Truly
        non-material global candidates may enter as an audited lexical
        supplement.  If the semantic route fails, an audited lexical fallback
        applies the same anchored threshold shape.
        """
        semantic_scores, semantic_audit = self._section_semantic_text_scores(
            query
        )
        if semantic_scores:
            material_rows = self._materialize_semantic_candidate_rows(
                semantic_scores
            )
            if material_rows:
                scored_rows = [
                    (
                        float(
                            semantic_scores.get(
                                str(row.get("chunk_id") or ""), 0.0
                            )
                        ),
                        row,
                    )
                    for row in material_rows
                    if row.get("chunk_id")
                ]
                admitted_semantic, admission_audit = (
                    self._apply_admission_threshold(
                        scored_rows,
                        relative_floor=SECTION_SEMANTIC_ADMISSION_RELATIVE_FLOOR,
                        absolute_floor=SECTION_SEMANTIC_ADMISSION_ABSOLUTE_FLOOR,
                        route="material_semantic_cache",
                        anchor_scores=list(semantic_scores.values()),
                    )
                )
                materialized_ids = {
                    str(row.get("chunk_id")) for row in material_rows
                }
                supplement_candidates = [
                    row
                    for row in all_text
                    if isinstance(row, dict)
                    and str(row.get("chunk_id") or "")
                    not in materialized_ids
                ]
                supplement_rows: list[dict[str, Any]] = []
                supplement_audit: dict[str, Any] = {
                    "route": "lexical_supplement",
                    "candidates_considered": len(supplement_candidates),
                    "admitted_count": 0,
                }
                if supplement_candidates:
                    supplement_rows, supplement_audit = (
                        self._apply_admission_threshold(
                            self._lexical_score_rows(
                                query, supplement_candidates
                            ),
                            relative_floor=SECTION_LEXICAL_ADMISSION_RELATIVE_FLOOR,
                            absolute_floor=SECTION_LEXICAL_ADMISSION_ABSOLUTE_FLOOR,
                            route="lexical_supplement",
                        )
                    )
                    supplement_audit[
                        "candidates_considered"
                    ] = len(supplement_candidates)
                audit = dict(admission_audit)
                audit["semantic_route"] = semantic_audit
                audit["library_scores_count"] = len(semantic_scores)
                audit["materialized_count"] = len(material_rows)
                audit["supplement"] = supplement_audit
                return admitted_semantic + supplement_rows, audit

        # Semantic route unavailable/failed or produced no material rows:
        # audited section-specific lexical fallback with the same anchored
        # threshold shape.  Never a silent all-global inclusion.
        lexical_scored = self._lexical_score_rows(query, all_text)
        admitted_fallback, fallback_audit = self._apply_admission_threshold(
            lexical_scored,
            relative_floor=SECTION_LEXICAL_ADMISSION_RELATIVE_FLOOR,
            absolute_floor=SECTION_LEXICAL_ADMISSION_ABSOLUTE_FLOOR,
            route="lexical_material_card_fallback",
        )
        fallback_audit["semantic_route"] = semantic_audit
        if semantic_scores:
            fallback_audit["reason"] = "semantic_rows_materialization_empty"
        elif semantic_audit.get("reason"):
            fallback_audit["reason"] = semantic_audit["reason"]
        return admitted_fallback, fallback_audit

    @staticmethod
    def _rank_anchor_menu_items(
        query: str,
        items: list[dict[str, Any]],
        *,
        text_fields: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            candidate_text = " ".join(compact(item.get(field), 420) for field in text_fields)
            score = text_overlap_score(query, candidate_text)
            try:
                score += min(0.08, max(0.0, float(item.get("score") or 0.0)) * 0.02)
            except (TypeError, ValueError):
                pass
            scored.append((score, index, item))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in scored[:limit]]

    def _ground_blueprint_architecture(
        self,
        architecture: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach candidate KB assets after the intellectual outline is fixed.

        This deliberately separates "what the review must argue" from "what is
        already present in the current corpus".  Missing support therefore
        becomes a retrieval gap instead of distorting the outline.
        """
        raw_sections = architecture.get("sections") if isinstance(architecture.get("sections"), list) else []
        if not raw_sections:
            return architecture
        grounder_prompt = (
            DEFAULT_BLUEPRINT_GROUNDER_PROMPT.read_text(encoding="utf-8")
            if DEFAULT_BLUEPRINT_GROUNDER_PROMPT.exists()
            else ""
        )
        all_concepts = [x for x in evidence.get("selected_concept_nodes", []) if isinstance(x, dict)]
        all_text = [x for x in evidence.get("retrieved_text_chunks", []) if isinstance(x, dict)]
        all_visual = [x for x in evidence.get("retrieved_visual_chunks", []) if isinstance(x, dict)]

        def ground_one(index_and_section: tuple[int, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
            index, raw_section = index_and_section
            section = dict(raw_section) if isinstance(raw_section, dict) else {}
            section_id = compact(section.get("section_id"), 20) or f"S{index + 1:02d}"
            section["section_id"] = section_id
            query = self._section_architecture_query(section)
            concept_candidates = self._rank_anchor_menu_items(
                query, all_concepts, text_fields=("label", "planning_value", "view_name"), limit=10
            )
            # Section-specific semantic admission: score the global inventory
            # against this section's architecture query and keep only
            # candidates passing the permissive adaptive relevance rule.  The
            # old behavior of appending the complete global inventory whenever
            # served_text_limit=None is gone; sparse sections are not padded
            # and >200 strong candidates are all retained.
            text_inventory_candidates, admission_audit = (
                self._admit_section_text_candidates(query, all_text)
            )
            # The 12-item menu is a transport budget drawn only from the
            # admitted inventory; the durable pool keeps every admitted
            # candidate reopenable and digest-batched.
            text_candidates = text_inventory_candidates[:12]
            visual_candidates = self._rank_anchor_menu_items(
                query,
                all_visual,
                text_fields=("title", "caption_preview", "best_use_in_review", "visual_role"),
                limit=12,
            )
            # Show the grounder the semantically admitted inventory for this
            # section.  It may still select a small set of explicit bindings,
            # but no admitted candidate is hidden merely because it fell below
            # the 12-item transport menu.  The durable section pool below
            # keeps every admitted candidate reopenable for claim and
            # counterevidence work.
            visible_text_candidates = text_inventory_candidates
            menu = {
                "concept_nodes": [
                    {"id": x.get("node_id"), "label": compact(x.get("label"), 120), "planning_value": compact(x.get("planning_value"), 120)}
                    for x in concept_candidates if x.get("node_id")
                ],
                "text_chunks": [
                    {
                        "id": x.get("chunk_id"),
                        "paper_id": x.get("paper_id"),
                        "title": compact(x.get("title"), 100),
                        "preview": compact(x.get("text_preview"), 120),
                        "content_depth": x.get("content_depth"),
                        "use_permission": x.get("use_permission"),
                        "material_card_bound": bool(
                            isinstance(x.get("material_card_binding"), dict)
                            and x["material_card_binding"].get("bound")
                        ),
                        "propositions": [
                            {
                                "id": proposition.get("proposition_id"),
                                "statement": compact(proposition.get("statement"), 180),
                                "question_function": proposition.get("question_function"),
                                "evidence_ceiling": proposition.get("evidence_ceiling"),
                            }
                            for proposition in (
                                (x.get("material_card_binding") or {}).get("propositions") or []
                            )[:3]
                            if isinstance(proposition, dict)
                        ],
                    }
                    for x in visible_text_candidates if x.get("chunk_id")
                ],
                "text_inventory_policy": {
                    "visible_count": len(visible_text_candidates),
                    "selection_rule": (
                        "Review every admitted candidate; explicit bindings "
                        "are optional, but unselected admitted candidates "
                        "remain in the reopenable pool."
                    ),
                    "admission_route": admission_audit.get("route"),
                    "admission_threshold": admission_audit.get("threshold"),
                    "admission_best_score": admission_audit.get("best_score"),
                    "admission_excluded_count": admission_audit.get(
                        "excluded_count"
                    ),
                    "all_candidates_visible": True,
                },
                "evidence_digest": self._compact_evidence_for_llm(
                    {"retrieved_text_chunks": visible_text_candidates}
                ).get("evidence_digest", {}),
                "evidence_material_layer": build_section_evidence_material_layer(
                    visible_text_candidates,
                    self.material_units_by_chunk_id,
                    chunk_limit=DEFAULT_SECTION_RAW_DOSSIER_TRANSPORT_LIMIT,
                ),
                "visual_chunks": [
                    {
                        "id": x.get("chunk_id"),
                        "role": compact(x.get("visual_role"), 60),
                        "caption": compact(x.get("caption_preview"), 180),
                        "best_use": compact(x.get("best_use_in_review"), 120),
                    }
                    for x in visual_candidates if x.get("chunk_id")
                ],
            }
            parsed: dict[str, Any] = {}
            attempt_audit: list[dict[str, Any]] = []
            if grounder_prompt:
                grounder_timeout_seconds = float(
                    os.environ.get(
                        "QWEN_GROUNDER_HTTP_TIMEOUT_SEC",
                        BLUEPRINT_GROUNDER_HTTP_TIMEOUT_SEC,
                    )
                )
                for attempt in range(1, 3):
                    result = call_qwen_chat(
                        "ReviewBlueprintEvidenceGrounderAgent",
                        [
                            {"role": "system", "content": grounder_prompt},
                            {"role": "user", "content": json.dumps({"section": section, "candidate_menu": menu}, ensure_ascii=False)},
                        ],
                        model_tier=BLUEPRINT_GROUNDER_MODEL_TIER,
                        temperature=0,
                        max_tokens=3200,
                        response_format={"type": "json_object"},
                        force_mock=False,
                        max_retries=0,
                        allow_model_fallback=False,
                        max_key_candidates=1,
                        max_transport_key_candidates=1,
                        stream=True,
                        accept_partial_stream=False,
                        timeout_seconds=grounder_timeout_seconds,
                    )
                    raw = str(result.get("content") or "")
                    parsed = safe_json_parse(raw)
                    attempt_audit.append(
                        {
                            "attempt": attempt,
                            "parsed": bool(parsed),
                            "raw_chars": len(raw),
                            "usage": result.get("_llm_usage", {}),
                            "raw_preview": compact(raw, 300),
                        }
                    )
                    if parsed:
                        break

            valid_concepts = {str(x.get("id")) for x in menu["concept_nodes"] if x.get("id")}
            valid_text = {str(x.get("id")) for x in menu["text_chunks"] if x.get("id")}
            valid_visual = {str(x.get("id")) for x in menu["visual_chunks"] if x.get("id")}

            def valid_ids(
                value: Any, allowed: set[str], limit: int | None
            ) -> list[str]:
                parsed_items = clean_list(
                    value, None if limit is None else limit * 2
                )
                valid = [x for x in parsed_items if x in allowed]
                return valid if limit is None else valid[:limit]

            concept_ids = valid_ids(parsed.get("concept_node_ids"), valid_concepts, 5)
            text_ids = valid_ids(parsed.get("text_chunk_ids"), valid_text, self.served_text_limit)
            visual_ids = valid_ids(parsed.get("visual_chunk_ids"), valid_visual, 6)
            # Deterministic candidates are a safe availability fallback, not a
            # claim that the assets prove anything.  M2a verifies exact support.
            if not concept_ids:
                concept_ids = [str(x.get("node_id")) for x in concept_candidates[:3] if x.get("node_id")]
            if not text_ids:
                text_ids = [str(x.get("chunk_id")) for x in text_candidates[:5] if x.get("chunk_id")]
            # Candidate pools are retrieval menus, not proof.  Preserve enough
            # breadth for M2a to test alternative claims and prefer independent
            # papers instead of repeatedly feeding it adjacent chunks from one
            # source.  Exact support is still decided by the verifier.
            if len(text_ids) < min(7, len(text_candidates)):
                candidate_by_id = {
                    str(x.get("chunk_id")): x for x in text_candidates if x.get("chunk_id")
                }
                selected_papers = {
                    str(candidate_by_id[cid].get("paper_id") or "")
                    for cid in text_ids if cid in candidate_by_id
                }
                remaining = [x for x in text_candidates if str(x.get("chunk_id")) not in text_ids]
                remaining.sort(
                    key=lambda x: (
                        str(x.get("paper_id") or "") not in selected_papers,
                        bool((x.get("material_card_binding") or {}).get("propositions")),
                        x.get("use_permission") == "factual_support",
                        bool(x.get("text_preview")),
                    ),
                    reverse=True,
                )
                for item in remaining:
                    cid = str(item.get("chunk_id") or "")
                    if not cid:
                        continue
                    text_ids.append(cid)
                    selected_papers.add(str(item.get("paper_id") or ""))
                    if len(text_ids) >= min(7, len(text_candidates)):
                        break
            if not visual_ids and section.get("visual_argument_goals"):
                visual_ids = [str(x.get("chunk_id")) for x in visual_candidates[:3] if x.get("chunk_id")]

            claim_bindings: list[dict[str, Any]] = []
            raw_bindings = parsed.get("claim_bindings") if isinstance(parsed.get("claim_bindings"), list) else []
            for item in raw_bindings[:4]:
                if not isinstance(item, dict):
                    continue
                claim_seed = compact(item.get("claim_seed"), 420)
                if not claim_seed:
                    continue
                claim_bindings.append(
                    {
                        "claim_seed": claim_seed,
                        "supporting_text_chunk_ids": valid_ids(item.get("supporting_text_chunk_ids"), valid_text, 4),
                        "supporting_visual_chunk_ids": valid_ids(item.get("supporting_visual_chunk_ids"), valid_visual, 3),
                        "counterevidence_text_chunk_ids": valid_ids(
                            item.get("counterevidence_text_chunk_ids"), valid_text, 4
                        ),
                        "boundary_text_chunk_ids": valid_ids(
                            item.get("boundary_text_chunk_ids"), valid_text, 4
                        ),
                        "background_text_chunk_ids": valid_ids(
                            item.get("background_text_chunk_ids"), valid_text, 4
                        ),
                        "relation_to_section": compact(item.get("relation_to_section"), 40) or "support",
                        "relation_roles": clean_list(
                            item.get("relation_roles")
                            or [compact(item.get("relation_to_section"), 40) or "support"],
                            6,
                        ),
                        "counterevidence_query": compact(
                            item.get("counterevidence_query")
                            or f"{claim_seed} contradictory findings limitations alternative explanation",
                            260,
                        ),
                        "boundary_conditions": clean_list(
                            item.get("boundary_conditions")
                            or ["Check operating regime, assumptions, and measurement conditions before generalizing."],
                            4,
                        ),
                        "axis_assignments": [
                            dict(axis) for axis in (item.get("axis_assignments") or [])
                            if isinstance(axis, dict) and axis.get("axis_id")
                        ][:6],
                    }
                )
            if not claim_bindings:
                for seed in self._architecture_claim_seeds(section):
                    claim_bindings.append(
                        {
                            **seed,
                            "supporting_text_chunk_ids": [],
                            "supporting_visual_chunk_ids": [],
                        }
                    )

            visual_slots: list[dict[str, Any]] = []
            raw_slots = parsed.get("visual_argument_slots") if isinstance(parsed.get("visual_argument_slots"), list) else []
            for item in raw_slots[:2]:
                if not isinstance(item, dict):
                    continue
                visual_slots.append(
                    {
                        "goal": compact(item.get("goal"), 180),
                        "purpose": compact(item.get("purpose"), 360),
                        "visual_chunk_ids": valid_ids(item.get("visual_chunk_ids"), valid_visual, 3),
                    }
                )
            if not visual_slots:
                goals = section.get("visual_argument_goals") if isinstance(section.get("visual_argument_goals"), list) else []
                for goal_index, item in enumerate(goals[:2]):
                    goal = item if isinstance(item, dict) else {"goal": item}
                    visual_slots.append(
                        {
                            "goal": compact(goal.get("goal"), 180),
                            "purpose": compact(goal.get("purpose"), 360),
                            "visual_chunk_ids": visual_ids[goal_index : goal_index + 2],
                        }
                    )

            # A section-level pool must include every identifier used by its
            # planning bindings. Otherwise the next stage correctly rejects a
            # binding as non-local even though it came from the same menu.
            # The model menu remains small, but the durable served pool is
            # widened with ranked alternatives from independent papers.
            bound_text_ids = [
                cid for binding in claim_bindings for cid in binding.get("supporting_text_chunk_ids", [])
            ]
            bound_visual_ids = [
                cid for binding in claim_bindings for cid in binding.get("supporting_visual_chunk_ids", [])
            ] + [cid for slot in visual_slots for cid in slot.get("visual_chunk_ids", [])]
            text_ids = list(dict.fromkeys(text_ids + bound_text_ids))
            selected_text_ids = set(text_ids)
            selected_papers = {
                str(item.get("paper_id") or "")
                for item in text_candidates
                if str(item.get("chunk_id") or "") in selected_text_ids
            }
            inventory_ranked = list(text_inventory_candidates)
            inventory_ranked.sort(
                key=lambda item: (
                    str(item.get("paper_id") or "") not in selected_papers,
                    bool((item.get("material_card_binding") or {}).get("propositions")),
                    item.get("use_permission") == "factual_support",
                    float(item.get("match_score") or 0.0),
                ),
                reverse=True,
            )
            for item in inventory_ranked:
                cid = str(item.get("chunk_id") or "")
                if not cid or cid in selected_text_ids:
                    continue
                text_ids.append(cid)
                selected_text_ids.add(cid)
                selected_papers.add(str(item.get("paper_id") or ""))
                if (
                    self.served_text_limit is not None
                    and len(text_ids) >= self.served_text_limit
                ):
                    break
            text_ids = (
                text_ids
                if self.served_text_limit is None
                else text_ids[: self.served_text_limit]
            )
            visual_ids = list(dict.fromkeys(visual_ids + bound_visual_ids))[:14]

            uncovered = clean_list(parsed.get("uncovered_needs"), 5)
            search_seeds = clean_list(section.get("candidate_search_seeds"), 3)
            if not search_seeds and uncovered:
                search_seeds = [compact(f"{section.get('title', '')} {uncovered[0]}", 300)]
            section.update(
                {
                    "concept_node_ids": concept_ids,
                    "text_chunk_ids": text_ids,
                    "visual_chunk_ids": visual_ids,
                    "candidate_material_pool": self._candidate_material_pool(
                        candidate_chunk_ids=[
                            str(x.get("chunk_id")) for x in text_inventory_candidates
                            if x.get("chunk_id")
                        ],
                        candidate_paper_ids=list(dict.fromkeys(
                            str(x.get("paper_id") or "")
                            for x in text_inventory_candidates
                            if x.get("paper_id")
                        )),
                        served_chunk_ids=list(text_ids),
                        model_context_chunk_ids=list(
                            text_ids[: self.model_text_context_limit]
                        ),
                        compression_policy=(
                            "Semantically admitted candidates are visible to "
                            "the section grounder and retained in the "
                            "reopenable pool; the digest batches summarize "
                            "every admitted candidate and only a bounded "
                            "raw-dossier subset is reopened per LLM call."
                        ),
                    ),
                    "_text_candidate_admission_audit": admission_audit,
                    "claim_graph_seed": claim_bindings,
                    "visual_argument_slots": visual_slots,
                    "candidate_search_seeds": search_seeds,
                    "evidence_risks": list(
                        dict.fromkeys(
                            clean_list(section.get("evidence_risks"), 5)
                            + uncovered
                            + ["Candidate anchors require exact source-span verification before scientific writing."]
                        )
                    )[:6],
                    "_grounding_status": "llm_grounded" if parsed else "deterministic_candidate_fallback",
                }
            )
            audit = {
                "section_id": section_id,
                "status": section["_grounding_status"],
                "candidate_counts": {
                    "concepts": len(concept_candidates),
                    "text": len(text_candidates),
                    "visual": len(visual_candidates),
                },
                "selected_counts": {
                    "concepts": len(concept_ids),
                    "text": len(text_ids),
                    "visual": len(visual_ids),
                },
                "text_admission_audit": admission_audit,
                "uncovered_needs": uncovered,
                "attempts": attempt_audit,
            }
            return index, section, audit

        workers = min(4, max(1, len(raw_sections)))
        grounded_results: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        progress_path = (
            self.output_dir / "review_blueprint_grounding_progress.json"
        )
        progress_created_at = utc_now()

        def write_grounding_progress() -> None:
            """Coordinator-thread-only progress write (never from workers)."""
            sorted_results = sorted(
                grounded_results, key=lambda row: row[0]
            )
            write_json(progress_path, {
                "created_at": progress_created_at,
                "updated_at": utc_now(),
                "total_sections": len(raw_sections),
                "completed_count": len(sorted_results),
                "remaining_count": len(raw_sections) - len(sorted_results),
                "completed_section_ids": [
                    str(row[1].get("section_id") or "")
                    for row in sorted_results
                ],
                "sections": [row[2] for row in sorted_results],
            })

        write_grounding_progress()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(ground_one, item)
                for item in enumerate(raw_sections)
            ]
            for future in as_completed(futures):
                grounded_results.append(future.result())
                write_grounding_progress()
        grounded_results.sort(key=lambda row: row[0])
        grounded_architecture = dict(architecture)
        grounded_architecture["sections"] = [row[1] for row in grounded_results]
        grounded_architecture["_grounding_summary"] = {
            "mode": "parallel_section_grounding",
            "model_tier": BLUEPRINT_GROUNDER_MODEL_TIER,
            "sections": len(grounded_results),
            "llm_grounded": sum(row[2]["status"] == "llm_grounded" for row in grounded_results),
            "deterministic_fallback": sum(row[2]["status"] != "llm_grounded" for row in grounded_results),
        }
        forced_reground = (
            architecture.get("_forced_reground")
            if isinstance(architecture, dict)
            else None
        )
        if isinstance(forced_reground, dict):
            grounded_architecture["_grounding_summary"]["forced_reground"] = True
            grounded_architecture["_grounding_summary"]["source_checkpoint"] = (
                str(forced_reground.get("source_checkpoint") or "")
            )
        # A long architecture response can be recoverable even when its final
        # gap list is truncated. In that case the section grounders have the
        # best direct view of what the current corpus does not cover, so promote
        # only the first high-value need from each section (maximum four).
        if not self._planner_gap_seeds(grounded_architecture):
            generated_gaps: list[dict[str, str]] = []
            for _, section, audit in grounded_results:
                needs = audit.get("uncovered_needs") if isinstance(audit.get("uncovered_needs"), list) else []
                if not needs:
                    continue
                queries = clean_list(section.get("candidate_search_seeds"), 2)
                generated_gaps.append(
                    {
                        "gap": compact(needs[0], 360),
                        "query": queries[0] if queries else compact(f"{section.get('title', '')} {needs[0]}", 300),
                        "why_high_marginal_value": "This evidence could confirm, narrow, or remove a load-bearing part of the planned section.",
                        "stop_condition": "Stop when one directly relevant primary study plus one independent corroborating source are bound to the needed claim, or when the claim is narrowed to the supported scope.",
                    }
                )
                if len(generated_gaps) >= 4:
                    break
            grounded_architecture["high_value_gap_seeds"] = generated_gaps
        write_json(
            self.output_dir / "review_blueprint_grounding_audit.json",
            {"created_at": utc_now(), "sections": [row[2] for row in grounded_results]},
        )
        # Durable paid-stage checkpoint: after all section grounding succeeds,
        # persist the complete grounded architecture so later candidate-claim /
        # decomposition failures can resume without paying for the grounder
        # again.  Only a checkpoint that satisfies the existing completeness
        # contract is written; it is reusable via --planner-architecture-path.
        checkpoint_path = (
            self.output_dir / "review_blueprint.grounded_checkpoint.json"
        )
        if self._is_complete_grounded_architecture(grounded_architecture):
            grounded_architecture["_grounding_summary"][
                "grounded_checkpoint_path"
            ] = str(checkpoint_path)
            grounded_architecture["_grounding_summary"][
                "grounded_checkpoint_written"
            ] = True
            write_json(checkpoint_path, grounded_architecture)
        else:
            grounded_architecture["_grounding_summary"][
                "grounded_checkpoint_written"
            ] = False
            grounded_architecture["_grounding_summary"][
                "grounded_checkpoint_reason"
            ] = "incomplete_grounding_contract"
        return grounded_architecture

    def _compact_evidence_for_llm(self, evidence: dict[str, Any]) -> dict[str, Any]:
        def short_node(x: dict[str, Any]) -> dict[str, Any]:
            return {
                "node_id": x.get("node_id"),
                "view_id": x.get("view_id"),
                "label": compact(x.get("label"), 120),
                "planning_value": compact(x.get("planning_value"), 100),
                "score": x.get("score"),
            }

        def short_text(x: dict[str, Any]) -> dict[str, Any]:
            binding = x.get("material_card_binding") if isinstance(x.get("material_card_binding"), dict) else {}
            return {
                "chunk_id": x.get("chunk_id"),
                "paper_id": x.get("paper_id"),
                "title": compact(x.get("title"), 90),
                "section_path": compact(x.get("section_path"), 60),
                "text_preview": compact(x.get("text_preview"), 100),
                "retrieval_source": x.get("retrieval_source"),
                "use_permission": x.get("use_permission"),
                "content_depth": x.get("content_depth"),
                "proposition_previews": [
                    {
                        "proposition_id": p.get("proposition_id"),
                        "statement": compact(p.get("statement"), 140),
                        "question_function": p.get("question_function"),
                        "evidence_ceiling": p.get("evidence_ceiling"),
                    }
                    for p in (binding.get("propositions") or [])[:3]
                    if isinstance(p, dict)
                ],
            }

        def short_visual(x: dict[str, Any]) -> dict[str, Any]:
            return {
                "chunk_id": x.get("chunk_id"),
                "paper_id": x.get("paper_id"),
                "title": compact(x.get("title"), 90),
                "visual_role": x.get("visual_role"),
                "review_utility": x.get("review_utility"),
                "caption_preview": compact(x.get("caption_preview"), 100),
                "best_use_in_review": compact(x.get("best_use_in_review"), 80),
            }

        def short_cluster(x: dict[str, Any]) -> dict[str, Any]:
            return {
                "cluster_id": x.get("cluster_id"),
                "view_id": x.get("view_id"),
                "view_name": x.get("view_name"),
                "central_labels": x.get("central_labels", [])[:4],
                "node_ids": x.get("node_ids", [])[:5],
                "text_chunk_ids": x.get("text_chunk_ids", [])[:5],
                "visual_chunk_ids": x.get("visual_chunk_ids", [])[:5],
                "evidence_counts": x.get("evidence_counts", {}),
            }

        raw_digest = evidence.get("evidence_digest")
        if not isinstance(raw_digest, dict):
            raw_digest = build_evidence_digest(
                [x for x in evidence.get("retrieved_text_chunks", []) if isinstance(x, dict)],
                batch_size=self.evidence_batch_size,
            )
        digest = {
            "schema_version": raw_digest.get("schema_version"),
            "strategy": raw_digest.get("strategy"),
            "chunk_count": raw_digest.get("chunk_count", 0),
            "retained_chunk_count": raw_digest.get("retained_chunk_count", 0),
            "batch_count": raw_digest.get("batch_count", 0),
            "batches": [
                {
                    "batch_id": item.get("batch_id"),
                    "chunk_ids": list(item.get("chunk_ids") or []),
                    "paper_ids": list(item.get("paper_ids") or []),
                    "summary": compact(item.get("summary"), 900),
                }
                for item in (raw_digest.get("batches") or [])
                if isinstance(item, dict)
            ],
            "chunk_index": [
                {
                    "ref": item.get("ref"),
                    "chunk_id": item.get("chunk_id"),
                    "paper_id": item.get("paper_id"),
                    "title": compact(item.get("title"), 100),
                    "summary": compact(item.get("summary"), 180),
                    "use_permission": item.get("use_permission"),
                    "content_depth": item.get("content_depth"),
                }
                for item in (raw_digest.get("chunk_index") or [])
                if isinstance(item, dict)
            ],
            "raw_text_policy": raw_digest.get("raw_text_policy"),
            "advisory_target_range": raw_digest.get(
                "advisory_target_range",
                list(PREFERRED_SECTION_TEXT_CANDIDATE_RANGE),
            ),
            "candidate_pool_status": raw_digest.get(
                "candidate_pool_status",
                advisory_candidate_pool_status(
                    int(raw_digest.get("chunk_count") or 0)
                ),
            ),
        }

        return {
            "topic_terms": evidence.get("topic_terms", [])[:14],
            "cluster_candidates": [short_cluster(x) for x in evidence.get("cluster_candidates", [])[:6] if isinstance(x, dict)],
            "selected_concept_nodes": [short_node(x) for x in evidence.get("selected_concept_nodes", [])[:8] if isinstance(x, dict)],
            # The architecture call should see the full retrieved text
            # inventory, not just the first six anchors.  Previews are short
            # and the final section grounder still performs the detailed
            # ranking, so this improves recall without exposing full papers.
            "retrieved_text_chunks": [
                short_text(x)
                for x in evidence.get("retrieved_text_chunks", [])[:12]
                if isinstance(x, dict)
            ],
            "evidence_digest": digest,
            "retrieved_visual_chunks": [short_visual(x) for x in evidence.get("retrieved_visual_chunks", [])[:6] if isinstance(x, dict)],
            "coverage": evidence.get("coverage", {}),
            "review_mentor_advice": evidence.get("review_mentor_advice", {}),
        }

    def _build_anchor_menu(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Full anchor menus for Show-then-Pick: LLM must pick IDs from these lists."""
        return {
            "concept_nodes": [
                {"id": n.get("node_id"), "label": compact(n.get("label"), 100)}
                for n in evidence.get("selected_concept_nodes", [])[:30]
                if isinstance(n, dict) and n.get("node_id")
            ],
            "text_chunks": [
                {
                    "id": c.get("chunk_id"),
                    "preview": compact(c.get("text_preview", ""), 80),
                    "retrieval_source": c.get("retrieval_source"),
                    "use_permission": c.get("use_permission"),
                    "content_depth": c.get("content_depth"),
                    "proposition_ids": [
                        p.get("proposition_id")
                        for p in ((c.get("material_card_binding") or {}).get("propositions") or [])[:3]
                        if isinstance(p, dict) and p.get("proposition_id")
                    ],
                }
                for c in evidence.get("retrieved_text_chunks", [])
                if isinstance(c, dict) and c.get("chunk_id")
            ],
            "visual_chunks": [
                {"id": c.get("chunk_id"), "role": c.get("visual_role", ""), "caption": compact(c.get("caption_preview", ""), 60)}
                for c in evidence.get("retrieved_visual_chunks", [])
                if isinstance(c, dict) and c.get("chunk_id")
            ],
        }

    def _sections_from_llm_plan(
        self,
        parsed: dict[str, Any],
        evidence: dict[str, Any],
        *,
        allow_deterministic_completion: bool = True,
    ) -> list[dict[str, Any]]:
        raw_sections = parsed.get("sections") if isinstance(parsed.get("sections"), list) else []
        if not raw_sections:
            return []
        range_is_authoritative = (
            self.min_sections,
            self.max_sections,
        ) == (8, 10)
        if (
            not allow_deterministic_completion
            and range_is_authoritative
            and not (
                self.min_sections <= len(raw_sections) <= self.max_sections
            )
        ):
            raise ValueError(
                f"Qwen architecture must contain {self.min_sections}-"
                f"{self.max_sections} chapters; received "
                f"{len(raw_sections)}; refusing deterministic completion."
            )
        concept_by_id = {n.node_id: self._node_brief(n) for n in self.concept_nodes if n.node_id}
        # Pre-build label token sets once to avoid repeated regex in fuzzy fallback.
        node_label_tokens: dict[str, set[str]] = {
            n.node_id: set(tokenize(n.label)) for n in self.concept_nodes if n.node_id
        }
        text_by_id = {x["chunk_id"]: x for x in evidence.get("retrieved_text_chunks", []) if isinstance(x, dict) and x.get("chunk_id")}
        visual_by_id = {x["chunk_id"]: x for x in evidence.get("retrieved_visual_chunks", []) if isinstance(x, dict) and x.get("chunk_id")}
        all_text_ids = []
        all_visual_ids = []
        for raw in raw_sections:
            if isinstance(raw, dict):
                all_text_ids.extend(clean_list(raw.get("text_chunk_ids"), 2000))
                all_text_ids.extend(clean_list(raw.get("candidate_text_pool_ids"), 2000))
                raw_pool_field = (raw.get("candidate_material_pool") or {})
                if isinstance(raw_pool_field, dict):
                    all_text_ids.extend(
                        clean_list(raw_pool_field.get("candidate_chunk_ids"), 2000)
                    )
                all_visual_ids.extend(clean_list(raw.get("visual_chunk_ids"), 30))
                for slot in raw.get("visual_argument_slots", []) if isinstance(raw.get("visual_argument_slots"), list) else []:
                    if isinstance(slot, dict):
                        all_visual_ids.extend(clean_list(slot.get("visual_chunk_ids"), 30))
        for fetched in self._fetch_text_chunks_by_ids(all_text_ids):
            chunk_id = str(fetched.get("chunk_id") or "")
            if not chunk_id:
                continue
            if chunk_id in text_by_id:
                merged = dict(fetched)
                merged.update(text_by_id[chunk_id])
                text_by_id[chunk_id] = merged
            else:
                text_by_id[chunk_id] = fetched
        visual_by_id.update({x["chunk_id"]: x for x in self._fetch_visual_chunks_by_ids(all_visual_ids) if x.get("chunk_id")})
        checkpoint_rematerialization_audit: dict[str, Any] = {
            "enabled": False,
            "requested_ids": [],
            "reconstructed_ids": [],
            "missing_ids": [],
            "source": "material_units_by_chunk_id",
        }
        if (
            self.material_units_by_chunk_id
            and (
                isinstance(parsed.get("_grounding_summary"), dict)
                or bool(parsed.get("_reused_grounded_architecture"))
            )
        ):
            # A grounded checkpoint is the durable source of truth for its
            # selected material IDs.  With kb_dir=None the SQLite/planning
            # evidence path cannot see those IDs, so reconstruct every
            # checkpoint candidate that exists in the long-term material card
            # cache with full provenance/permission/preview/propositions.
            checkpoint_rematerialization_audit["enabled"] = True
            for chunk_id in dict.fromkeys(all_text_ids):
                if chunk_id in text_by_id:
                    continue
                unit = self.material_units_by_chunk_id.get(chunk_id)
                if not isinstance(unit, dict):
                    checkpoint_rematerialization_audit["missing_ids"].append(
                        chunk_id
                    )
                    continue
                identity = (
                    unit.get("identity")
                    if isinstance(unit.get("identity"), dict)
                    else {}
                )
                durable = (
                    unit.get("durable_content")
                    if isinstance(unit.get("durable_content"), dict)
                    else {}
                )
                audit = (
                    unit.get("audit")
                    if isinstance(unit.get("audit"), dict)
                    else {}
                )
                base_row = {
                    "chunk_id": chunk_id,
                    "paper_id": compact(identity.get("paper_id"), 120),
                    "title": compact(identity.get("title"), 180),
                    "section_path": compact(durable.get("section_path"), 120),
                    "text_preview": compact(
                        durable.get("raw_text")
                        or durable.get("normalized_text"),
                        1800,
                    ),
                    "provenance": (
                        audit.get("source_provenance")
                        if isinstance(audit.get("source_provenance"), dict)
                        else {}
                    ),
                }
                text_by_id[chunk_id] = self._attach_material_binding(
                    base_row, unit
                )
                checkpoint_rematerialization_audit[
                    "reconstructed_ids"
                ].append(chunk_id)
            checkpoint_rematerialization_audit["requested_ids"] = list(
                dict.fromkeys(all_text_ids)
            )
            checkpoint_rematerialization_audit["missing_ids"] = list(
                dict.fromkeys(
                    checkpoint_rematerialization_audit["missing_ids"]
                )
            )
        sections: list[dict[str, Any]] = []
        for idx, raw in enumerate(raw_sections, start=1):
            if not isinstance(raw, dict):
                continue
            section_id = compact(raw.get("section_id"), 20) or f"S{idx:02d}"
            if not re.match(r"^S\d\d$", section_id):
                section_id = f"S{idx:02d}"
            concept_ids = clean_list(raw.get("concept_node_ids"), 10)
            text_ids = clean_list(raw.get("text_chunk_ids"), self.served_text_limit)
            visual_ids = clean_list(raw.get("visual_chunk_ids"), 14)
            raw_pool_field = raw.get("candidate_material_pool") or {}
            if not isinstance(raw_pool_field, dict):
                raw_pool_field = {}
            raw_pool_ids = list(
                dict.fromkeys(
                    clean_list(
                        raw.get("candidate_text_pool_ids")
                        or raw_pool_field.get("candidate_chunk_ids")
                        or raw.get("text_chunk_ids"),
                        2000,
                    )
                )
            )
            section_rematerialization_audit = {
                "enabled": checkpoint_rematerialization_audit["enabled"],
                "requested_ids": list(dict.fromkeys(raw_pool_ids)),
                "reconstructed_ids": [
                    chunk_id
                    for chunk_id in dict.fromkeys(raw_pool_ids)
                    if chunk_id
                    in checkpoint_rematerialization_audit[
                        "reconstructed_ids"
                    ]
                ],
                "missing_ids": [
                    chunk_id
                    for chunk_id in dict.fromkeys(raw_pool_ids)
                    if chunk_id not in text_by_id
                ],
                "source": "material_units_by_chunk_id",
            }
            concept_nodes = [concept_by_id[x] for x in concept_ids if x in concept_by_id]
            # The planner's selected IDs are a compact starting point.  Reopen
            # the section's durable candidate pool up to the configured served
            # limit so later claim/counterevidence work is not trapped in the
            # first few model choices.
            expanded_text_ids = list(text_ids)
            for candidate_id in raw_pool_ids:
                if candidate_id in text_by_id and candidate_id not in expanded_text_ids:
                    expanded_text_ids.append(candidate_id)
                if (
                    self.served_text_limit is not None
                    and len(expanded_text_ids) >= self.served_text_limit
                ):
                    break
            text_chunks = [text_by_id[x] for x in expanded_text_ids if x in text_by_id]
            retained_text_chunks = [
                text_by_id[x]
                for x in dict.fromkeys(raw_pool_ids)
                if x in text_by_id
            ]
            visual_chunks = [visual_by_id[x] for x in visual_ids if x in visual_by_id]
            evidence_digest = build_evidence_digest(
                retained_text_chunks, batch_size=self.evidence_batch_size
            )
            labels = [x.get("label", "") for x in concept_nodes if x.get("label")]
            if not concept_nodes:
                # Fuzzy fallback: LLM may have used IDs not in the map (invented or mistyped).
                # Recover by matching concept node labels against section title/role keywords.
                title_text = (raw.get("title") or "") + " " + (raw.get("argument_role") or "")
                title_tokens = set(tokenize(title_text))
                if title_tokens:
                    fuzzy = [
                        self._node_brief(n) for n in self.concept_nodes
                        if title_tokens & node_label_tokens.get(n.node_id, set())
                    ][:5]
                    concept_nodes = fuzzy
                    labels = [x.get("label", "") for x in concept_nodes if x.get("label")]
                # A Qwen section with no local concept anchor is preserved with
                # empty current anchors so it exposes an evidence gap; it is
                # never dropped because local grounding found no node.
            section = {
                "section_id": section_id,
                "title": compact(raw.get("title"), 180)
                or (
                    self._dynamic_title("Evidence cluster", labels)
                    if allow_deterministic_completion
                    else ""
                ),
                "argument_role": compact(raw.get("argument_role"), 520)
                or (
                    self._dynamic_argument_role("evidence cluster", labels)
                    if allow_deterministic_completion
                    else ""
                ),
                "key_questions": clean_list(raw.get("key_questions"), 5)
                or (
                    self._dynamic_questions("evidence cluster", labels)
                    if allow_deterministic_completion
                    else []
                ),
                "unique_contribution": compact(
                    raw.get("unique_contribution")
                    or raw.get("novel_contribution_to_review"),
                    600,
                )
                or (
                    self._novel_contribution("LLM-planned", labels, {})
                    if allow_deterministic_completion
                    else ""
                ),
                "must_cover": clean_list(raw.get("must_cover"), 12),
                "must_not_cover": clean_list(raw.get("must_not_cover"), 12),
                "assigned_user_axes": clean_list(
                    raw.get("assigned_user_axes"), 8
                ),
                "handoff_from_previous": compact(
                    raw.get("handoff_from_previous"), 420
                ),
                "handoff_to_next": compact(
                    raw.get("handoff_to_next")
                    or raw.get("transition_to_next"),
                    420,
                ),
                # T4: preserve LLM-generated section schema fields; fall back to deterministic helpers
                "required_claim_kinds": clean_list(raw.get("required_claim_kinds"), 4) or [],
                "transition_from_previous": compact(raw.get("transition_from_previous"), 420),
                "transition_to_next": compact(raw.get("transition_to_next"), 420),
                "expected_visual_arguments": clean_list(raw.get("expected_visual_arguments"), 4) or self._expected_visual_arguments(labels, visual_chunks),
                "novel_contribution_to_review": compact(
                    raw.get("novel_contribution_to_review"), 600
                )
                or (
                    self._novel_contribution("LLM-planned", labels, {})
                    if allow_deterministic_completion
                    else ""
                ),
                "scope_guardrails": clean_list(raw.get("scope_guardrails"), 4) or self._scope_guardrails({}, text_chunks),
                "concept_map_nodes": concept_nodes,
                "candidate_text_chunks": text_chunks,
                "candidate_text_context": evidence_digest.get("chunk_index", [])[: self.model_text_context_limit],
                "candidate_text_chunk_ids": [x.get("chunk_id") for x in text_chunks],
                "candidate_evidence_digest": evidence_digest,
                "checkpoint_rematerialization_audit": (
                    section_rematerialization_audit
                ),
                "candidate_text_model_policy": {
                    "raw_text_in_default_model_prompt": False,
                    "digest_field": "candidate_evidence_digest",
                    "reopen_by": "chunk_id",
                    "batch_size": self.evidence_batch_size,
                },
                "candidate_material_pool": self._candidate_material_pool(
                    candidate_chunk_ids=[
                        chunk_id
                        for chunk_id in raw_pool_ids
                        if chunk_id in text_by_id
                    ],
                    candidate_paper_ids=list(dict.fromkeys(
                        str(text_by_id[chunk_id].get("paper_id") or "")
                        for chunk_id in raw_pool_ids
                        if chunk_id in text_by_id
                        and text_by_id[chunk_id].get("paper_id")
                    )),
                    served_chunk_ids=[
                        x.get("chunk_id") for x in text_chunks
                    ],
                    model_context_chunk_ids=[
                        x.get("chunk_id")
                        for x in text_chunks[: self.model_text_context_limit]
                    ],
                    compression_policy=(
                        "Every retained candidate is summarized in "
                        "candidate_evidence_digest batches and stays reopenable "
                        "by chunk_id; downstream planning reads batch summaries "
                        "plus a bounded raw-dossier layer. No hard 200th-item "
                        "cut."
                    ),
                ),
                "candidate_visual_chunks": visual_chunks,
                "visual_argument_slots": self._llm_visual_slots(section_id, raw, visual_chunks, labels),
                "axis_assignments": [
                    dict(item) for item in (raw.get("axis_assignments") or [])
                    if isinstance(item, dict) and item.get("axis_id")
                ],
                "argument_structure": dict(raw.get("argument_structure") or {
                    "composition_mode": "multi_axis_claim_centered",
                    "required_relation_roles": [
                        "support", "counterevidence", "boundary_condition",
                        "background_context", "open_gap",
                    ],
                }),
                "claim_graph_seed": {
                    "central_claim_candidates": raw.get("claim_graph_seed", []) if isinstance(raw.get("claim_graph_seed"), list) else [],
                    "relation_types_to_check": ["support", "contrast", "refine", "boundary_condition", "open_gap"],
                    "relation_contract": dict(
                        (raw.get("claim_graph_seed") or {}).get("relation_contract")
                        or {
                            "required_roles": [
                                "support", "counterevidence", "boundary_condition",
                                "background_context", "open_gap",
                            ],
                        }
                    ) if isinstance(raw.get("claim_graph_seed"), dict) else {
                        "required_roles": [
                            "support", "counterevidence", "boundary_condition",
                            "background_context", "open_gap",
                        ]
                    },
                    "claim_binding_rule": "A later evidence binder must attach each claim to exact text or caption spans before final writing.",
                },
                "review_example_archetype_anchor": self._archetype_anchor_for_section(compact(raw.get("title"), 180), "LLM-planned section"),
                "candidate_search_seeds": clean_list(raw.get("candidate_search_seeds"), 5) or self._section_search_seeds(labels, compact(raw.get("title"), 180)),
                "writing_requirements": clean_list(raw.get("writing_requirements"), 6)
                or [
                    "Explain the section's argument before listing papers.",
                    "Bind claims to exact source spans during writing.",
                ],
                "evidence_risks": clean_list(raw.get("evidence_risks"), 5)
                or ["LLM-generated structure still needs source-span binding before final writing."],
                "generated_from": {
                    "planner": "llm_integrated_dynamic",
                    "concept_node_ids_requested": concept_ids,
                    "text_chunk_ids_requested": text_ids,
                    "visual_chunk_ids_requested": visual_ids,
                    "dynamic_not_template": True,
                },
            }
            sections.append(section)
        if (
            0 < len(sections) < self.min_sections
            and allow_deterministic_completion
        ):
            sections = self._complete_partial_llm_sections(sections, evidence)
        return sections

    def _complete_partial_llm_sections(self, sections: list[dict[str, Any]], evidence: dict[str, Any]) -> list[dict[str, Any]]:
        deterministic = self._build_dynamic_sections(evidence)
        used_node_ids = {
            node.get("node_id")
            for section in sections
            for node in section.get("concept_map_nodes", [])
            if isinstance(node, dict) and node.get("node_id")
        }
        used_titles = {str(section.get("title") or "").lower() for section in sections}
        out = list(sections)
        for section in deterministic:
            title = str(section.get("title") or "").lower()
            node_ids = {
                node.get("node_id")
                for node in section.get("concept_map_nodes", [])
                if isinstance(node, dict) and node.get("node_id")
            }
            if title in used_titles:
                continue
            if node_ids and (node_ids & used_node_ids):
                continue
            section = dict(section)
            section["section_id"] = f"S{len(out)+1:02d}"
            generated_from = dict(section.get("generated_from") or {})
            generated_from["planner"] = "deterministic_completion_after_partial_llm"
            section["generated_from"] = generated_from
            out.append(section)
            used_titles.add(title)
            used_node_ids.update(node_ids)
            if len(out) >= self.min_sections:
                break
        # Renumber once after completion.
        for idx, section in enumerate(out, start=1):
            section["section_id"] = f"S{idx:02d}"
            for slot_idx, slot in enumerate(section.get("visual_argument_slots", []), start=1):
                slot["slot_id"] = f"S{idx:02d}-V{slot_idx:02d}"
        return out

    def _fetch_text_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        ids = [x for x in dict.fromkeys(chunk_ids) if x]
        if not ids or not self.db_path:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = read_sqlite_rows(
            self.db_path,
            f"SELECT chunk_id,paper_id,title,section_path,text FROM text_chunks WHERE chunk_id IN ({placeholders})",
            ids,
        )
        return [
            self._attach_material_binding({
                "chunk_id": row["chunk_id"],
                "paper_id": row["paper_id"],
                "title": compact(row["title"], 180),
                "section_path": compact(row["section_path"], 120),
                "text_preview": compact(row["text"], 520),
                "retrieval_query": "llm_selected_id",
                "retrieval_source": "sqlite_id_lookup",
            })
            for row in rows
        ]

    def _fetch_visual_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        ids = [x for x in dict.fromkeys(chunk_ids) if x]
        if not ids or not self.db_path:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = read_sqlite_rows(
            self.db_path,
            f"""
            SELECT chunk_id,paper_id,title,chunk_kind,parent_label,subfigure_label,visual_role,
                   review_utility,local_image_path,caption,raw_json
            FROM visual_chunks
            WHERE chunk_id IN ({placeholders})
            """,
            ids,
        )
        out = []
        for row in rows:
            raw_json = safe_json_parse(row["raw_json"])
            out.append(
                {
                    "chunk_id": row["chunk_id"],
                    "paper_id": row["paper_id"],
                    "title": compact(row["title"], 180),
                    "chunk_kind": row["chunk_kind"],
                    "parent_label": row["parent_label"],
                    "subfigure_label": row["subfigure_label"],
                    "visual_role": row["visual_role"],
                    "review_utility": row["review_utility"],
                    "local_image_path": row["local_image_path"],
                    "caption_preview": compact(row["caption"], 360),
                    "best_use_in_review": compact(raw_json.get("best_use_in_review"), 260),
                    "direct_use_candidate": raw_json.get("direct_use_candidate", ""),
                    "redraw_recommendation": raw_json.get("redraw_recommendation", ""),
                    "quality_flags": raw_json.get("quality_flags", []),
                    "retrieval_query": "llm_selected_id",
                }
            )
        return out

    def _llm_visual_slots(
        self,
        section_id: str,
        raw_section: dict[str, Any],
        visual_chunks: list[dict[str, Any]],
        labels: list[str],
    ) -> list[dict[str, Any]]:
        raw_slots = raw_section.get("visual_argument_slots") if isinstance(raw_section.get("visual_argument_slots"), list) else []
        if not raw_slots:
            return self._visual_slots_for_section(section_id, labels, visual_chunks)
        visual_by_id = {x.get("chunk_id"): x for x in visual_chunks}
        slots = []
        for idx, raw in enumerate(raw_slots[:3], start=1):
            if not isinstance(raw, dict):
                continue
            ids = clean_list(raw.get("visual_chunk_ids"), 5)
            candidates = [visual_by_id[x] for x in ids if x in visual_by_id]
            if not candidates:
                candidates = self._rank_visuals_for_goal(str(raw.get("goal") or ""), visual_chunks, set(), limit=4)
            slots.append(
                {
                    "slot_id": f"{section_id}-V{idx:02d}",
                    "goal": compact(raw.get("goal"), 180) or "Use visual evidence to make this section's argument inspectable.",
                    "purpose": compact(raw.get("purpose"), 360) or self._visual_slot_purpose(str(raw.get("goal") or ""), candidates),
                    "preferred_visual_roles": sorted({str(x.get("visual_role") or "unknown") for x in candidates}),
                    "candidate_visual_chunks": candidates,
                    "usage_rule": "Use only after checking the caption, nearby text, image crop quality, and original source path.",
                }
            )
        return slots

    # Physics-first tier keywords (lower tier = earlier in review)
    _PHYSICS_FIRST_TIERS: dict[int, frozenset[str]] = {
        0: frozenset({"mechanism", "mechanisms", "physical", "principle", "theory", "optical",
                      "spectral", "interference", "scattering", "radiative", "thermodynamic",
                      "photonic", "electromagnetic", "quantum", "optic"}),
        1: frozenset({"material", "materials", "structure", "structural", "design",
                      "fabrication", "film", "coating", "polymer", "nanoparticle",
                      "cellulose", "composite", "matrix", "particle", "scaffold"}),
        2: frozenset({"application", "applications", "performance", "cooling", "thermal",
                      "energy", "wearable", "building", "agriculture", "outdoor", "device"}),
        3: frozenset({"bottleneck", "bottlenecks", "tradeoff", "trade-off", "challenge",
                      "limitation", "durability", "degradation", "scalability",
                      "manufacturability", "constraint"}),
        4: frozenset({"characterization", "method", "methods", "measurement", "modeling",
                      "simulation", "computational", "spectroscopy", "microscopy", "metric"}),
        5: frozenset({"future", "direction", "outlook", "perspective", "frontier",
                      "roadmap", "timeline", "development", "emerging"}),
    }

    def _detect_section_tier(self, section: dict[str, Any]) -> int:
        text = " ".join([
            str(section.get("title") or ""),
            str(section.get("argument_role") or "")[:120],
        ]).lower()
        tokens = set(re.findall(r"[a-z]{3,}", text))
        best_tier, best_hits = 99, 0
        for tier, keywords in self._PHYSICS_FIRST_TIERS.items():
            hits = len(tokens & keywords)
            if hits > best_hits or (hits == best_hits and tier < best_tier):
                best_tier, best_hits = tier, hits
        return best_tier

    def _enforce_physics_first_order(
        self,
        sections: list[dict[str, Any]],
        *,
        preserve_planner_order: bool = False,
    ) -> list[dict[str, Any]]:
        """Use physics-first only as a deterministic fallback.

        A real architecture may intentionally use a controversy, taxonomy,
        historical-turning-point, or design-trade-off progression.  Reordering
        such an outline after planning destroys its argument.
        """
        if len(sections) <= 1:
            return sections
        if preserve_planner_order:
            sorted_sections = list(sections)
        else:
            tiers = [self._detect_section_tier(s) for s in sections]
            # Stable sort: sections with same tier keep their relative order
            sorted_sections = [s for _, s in sorted(zip(tiers, sections), key=lambda x: x[0])]
        # Renumber section IDs after reordering
        for idx, section in enumerate(sorted_sections, start=1):
            section["section_id"] = f"S{idx:02d}"
            for slot_idx, slot in enumerate(section.get("visual_argument_slots", []), start=1):
                slot["slot_id"] = f"S{idx:02d}-V{slot_idx:02d}"
        return sorted_sections

    def _attach_transitions(
        self,
        sections: list[dict[str, Any]],
        *,
        fill_missing_handoffs: bool = False,
    ) -> list[dict[str, Any]]:
        """Fill missing transition prose only on the deterministic path.

        On the Qwen production path, handoffs are Qwen-authored scientific
        organization; ``_validate_raw_section_division`` already rejects
        missing handoffs, so this routine never invents them for production.
        """
        if not fill_missing_handoffs:
            return sections
        for i, (left, right) in enumerate(zip(sections, sections[1:])):
            left_labels = ", ".join((left.get("generated_from") or {}).get("central_labels", [])[:2])
            right_labels = ", ".join((right.get("generated_from") or {}).get("central_labels", [])[:2])
            transition_fwd = compact(
                f"Move from {left.get('section_id')} to {right.get('section_id')} by showing how {left_labels or left.get('title')} creates the need to examine {right_labels or right.get('title')}.",
                420,
            )
            transition_back = compact(
                f"This section builds on the {left_labels or left.get('title')} foundation established in {left.get('section_id')}.",
                420,
            )
            if not compact(left.get("transition_to_next"), 420):
                left["transition_to_next"] = transition_fwd
            if not compact(right.get("transition_from_previous"), 420):
                right["transition_from_previous"] = transition_back
        return sections

    def _global_argument_map(self, sections: list[dict[str, Any]]) -> dict[str, Any]:
        edges = []
        for left, right in zip(sections, sections[1:]):
            edges.append(
                {
                    "from": left.get("section_id"),
                    "to": right.get("section_id"),
                    "relation": "sets_up",
                    "rationale": left.get("transition_to_next", ""),
                }
            )
        return {
            "node_type": "section_argument_nodes",
            "edges": edges,
            "claim_graph_status": "seeded; exact claim-level evidence binding is a later stage",
        }

    def _global_visual_strategy(self, sections: list[dict[str, Any]]) -> dict[str, Any]:
        role_counter = Counter()
        visual_ids = set()
        for section in sections:
            for slot in section.get("visual_argument_slots", []):
                for visual in slot.get("candidate_visual_chunks", []):
                    visual_ids.add(visual.get("chunk_id"))
                    role_counter[str(visual.get("visual_role") or "unknown")] += 1
        recommended = []
        if role_counter:
            for role, _ in role_counter.most_common(4):
                recommended.append(f"Use {role} assets only where they support a specific section argument.")
        return {
            "unique_candidate_visual_chunks": len(visual_ids),
            "visual_role_distribution": dict(role_counter),
            "recommended_review_level_figures": recommended,
            "review_example_figure_table_patterns": [
                x for x in self.review_example_memory.get("figure_table_patterns", [])[:6] if isinstance(x, dict)
            ]
            if self.review_example_memory
            else [],
        }

    @staticmethod
    def _planner_gap_seeds(planner_output: dict[str, Any]) -> list[dict[str, str]]:
        raw = planner_output.get("high_value_gap_seeds") if isinstance(planner_output, dict) else []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw[:5]:
            if not isinstance(item, dict):
                continue
            gap = compact(item.get("gap"), 360)
            query = compact(item.get("query"), 300)
            if not gap or not query:
                continue
            out.append(
                {
                    "gap": gap,
                    "query": query,
                    "why_high_marginal_value": compact(item.get("why_high_marginal_value"), 420),
                    "stop_condition": compact(item.get("stop_condition"), 360),
                }
            )
        return out

    def _dynamic_gap_seeds(self, evidence: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = []
        for section in sections:
            risks = section.get("evidence_risks", [])
            if any("low paper diversity" in str(x).lower() for x in risks):
                title = compact(section.get("title"), 120)
                gaps.append(
                    {
                        "gap": f"Evidence diversity may be insufficient for {title}.",
                        "query": compact(" ".join(section.get("candidate_search_seeds", [])[:2]), 220),
                        "why_high_marginal_value": "More diverse papers may change whether this deserves a major section or only a subsection.",
                        "stop_condition": "Stop when at least three independent papers or one strong review establish the dimension.",
                    }
                )
        clusters = evidence.get("cluster_candidates") if isinstance(evidence.get("cluster_candidates"), list) else []
        for cluster in clusters:
            counts = cluster.get("evidence_counts") if isinstance(cluster.get("evidence_counts"), dict) else {}
            if counts.get("visual_chunks", 0) == 0:
                labels = "; ".join(cluster.get("central_labels", [])[:3])
                gaps.append(
                    {
                        "gap": f"Visual explanation may be missing for {labels}.",
                        "query": compact(f"{labels} figure schematic benchmark review", 220),
                        "why_high_marginal_value": "A review blueprint with visual reasoning needs at least one inspectable figure for visually complex mechanisms or comparisons.",
                        "stop_condition": "Stop if no reliable source figure appears in the next focused retrieval pass.",
                    }
                )
        if not gaps:
            # Keep 1-3 search seeds, not unlimited reviewer nitpicks.
            for section in sections[:3]:
                labels = (section.get("generated_from") or {}).get("central_labels", [])
                gaps.append(
                    {
                        "gap": f"Check whether {section.get('title')} needs one newer or more authoritative review/perspective anchor.",
                        "query": compact(" ".join(labels[:3] + ["review perspective benchmark"]), 220),
                        "why_high_marginal_value": "This is a limited recency/authority check, not an open-ended criticism loop.",
                        "stop_condition": "Stop after finding one strong recent review/perspective or confirming the current corpus is sufficient.",
                    }
                )
        return gaps[:5]

    def _validate(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        sections = blueprint.get("sections", [])
        visual_paths_missing = []
        sections_without_visuals = []
        sections_without_nodes = []
        sections_without_text = []
        section_titles = {str(s.get("title") or "").lower() for s in sections}
        legacy_title_hits = sorted(section_titles & LEGACY_TEMPLATE_TITLES)
        visual_ids = set()
        text_ids = set()
        claims: list[dict[str, Any]] = []
        invalid_claim_anchor_ids: list[dict[str, str]] = []
        candidate_rows_by_id: dict[str, dict[str, Any]] = {}
        missing_retrieval_sources: list[str] = []
        missing_material_units: list[str] = []
        material_permission_mismatches: list[dict[str, str]] = []
        abstract_permission_violations: list[str] = []
        sections_without_proposition_anchors: list[str] = []
        for section in sections:
            if len(section.get("concept_map_nodes", [])) < 1:
                sections_without_nodes.append(section.get("section_id"))
            if len(section.get("candidate_text_chunk_ids", [])) < 2:
                sections_without_text.append(section.get("section_id"))
            section_visual_count = 0
            for slot in section.get("visual_argument_slots", []):
                for visual in slot.get("candidate_visual_chunks", []):
                    section_visual_count += 1
                    visual_ids.add(visual.get("chunk_id"))
                    path = visual.get("local_image_path")
                    if path and not Path(path).exists():
                        visual_paths_missing.append({"section_id": section.get("section_id"), "chunk_id": visual.get("chunk_id"), "path": path})
            if section_visual_count < 1:
                sections_without_visuals.append(section.get("section_id"))
            section_has_proposition_anchor = False
            for row in section.get("candidate_text_chunks") or []:
                if not isinstance(row, dict):
                    continue
                chunk_id = str(row.get("chunk_id") or "")
                if not chunk_id:
                    continue
                candidate_rows_by_id[chunk_id] = row
                if not str(row.get("retrieval_source") or ""):
                    missing_retrieval_sources.append(chunk_id)
                binding = row.get("material_card_binding") if isinstance(row.get("material_card_binding"), dict) else {}
                if binding.get("propositions"):
                    section_has_proposition_anchor = True
                if self.material_units_by_chunk_id:
                    unit = self.material_units_by_chunk_id.get(chunk_id)
                    if not unit:
                        missing_material_units.append(chunk_id)
                        continue
                    quality = ((unit.get("durable_content_card") or {}).get("content_quality") or {})
                    provenance = ((unit.get("audit") or {}).get("source_provenance") or {})
                    expected_permission = str(
                        quality.get("evidence_ceiling")
                        or provenance.get("use_permission")
                        or "discovery_only"
                    )
                    actual_permission = str(row.get("use_permission") or "")
                    if expected_permission != actual_permission:
                        material_permission_mismatches.append({
                            "chunk_id": chunk_id,
                            "expected": expected_permission,
                            "actual": actual_permission,
                        })
                    source_kind = str(quality.get("source_kind") or row.get("source_kind") or "").lower()
                    if source_kind in {"abstract", "abstract_claim"} and (
                        actual_permission == "factual_support"
                        or bool(row.get("factual_support_allowed"))
                    ):
                        abstract_permission_violations.append(chunk_id)
            if self.material_units_by_chunk_id and not section_has_proposition_anchor:
                sections_without_proposition_anchors.append(str(section.get("section_id") or ""))
            for cid in section.get("candidate_text_chunk_ids", []):
                text_ids.add(cid)
            section_anchor_ids = set(section.get("candidate_text_chunk_ids") or [])
            for claim in section.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                claims.append(claim)
                for cid in claim.get("supporting_text_chunk_ids") or []:
                    if cid not in section_anchor_ids:
                        invalid_claim_anchor_ids.append({
                            "claim_id": str(claim.get("claim_id", "")),
                            "chunk_id": str(cid),
                        })
        claim_counts = [len(s.get("claims") or []) for s in sections]
        def requires_conclusion_grade_evidence(claim: dict[str, Any]) -> bool:
            state = str(claim.get("claim_state") or "planned").lower()
            disposition = str(claim.get("closure_disposition") or "").lower()
            requirement = str(claim.get("evidence_requirement") or "factual").lower()
            if state in {"dropped", "open_question", "reframed"}:
                return False
            if disposition in {"dropped", "open_question", "recommendation"}:
                return False
            return requirement == "factual"

        factual_claims = [c for c in claims if requires_conclusion_grade_evidence(c)]
        bound_claims = [
            c for c in factual_claims
            if c.get("supporting_text_chunk_ids")
            and c.get("evidence_binding_status") in {"direct", "synthesized", "partial"}
        ]
        load_bearing_unbound = [
            str(c.get("claim_id", "")) for c in factual_claims
            if c.get("load_bearing")
            and (
                not c.get("supporting_text_chunk_ids")
                or c.get("evidence_binding_status") in {"insufficient", "contradicted"}
            )
        ]
        off_scope_claims = [
            str(c.get("claim_id", "")) for c in claims
            if c.get("section_fit") == "off_scope"
        ]
        claim_permission_violations: list[dict[str, str]] = []
        for claim in factual_claims:
            if claim.get("evidence_binding_status") not in {"direct", "synthesized", "partial"}:
                continue
            for chunk_id in claim.get("supporting_text_chunk_ids") or []:
                row = candidate_rows_by_id.get(str(chunk_id)) or {}
                if str(row.get("use_permission") or "") != "factual_support":
                    claim_permission_violations.append({
                        "claim_id": str(claim.get("claim_id") or ""),
                        "chunk_id": str(chunk_id),
                        "use_permission": str(row.get("use_permission") or "missing"),
                    })
        dag = blueprint.get("argument_dag") if isinstance(blueprint.get("argument_dag"), dict) else {}
        adjacent_transition_pairs = []
        for index in range(max(0, len(sections) - 1)):
            current = sections[index]
            following = sections[index + 1]
            adjacent_transition_pairs.append({
                "source_section_id": str(current.get("section_id") or ""),
                "target_section_id": str(following.get("section_id") or ""),
                "source_transition": bool(str(current.get("transition_to_next") or "").strip()),
                "target_transition": bool(str(following.get("transition_from_previous") or "").strip()),
            })
        connected_transition_pairs = sum(
            row["source_transition"] and row["target_transition"]
            for row in adjacent_transition_pairs
        )
        planner_status = blueprint.get("planner_output_status") if isinstance(blueprint.get("planner_output_status"), dict) else {}
        visual_corpus_available = bool(visual_ids)
        scope_coverage = blueprint.get("scope_coverage_status") if isinstance(blueprint.get("scope_coverage_status"), dict) else {}
        seed_axis_coverage = (
            scope_coverage.get("seed_axis_coverage")
            if isinstance(scope_coverage.get("seed_axis_coverage"), dict)
            else {}
        )
        missing_seed_axis_ids = [
            str(value) for value in seed_axis_coverage.get("missing_axis_ids") or []
            if value
        ]
        scope_capacity_misses = [
            x for x in (scope_coverage.get("skipped_proposals") or [])
            if isinstance(x, dict) and x.get("reason") == "section_capacity_reached"
        ]
        real_claims = bool((blueprint.get("claim_decomposition_status") or {}).get("real_llm_claims"))
        binding_ratio = len(bound_claims) / max(1, len(factual_claims))
        material_cache_active = bool(self.material_units_by_chunk_id)
        contracts_missing = [
            str(claim.get("claim_id") or "")
            for claim in claims
            if not isinstance(claim.get("evidence_contract"), dict)
        ]
        contract_gap_claims = [
            str(claim.get("claim_id") or "")
            for claim in claims
            if isinstance(claim.get("evidence_contract"), dict)
            and (claim.get("evidence_contract") or {}).get("status") == "gap"
        ]
        proposition_bound_candidate_count = sum(
            bool((row.get("material_card_binding") or {}).get("propositions"))
            for row in candidate_rows_by_id.values()
        )
        checks = {
            "has_dynamic_schema": blueprint.get("schema_version") == "dynamic_review_blueprint.v4",
            "has_nonempty_qwen_section_plan": bool(sections),
            "section_count_within_configured_range": (
                self.min_sections <= len(sections) <= self.max_sections
            ),
            "all_sections_have_concept_nodes": not sections_without_nodes,
            "most_sections_have_text_anchors": len(sections_without_text) <= max(1, len(sections) // 4),
            "most_sections_have_visual_candidates": (not visual_corpus_available) or len(sections_without_visuals) <= max(1, len(sections) // 3),
            "enough_unique_text_chunks": len(text_ids) >= max(12, len(sections) * 3),
            # The blueprint only needs a viable visual seed for each section.
            # M4 subsequently searches the full visual KB and performs
            # multimodal reranking; enforcing the final figure quota here would
            # reject a blueprint before the stage responsible for fixing it.
            # A text-only acquisition round is allowed to produce a usable
            # text blueprint. Once visual assets exist, the original quota is
            # enforced; before that, visual work is explicitly pending rather
            # than turning a valid text plan into a hard failure.
            "enough_unique_visual_chunks": (not visual_corpus_available) or len(visual_ids) >= max(4, len(sections)),
            "all_visual_paths_exist": not visual_paths_missing,
            "no_legacy_template_title_hits": not legacy_title_hits,
            "has_integrated_refinement_status": bool(blueprint.get("integrated_refinement_status", {}).get("sidecar_refinement_removed")),
            "has_traceable_planning_evidence": bool(blueprint.get("planning_evidence_brief", {}).get("cluster_candidates")),
            "candidate_retrieval_sources_preserved": (not material_cache_active) or not missing_retrieval_sources,
            "candidate_chunks_exist_in_material_cache": (not material_cache_active) or not missing_material_units,
            "candidate_material_permissions_match_store": (not material_cache_active) or not material_permission_mismatches,
            "abstract_permission_ceiling_preserved": not abstract_permission_violations,
            "all_sections_have_proposition_bound_anchor": (not material_cache_active) or not sections_without_proposition_anchors,
            "bound_factual_claims_respect_permission": (not material_cache_active) or not claim_permission_violations,
            "all_sections_have_2_to_5_claims": bool(claim_counts) and all(2 <= n <= 5 for n in claim_counts),
            # Hand-built legacy fixtures may predate the contract stage.  A
            # real planner run advertises ``argument_quality`` and is then
            # required to provide one contract per claim.
            "all_claims_have_evidence_contracts": (
                not isinstance(blueprint.get("argument_quality"), dict)
                or not contracts_missing
            ),
            "claim_anchor_ids_are_local_and_traceable": not invalid_claim_anchor_ids,
            "claim_evidence_binding_quality": (not real_claims) or binding_ratio >= 0.70,
            "load_bearing_claims_are_evidence_bound": (not real_claims) or not load_bearing_unbound,
            "claims_fit_their_assigned_sections": (not real_claims) or not off_scope_claims,
            # Narrative continuity and scientific claim relations are separate
            # contracts. Requiring one claim edge per section boundary forces
            # agents to invent causal bridges when evidence is sparse.
            "section_narrative_is_connected": (
                not adjacent_transition_pairs
                or connected_transition_pairs == len(adjacent_transition_pairs)
            ),
            "argument_graph_has_cross_section_structure": int(
                dag.get("cross_section_edge_count") or 0
            ) >= (1 if len(sections) > 1 else 0),
            "requested_llm_planner_did_not_silently_fallback": (not self.real_llm_plan) or bool(planner_status.get("llm_used_in_blueprint")),
            "user_intent_dimensions_preserved": not scope_capacity_misses and not missing_seed_axis_ids,
        }
        validation_passed = all(checks.values())
        return {
            "schema_version": "dynamic_review_blueprint_validation.v1",
            "created_at": utc_now(),
            "passed": validation_passed,
            "checks": checks,
            "counts": {
                "sections": len(sections),
                "section_count_range": [self.min_sections, self.max_sections],
                "unique_candidate_text_chunks": len(text_ids),
                "unique_candidate_visual_chunks": len(visual_ids),
                "visual_corpus_available": visual_corpus_available,
                "visual_evidence_pending": not visual_corpus_available,
                "sections_without_visuals": len(sections_without_visuals),
                "sections_without_concept_nodes": len(sections_without_nodes),
                "sections_without_text": len(sections_without_text),
                "missing_visual_paths": len(visual_paths_missing),
                "legacy_title_hits": len(legacy_title_hits),
                "claims": len(claims),
                "factual_claims_requiring_evidence": len(factual_claims),
                "open_question_claims": sum(
                    c.get("evidence_requirement") == "open_question"
                    or c.get("claim_state") == "open_question"
                    or c.get("closure_disposition") == "open_question"
                    for c in claims
                ),
                "normative_claims": sum(
                    c.get("evidence_requirement") == "normative"
                    or c.get("closure_disposition") == "recommendation"
                    for c in claims
                ),
                "dropped_claims": sum(c.get("claim_state") == "dropped" for c in claims),
                "evidence_bound_claims": len(bound_claims),
                "evidence_binding_ratio": round(binding_ratio, 4),
                "load_bearing_unbound": len(load_bearing_unbound),
                "invalid_claim_anchor_ids": len(invalid_claim_anchor_ids),
                "off_scope_claims": len(off_scope_claims),
                "dag_edges": int(dag.get("edge_count") or 0),
                "dag_grounded_edges": int(dag.get("grounded_edge_count") or 0),
                "dag_provisional_edges": int(dag.get("provisional_edge_count") or 0),
                "adjacent_section_transitions": len(adjacent_transition_pairs),
                "connected_section_transitions": int(connected_transition_pairs),
                "scope_sections_added": len(scope_coverage.get("added_sections") or []),
                "scope_capacity_misses": len(scope_capacity_misses),
                "expected_seed_axes": len(seed_axis_coverage.get("expected_axis_ids") or []),
                "covered_seed_axes": len(seed_axis_coverage.get("covered_axis_ids") or []),
                "missing_seed_axes": len(missing_seed_axis_ids),
                "material_cache_active": material_cache_active,
                "candidate_chunks_with_material_units": len(candidate_rows_by_id) - len(set(missing_material_units)),
                "proposition_bound_candidate_chunks": proposition_bound_candidate_count,
                "sections_without_proposition_anchors": len(sections_without_proposition_anchors),
                "missing_candidate_retrieval_sources": len(set(missing_retrieval_sources)),
                "missing_candidate_material_units": len(set(missing_material_units)),
                "material_permission_mismatches": len(material_permission_mismatches),
                "abstract_permission_violations": len(set(abstract_permission_violations)),
                "claim_permission_violations": len(claim_permission_violations),
                "claims_without_evidence_contracts": len(contracts_missing),
                "candidate_contract_gap_claims": len(contract_gap_claims),
            },
            "samples": {
                "sections_without_visuals": sections_without_visuals,
                "sections_without_concept_nodes": sections_without_nodes,
                "sections_without_text": sections_without_text,
                "missing_visual_paths": visual_paths_missing[:10],
                "legacy_title_hits": legacy_title_hits,
                "section_titles": [s.get("title") for s in sections],
                "load_bearing_unbound": load_bearing_unbound[:20],
                "invalid_claim_anchor_ids": invalid_claim_anchor_ids[:20],
                "off_scope_claims": off_scope_claims[:20],
                "scope_sections_added": scope_coverage.get("added_sections") or [],
                "scope_capacity_misses": scope_capacity_misses,
                "missing_seed_axis_ids": missing_seed_axis_ids,
                "seed_axes_added_to_existing_sections": seed_axis_coverage.get("added_axes") or [],
                "incomplete_section_transitions": [
                    row
                    for row in adjacent_transition_pairs
                    if not (row["source_transition"] and row["target_transition"])
                ],
                "sections_without_proposition_anchors": sections_without_proposition_anchors,
                "missing_candidate_retrieval_sources": sorted(set(missing_retrieval_sources))[:20],
                "missing_candidate_material_units": sorted(set(missing_material_units))[:20],
                "material_permission_mismatches": material_permission_mismatches[:20],
                "abstract_permission_violations": sorted(set(abstract_permission_violations))[:20],
                "claim_permission_violations": claim_permission_violations[:20],
                "claims_without_evidence_contracts": contracts_missing[:20],
                "candidate_contract_gap_claims": contract_gap_claims[:20],
            },
            "readiness": {
                "blueprint_structure_ready": validation_passed,
                "claim_evidence_stage": "claim_bound" if real_claims else "planning_anchors_only",
                "claim_evidence_ready": bool(real_claims)
                and binding_ratio >= 0.70
                and not claim_permission_violations,
                "writing_ready": bool(real_claims)
                and binding_ratio >= 0.70
                and not claim_permission_violations
                and not load_bearing_unbound,
                "note": (
                    "A structurally valid blueprint is not yet writing-ready when claims have only planning anchors."
                ),
            },
            "advisories": {
                "claim_graph_below_nominal_bridge_density": int(
                    dag.get("cross_section_edge_count") or 0
                ) < max(1, len(sections) - 1),
                "nominal_bridge_target": max(1, len(sections) - 1),
                "note": (
                    "A sparse claim graph is not an automatic failure when the section narrative is "
                    "connected and weak relations are explicitly provisional; it remains a target for "
                    "M3 evidence supplementation or claim reframing."
                ),
            },
            "admission_decision": (
                "admit"
                if (
                    bool(blueprint.get("production_blueprint"))
                    and self.min_sections <= len(sections) <= self.max_sections
                    and validation_passed
                )
                else "reject"
            ),
            "admission": {
                "decision": (
                    "admit"
                    if (
                        bool(blueprint.get("production_blueprint"))
                        and self.min_sections <= len(sections) <= self.max_sections
                        and validation_passed
                    )
                    else "reject"
                ),
                "production": bool(blueprint.get("production_blueprint")),
                "non_production_deterministic": not bool(
                    blueprint.get("production_blueprint")
                ),
                "qwen_architecture_present": bool(
                    blueprint.get("production_blueprint")
                ),
                "section_count": len(sections),
                "section_count_range": [
                    self.min_sections,
                    self.max_sections,
                ],
                "validation_passed": bool(validation_passed),
                "reason": (
                    "Qwen architecture is present, within 8-10 chapters, and "
                    "passed all build-time checks."
                    if (
                        bool(blueprint.get("production_blueprint"))
                        and self.min_sections <= len(sections) <= self.max_sections
                        and validation_passed
                    )
                    else (
                        "Deterministic fallback is explicitly non-production "
                        "and never admitted to the production mainline."
                        if not blueprint.get("production_blueprint")
                        else "Qwen architecture failed build-time validation "
                        "or is outside the 8-10 chapter contract."
                    )
                ),
            },
        }

    def _markdown(self, blueprint: dict[str, Any], validation: dict[str, Any]) -> str:
        lines = [
            "# Dynamic Review Blueprint v4",
            "",
            f"- Passed validation: `{validation.get('passed')}`",
            f"- Admission decision: `{blueprint.get('admission_decision')}`",
            f"- Planning mode: `{blueprint.get('planning_mode')}`",
            f"- Source concept map: `{blueprint.get('source_concept_map')}`",
            f"- Source knowledge base: `{blueprint.get('source_review_knowledge_base')}`",
            f"- Legacy template title hits: `{validation.get('counts', {}).get('legacy_title_hits')}`",
            "",
            "## Thesis",
            "",
            blueprint.get("review_thesis", ""),
            "",
            "## Sections",
            "",
        ]
        for section in blueprint.get("sections", []):
            visual_count = sum(len(slot.get("candidate_visual_chunks", [])) for slot in section.get("visual_argument_slots", []))
            labels = ", ".join((section.get("generated_from") or {}).get("central_labels", [])[:4])
            lines.extend(
                [
                    f"### {section.get('section_id')} {section.get('title')}",
                    "",
                    section.get("argument_role", ""),
                    "",
                    f"- Dynamic labels: {labels}",
                    f"- Concept nodes: {len(section.get('concept_map_nodes', []))}",
                    f"- Candidate text chunks: {len(section.get('candidate_text_chunk_ids', []))}",
                    f"- Candidate visual chunks: {visual_count}",
                    "",
                ]
            )
            for claim in section.get("claim_graph_seed", {}).get("central_claim_candidates", [])[:3]:
                lines.append(f"- Claim seed `{claim.get('claim_seed_id')}`: {claim.get('claim_seed')}")
            for slot in section.get("visual_argument_slots", []):
                lines.append(f"- Visual slot `{slot.get('slot_id')}`: {slot.get('goal')} ({len(slot.get('candidate_visual_chunks', []))} candidates)")
            lines.append("")
        lines.extend(["## High-value gap seeds", ""])
        for item in blueprint.get("high_value_gap_seeds", []):
            lines.append(f"- {item.get('gap')}  Query: `{item.get('query')}`")
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a dynamic, evidence-aware review blueprint.")
    parser.add_argument("--concept-map", default=str(DEFAULT_CONCEPT_MAP))
    parser.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR))
    parser.add_argument("--material-units", default=None,
                        help="Durable MATERIAL_UNITS_FINAL.json cache used as a local evidence overlay/fallback")
    parser.add_argument("--material-vectors", default=None,
                        help="SQLite semantic vector cache used before lexical material retrieval")
    parser.add_argument("--material-embedding-model", default="text-embedding-v4")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--user-question", default="Build a high-quality scientific literature review from the selected corpus.")
    parser.add_argument("--problem-understanding", default="The review should synthesize the selected literature into a coherent expert-level narrative.")
    parser.add_argument("--scope-definition", default="Focus on review planning, evidence anchors, visual argument planning, and high-value gap discovery.")
    parser.add_argument("--query-plan", default=None,
                        help="Validated QueryPlanner JSON/review package; overrides the three free-text input fields")
    parser.add_argument("--review-example-memory", default=str(DEFAULT_REVIEW_EXAMPLE_MEMORY))
    parser.add_argument(
        "--real-llm-plan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Qwen-required global architecture plan (default: True); "
            "--no-real-llm-plan enables the explicit non-production "
            "deterministic fallback"
        ),
    )
    parser.add_argument("--enable-mentor", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--real-llm-mentor", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--mentor-model-tier", default="advanced_model")
    parser.add_argument("--mentor-three-party", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--mentor-advice-path", default=None)
    parser.add_argument("--domain-config", default=None, help="Per-run domain_config.yaml; avoids global-topic coupling")
    parser.add_argument("--real-llm-claims", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--real-llm-dag", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--active-library-path", default=None, help="Path to active_library.json")
    parser.add_argument("--dag-candidate-mode", default=None,
                        choices=["exhaustive", "ranked_topk"],
                        help="Layer 3 candidate selection mode (auto if omitted)")
    parser.add_argument("--dag-topk-per-target", type=int, default=5,
                        help="Top-k candidates per target claim when candidate_mode=ranked_topk (default: 5)")
    parser.add_argument("--dag-max-layer4-candidates", type=int, default=80,
                        help="Cap Layer4 LLM calls to this many candidates (default: 80, 0=unlimited)")
    parser.add_argument("--m3-real-gap-rounds", type=int, default=0,
                        help="Run targeted OA-only M3-real gap loop for this many rounds (default: 0/off)")
    parser.add_argument("--m3-real-max-claims", type=int, default=3)
    parser.add_argument("--m3-real-saturation-threshold", type=float, default=1.5)
    parser.add_argument("--m3-real-output-dir", default=None)
    parser.add_argument("--m3-real-metadata-db", default=None)
    parser.add_argument("--m3-real-topic-context", default="")
    parser.add_argument("--m3-real-max-queries", type=int, default=3)
    parser.add_argument("--m3-real-results-per-backend", type=int, default=10)
    parser.add_argument("--m3-real-top-k", type=int, default=5)
    parser.add_argument("--m3-real-from-year", type=int, default=None)
    parser.add_argument("--m3-real-download-top-n", type=int, default=2)
    parser.add_argument("--m3-real-citation-chase-top-n", type=int, default=2)
    parser.add_argument("--m3-real-references-per-seed", type=int, default=8)
    parser.add_argument("--m3-adaptive-closure", action=argparse.BooleanOptionalAction, default=True,
                        help="After retrieval stops, keep, narrow, reframe, open, or drop unresolved claims audibly")
    parser.add_argument(
        "--s2-first",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use S2-first snippets/recommendations before the legacy M3 path.",
    )
    parser.add_argument("--s2-literature-graph", default=None)
    parser.add_argument("--planner-prompt-path", default=str(DEFAULT_PLANNER_PROMPT))
    parser.add_argument("--planner-model-tier", default="premium_model")
    parser.add_argument(
        "--planner-max-tokens",
        type=int,
        default=9600,
        help=(
            "Architecture output budget for the 8-10 section global plan "
            "(default: 9600); larger values reduce partial-JSON repair on "
            "complex reviews"
        ),
    )
    parser.add_argument(
        "--served-text-limit",
        type=int,
        default=0,
        help=(
            "Maximum ranked text candidates served per section; 0 preserves "
            "every retrieved unique candidate (production default). Explicit "
            "positive values constrain deliberately limited runs and are "
            "reported in the candidate pool audit."
        ),
    )
    parser.add_argument(
        "--model-text-context-limit",
        type=int,
        default=0,
        help=(
            "Chunk-index summaries exposed to downstream claim planning; 0 "
            "means all retained candidates. Raw text is never bulk-injected; "
            "all retained candidates are always covered by evidence digest "
            "batches."
        ),
    )
    parser.add_argument(
        "--evidence-batch-size",
        type=int,
        default=12,
        help="Number of candidate chunks summarized per evidence batch (default: 12)",
    )
    parser.add_argument("--planner-architecture-path", default=None,
                        help="Reuse a previously saved architecture JSON; normally reuses grounded checkpoints without re-grounding")
    parser.add_argument(
        "--force-reground",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When reusing --planner-architecture-path, preserve the Qwen "
            "chapter division but force every section's evidence grounding / "
            "candidate pools / claim bindings to be rebuilt from the currently "
            "configured material library and vector cache. Default: False."
        ),
    )
    parser.add_argument(
        "--min-sections",
        type=int,
        default=8,
        help=(
            "Minimum chapter count enforced by Python on the production path "
            "(default: 8; must remain 8 with --max-sections 10). Qwen owns "
            "which chapters; Python rejects any count outside the range."
        ),
    )
    parser.add_argument(
        "--max-sections",
        type=int,
        default=10,
        help=(
            "Maximum chapter count enforced by Python on the production path "
            "(default: 10; must remain 10 with --min-sections 8). Qwen owns "
            "which chapters; Python rejects any count outside the range."
        ),
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    user_question = args.user_question
    problem_understanding = args.problem_understanding
    scope_definition = args.scope_definition
    query_plan_path = Path(args.query_plan) if args.query_plan else None
    if query_plan_path:
        package = load_json(query_plan_path)
        payload = package.get("result") if isinstance(package.get("result"), dict) else package
        input_block = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        output_block = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        scope_block = output_block.get("scope_definition")
        if not input_block.get("user_query") or not output_block.get("problem_understanding") or not isinstance(scope_block, dict):
            raise ValueError(f"Invalid QueryPlanner artifact: {query_plan_path}")
        problem_understanding = str(output_block["problem_understanding"])
        # The raw user query remains in the QueryPlanner artifact for
        # provenance.  All downstream model-facing context is English.
        user_question = problem_understanding
        scope_definition = "\n".join(
            [str(scope_block.get("main_scope") or "")]
            + [f"- {x}" for x in (scope_block.get("scope_items") or []) if str(x).strip()]
        ).strip()
    result = DynamicReviewBlueprintPlanner(
        Path(args.concept_map),
        Path(args.output_dir),
        user_question=user_question,
        problem_understanding=problem_understanding,
        scope_definition=scope_definition,
        query_plan_path=query_plan_path,
        kb_dir=Path(args.kb_dir) if args.kb_dir else None,
        material_units_path=Path(args.material_units) if args.material_units else None,
        material_vectors_path=Path(args.material_vectors) if args.material_vectors else None,
        material_embedding_model=args.material_embedding_model,
        review_example_memory_path=Path(args.review_example_memory) if args.review_example_memory else None,
        real_llm_plan=bool(args.real_llm_plan),
        enable_mentor=bool(args.enable_mentor),
        real_llm_mentor=bool(args.real_llm_mentor),
        mentor_model_tier=args.mentor_model_tier,
        mentor_three_party=bool(args.mentor_three_party),
        mentor_advice_path=Path(args.mentor_advice_path) if args.mentor_advice_path else None,
        domain_config_path=Path(args.domain_config) if args.domain_config else None,
        real_llm_claims=bool(args.real_llm_claims),
        real_llm_dag=bool(args.real_llm_dag),
        active_library_path=args.active_library_path,
        dag_candidate_mode=args.dag_candidate_mode,
        dag_topk_per_target=int(args.dag_topk_per_target),
        dag_max_layer4_candidates=None if getattr(args, "dag_max_layer4_candidates", 80) == 0 else getattr(args, "dag_max_layer4_candidates", 80),
        m3_real_gap_rounds=int(args.m3_real_gap_rounds),
        m3_real_max_claims=int(args.m3_real_max_claims),
        m3_real_saturation_threshold=float(args.m3_real_saturation_threshold),
        m3_real_output_dir=Path(args.m3_real_output_dir) if args.m3_real_output_dir else None,
        m3_real_metadata_db=Path(args.m3_real_metadata_db) if args.m3_real_metadata_db else None,
        m3_real_topic_context=args.m3_real_topic_context,
        m3_real_max_queries=int(args.m3_real_max_queries),
        m3_real_results_per_backend=int(args.m3_real_results_per_backend),
        m3_real_top_k=int(args.m3_real_top_k),
        m3_real_from_year=args.m3_real_from_year,
        m3_real_download_top_n=int(args.m3_real_download_top_n),
        m3_real_citation_chase_top_n=int(args.m3_real_citation_chase_top_n),
        m3_real_references_per_seed=int(args.m3_real_references_per_seed),
        m3_adaptive_closure=bool(args.m3_adaptive_closure),
        s2_first_enabled=bool(args.s2_first),
        s2_literature_graph_path=(
            Path(args.s2_literature_graph) if args.s2_literature_graph else None
        ),
        planner_prompt_path=Path(args.planner_prompt_path),
        planner_model_tier=args.planner_model_tier,
        planner_max_tokens=int(args.planner_max_tokens),
        planner_architecture_path=Path(args.planner_architecture_path) if args.planner_architecture_path else None,
        force_reground=bool(args.force_reground),
        served_text_limit=(
            None
            if int(args.served_text_limit) <= 0
            else int(args.served_text_limit)
        ),
        model_text_context_limit=(None if int(args.model_text_context_limit) <= 0 else int(args.model_text_context_limit)),
        evidence_batch_size=int(args.evidence_batch_size),
        min_sections=int(args.min_sections),
        max_sections=int(args.max_sections),
    ).build()
    print(
        json.dumps(
            {
                "ok": True,
                "passed": result["validation"].get("passed"),
                "paths": result["paths"],
                "counts": result["validation"].get("counts"),
                "section_titles": result["validation"].get("samples", {}).get("section_titles", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["validation"].get("passed") else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
