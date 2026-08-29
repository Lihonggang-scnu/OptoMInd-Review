"""Deterministic article-driven visual asset planner (sidecar core).

This module is an additive adapter, not a second visual factory.  It reads a
completed article/section drafts and a searchable visual-cache index, then
emits a deterministic visual construction plan that is compatible with the
existing Visual Editor and Visual Evidence Factory contracts.

Design rules implemented here:

* 0-2 source placements per section; zero is always allowed.
* Weak filler is rejected through a relevance threshold, utility/confidence
  filters, and explicit claim binding.
* A cached image (by canonical path or chunk id) is never reused twice.
* Local source visuals are preferred; whole-figure versus coherent-subfigure
  decisions are made from cache lineage, not guessed.
* Article-level coverage requires at least one mechanism/conceptual diagram and
  one workflow/process diagram; unmet requirements become unfilled needs and
  never block prose.
* Only non-empirical explanatory visuals can become generation requests, and
  every request carries mandatory AI disclosure.  Curves, microscopy, spectra,
  measurements and simulation results are never requested for generation.
* Tag/vector shortlisting happens first; final candidates carry an explicit
  image-review-required state.
* Validation is deterministic and fails open to a partial, still-usable plan.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from optomind_research.visual_argument_protocol import (
    VALID_VISUAL_ARGUMENT_TYPES,
)

from .visual_source_contracts import (
    build_figure_contract,
    build_visual_source_map,
    validate_figure_contract,
    validate_visual_source_map,
)

CONSTRUCTION_PLAN_SCHEMA_VERSION = (
    "research_harness.article_visual_construction_plan.v1"
)
EDITORIAL_PLAN_SCHEMA_VERSION = (
    "research_harness.visual_editorial_plan.v1"
)
IMAGE_REVIEW_SCHEMA_VERSION = (
    "research_harness.article_visual_image_review.v1"
)

ALLOWED_CONCEPTUAL_FIGURE_KINDS = frozenset(
    {
        "concept_map",
        "mechanism_schematic",
        "workflow_schematic",
        "taxonomy_diagram",
    }
)

# Kinds the Visual Editor may accept, but which this planner never emits as
# generation requests because they imply quantitative or empirical content.
EMPIRICAL_FIGURE_KINDS = frozenset(
    {
        "data_infographic",
        "trend_schematic",
        "comparison_diagram",
    }
)

FORBIDDEN_GENERATION_KINDS = EMPIRICAL_FIGURE_KINDS | frozenset(
    {
        "source_figure",
        "source_table",
        "spectrum",
        "micrograph",
        "measurement_plot",
        "simulation_plot",
    }
)

PLANNER_ASSET_KINDS = frozenset(
    {
        "figure",
        "table",
        "diagram",
        "photo",
        "equation",
        "page_region",
        "unknown",
    }
)

EMPIRICAL_SIGNALS = (
    "spectrum",
    "spectra",
    "microscopy",
    "micrograph",
    "measurement",
    "measured",
    "experimental result",
    "simulation",
    "simulated",
    "curve",
    "plot",
    "benchmark",
    "performance data",
    "transmittance",
    "reflectance",
    "absorbance",
    "xrd",
    "xps",
    "ftir",
)

CONCEPTUAL_SIGNALS = (
    "mechanism",
    "explain",
    "illustrate",
    "principle",
    "process",
    "workflow",
    "schematic",
    "concept",
    "taxonomy",
    "roadmap",
    "architecture",
    "pipeline",
    "how",
)

ROLE_TO_PREFERRED_TYPES: Dict[str, Tuple[str, ...]] = {
    "mechanism": (
        "mechanism_anchor",
        "method_or_workflow",
        "trend_or_parameter_map",
    ),
    "materials": (
        "method_or_workflow",
        "mechanism_anchor",
        "representative_example",
    ),
    "performance": (
        "quantitative_comparison",
        "trend_or_parameter_map",
        "mechanism_anchor",
    ),
    "applications": (
        "representative_example",
        "method_or_workflow",
        "quantitative_comparison",
    ),
    "challenges": (
        "anomaly_or_limitation",
        "quantitative_comparison",
        "trend_or_parameter_map",
    ),
    "methods": (
        "method_or_workflow",
        "quantitative_comparison",
        "trend_or_parameter_map",
    ),
    "future": (
        "taxonomy_or_roadmap",
        "synthesis_overview",
        "trend_or_parameter_map",
    ),
}

GENERIC_ROLE_RULES: Dict[str, Tuple[str, ...]] = {
    "mechanism": ("mechanism", "physical", "principle", "theory"),
    "materials": ("material", "structure", "fabrication"),
    "performance": ("metric", "measurement", "performance", "benchmark"),
    "applications": ("application", "deployment", "device", "system"),
    "challenges": ("trade-off", "tradeoff", "bottleneck", "limitation", "challenge", "gap"),
    "methods": ("method", "characterization", "simulation", "workflow"),
    "future": ("future", "frontier", "roadmap", "outlook"),
}

APPROVED_CLAIM_STATUSES = frozenset({"approved", "accepted", "cited"})
_CAPTION_UNAVAILABLE_MARKER = "caption unavailable"

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "this", "that", "with", "from", "are", "was",
        "has", "have", "its", "can", "may", "but", "not", "also", "all",
        "than", "been", "more", "such", "these", "their", "they", "both",
        "some", "well", "any", "our", "one", "two", "fig", "figure", "table",
        "above", "below", "left", "right", "shown", "show", "shows", "used",
        "using", "where", "when", "which", "were", "will", "into", "under",
        "over", "each", "other", "while", "here", "thus", "then", "between",
        "through", "within", "during", "after", "upon", "about", "result",
        "results", "study", "studies", "analysis", "value", "values", "sample",
        "samples", "method", "methods", "data", "use", "due", "per", "via",
        "see", "new", "high", "low", "large", "small", "black", "white", "red",
        "blue", "green", "color", "top", "bottom", "panel", "panels", "argument",
        "visual", "claim", "type", "status", "chunk", "supported", "aspect",
        "confidence", "schema", "version", "role", "anchor", "provide",
        "provides", "provided", "demonstrate", "demonstrated", "indicate",
        "indicates", "present", "presents", "compare", "compared", "comparing",
        "based", "obtain", "obtained", "measure", "measured", "calculate",
        "calculated", "improve", "improved", "increase", "increased",
        "decrease", "decreased", "significant", "significantly", "difference",
        "different", "better", "worse", "higher", "lower", "greater",
        "smaller", "body", "text", "section", "page", "background", "related",
        "work", "research", "paper", "approach", "various", "several", "first",
        "second", "third", "note", "main", "typical", "correspond",
        "corresponding", "apply", "applied", "given", "together", "along",
        "across", "around", "without", "although", "however", "therefore",
        "respectively",
    }
)


@dataclass(frozen=True)
class ArticleClaimInput:
    """One approved/cited claim available for visual binding."""

    claim_id: str
    statement: str = ""
    status: str = "approved"
    citation_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleCitationInput:
    """One citation available as provenance context."""

    citation_id: str
    paper_id: str = ""
    doi: str = ""
    text: str = ""


@dataclass(frozen=True)
class ArticleSectionInput:
    """One completed manuscript/section draft."""

    section_id: str
    title: str = ""
    text: str = ""
    argument_role: str = ""
    claims: Tuple[ArticleClaimInput, ...] = ()
    citations: Tuple[ArticleCitationInput, ...] = ()
    expected_visual_arguments: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleVisualAssetPlannerConfig:
    """Deterministic planning knobs.  Defaults are deliberately conservative."""

    max_placements_per_section: int = 2
    max_generation_requests_per_section: int = 1
    max_total_visual_items_per_section: int = 2
    min_relevance_score: float = 0.05
    shortlist_top_k: int = 12
    shortlist_max_per_paper: int = 3
    # Soft article-level diversity strength.  The effective reuse penalty grows
    # quadratically with placements from the same paper and an unseen-paper
    # coverage bonus is applied, so similarly relevant multi-paper candidates
    # spread the article without hard quotas or candidate deletion.  A clearly
    # superior candidate still wins when its score gap exceeds the penalty.
    paper_diversity_penalty: float = 0.05
    max_subfigure_composite_panels: int = 3
    allow_subfigure_composites: bool = True
    emit_source_maps: bool = True
    emit_figure_contracts: bool = True
    separate_caption_attribution: bool = True
    coverage_requirements: Tuple[str, ...] = (
        "mechanism_or_conceptual_diagram",
        "workflow_or_process_diagram",
    )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_placements_per_section": self.max_placements_per_section,
            "max_generation_requests_per_section": (
                self.max_generation_requests_per_section
            ),
            "max_total_visual_items_per_section": (
                self.max_total_visual_items_per_section
            ),
            "min_relevance_score": self.min_relevance_score,
            "shortlist_top_k": self.shortlist_top_k,
            "shortlist_max_per_paper": self.shortlist_max_per_paper,
            "paper_diversity_penalty": float(
                self.paper_diversity_penalty
            ),
            "max_subfigure_composite_panels": (
                self.max_subfigure_composite_panels
            ),
            "allow_subfigure_composites": self.allow_subfigure_composites,
            "emit_source_maps": self.emit_source_maps,
            "emit_figure_contracts": self.emit_figure_contracts,
            "separate_caption_attribution": self.separate_caption_attribution,
            "coverage_requirements": list(self.coverage_requirements),
        }


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return {}


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _is_placeholder_caption(value: Any) -> bool:
    """True when the caption is the neutral missing-caption placeholder."""

    return _CAPTION_UNAVAILABLE_MARKER in str(value or "").casefold()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _path_key(path: Any) -> str:
    return str(Path(str(path or "")).resolve()).lower()


def _tokenize(text: str) -> Set[str]:
    tokens: Set[str] = set()
    for word in re.split(r"[\W_]+", str(text or "").lower()):
        if len(word) < 4 or len(word) > 18:
            continue
        if word in _STOPWORDS:
            continue
        if re.fullmatch(r"\d+", word) or re.fullmatch(r"\d+[a-z]?", word):
            continue
        tokens.add(word)
    return tokens


def _normalize_claim(value: Any, index: int) -> Dict[str, Any]:
    raw = _as_dict(value)
    claim_id = _coerce_str(raw.get("claim_id") or raw.get("id"))
    if not claim_id:
        raise ValueError(f"claim[{index}] missing claim_id")
    status = _coerce_str(raw.get("status") or "approved").lower()
    statement = _coerce_str(raw.get("statement") or raw.get("text"))
    return {
        "claim_id": claim_id,
        "statement": statement,
        "status": status,
        "approved": status in APPROVED_CLAIM_STATUSES,
        "citation_ids": _as_list(raw.get("citation_ids") or raw.get("citations")),
    }


def _normalize_citation(value: Any, index: int) -> Dict[str, Any]:
    raw = _as_dict(value)
    citation_id = _coerce_str(
        raw.get("citation_id") or raw.get("id") or raw.get("paper_id")
    )
    if not citation_id:
        raise ValueError(f"citation[{index}] missing citation_id")
    return {
        "citation_id": citation_id,
        "paper_id": _coerce_str(raw.get("paper_id")),
        "doi": _coerce_str(raw.get("doi")),
        "text": _coerce_str(raw.get("text")),
    }


def _normalize_section(value: Any, index: int) -> Dict[str, Any]:
    raw = _as_dict(value)
    section_id = _coerce_str(raw.get("section_id"))
    if not section_id:
        raise ValueError(f"section[{index}] missing section_id")
    claims: List[Dict[str, Any]] = []
    for claim_index, claim in enumerate(raw.get("claims") or []):
        claims.append(_normalize_claim(claim, claim_index))
    citations: List[Dict[str, Any]] = []
    for citation_index, citation in enumerate(raw.get("citations") or []):
        citations.append(_normalize_citation(citation, citation_index))
    expected = _as_list(raw.get("expected_visual_arguments"))
    text = _coerce_str(raw.get("text"))
    return {
        "section_id": section_id,
        "title": _coerce_str(raw.get("title")),
        "text": text,
        "argument_role": _coerce_str(
            raw.get("argument_role") or raw.get("chapter_argument")
        ),
        "claims": claims,
        "approved_claims": [row for row in claims if row["approved"]],
        "citations": citations,
        "expected_visual_arguments": expected,
        "approved_claim_ids": [
            row["claim_id"] for row in claims if row["approved"]
        ],
    }


def _normalize_permission(record: Dict[str, Any]) -> Dict[str, Any]:
    raw_permission = record.get("permission")
    if isinstance(raw_permission, dict):
        permission = raw_permission
    else:
        permission = {}
    raw_status = _coerce_str(
        permission.get("status")
        or permission.get("permission_status")
        or record.get("permission_status")
        or permission.get("rights")
    ).lower()
    allowed_values = {
        "allowed",
        "open_access",
        "permitted",
        "yes",
        "granted",
        "cc-by",
        "cc by",
        "creative commons",
    }
    restricted_values = {
        "restricted",
        "denied",
        "forbidden",
        "no",
        "copyrighted",
        "proprietary",
    }
    if raw_status in allowed_values:
        status = "allowed"
    elif raw_status in restricted_values:
        status = "restricted"
    else:
        status = "requires_review"
    license_name = _coerce_str(
        permission.get("license")
        or permission.get("license_name")
        or record.get("license")
        or record.get("rights")
    )
    return {
        "status": status,
        "license": license_name,
        "use_permission": _coerce_str(
            permission.get("use_permission")
            or record.get("use_permission")
        ),
        "evidence_ceiling": _coerce_str(
            permission.get("evidence_ceiling")
            or record.get("evidence_ceiling")
        ),
        "allowed_claim_kinds": _as_list(
            permission.get("allowed_claim_kinds")
            or record.get("allowed_claim_kinds")
        ),
        "note": _coerce_str(
            permission.get("note") or permission.get("permission_note")
            or record.get("permission_note")
        ),
    }


def _normalize_visual_cache_record(value: Any, index: int) -> Dict[str, Any]:
    raw = _as_dict(value)
    if not raw:
        raise ValueError(f"visual_cache_record[{index}] is not an object")
    chunk_id = _coerce_str(
        raw.get("chunk_id") or raw.get("visual_chunk_id")
    )
    paper_id = _coerce_str(raw.get("paper_id"))
    local_image_path = _coerce_str(raw.get("local_image_path"))
    if not chunk_id or not local_image_path:
        raise ValueError(
            f"visual_cache_record[{index}] missing chunk_id/local_image_path"
        )
    quality = raw.get("quality")
    if not isinstance(quality, dict):
        quality = {}
    kind = _coerce_str(
        raw.get("chunk_kind") or raw.get("asset_type")
    ).lower()
    subfigure_label = _coerce_str(raw.get("subfigure_label"))
    parent_label = _coerce_str(raw.get("parent_label"))
    parent_chunk_id = _coerce_str(
        raw.get("parent_chunk_id")
        or raw.get("parent_visual_chunk_id")
        or raw.get("parent_id")
    )
    source_kind = _coerce_str(raw.get("source_kind"))
    is_generated_visual = bool(
        kind == "generated_visual"
        or source_kind == "ai_generated_explanatory_visual"
        or _coerce_bool(raw.get("generated_visual"))
    )
    if not paper_id and not is_generated_visual:
        raise ValueError(f"visual_cache_record[{index}] missing paper_id")
    if is_generated_visual and not paper_id:
        paper_id = "generated:article_owned_visual"
    if "composite" in kind and "subfigure" not in kind:
        figure_mode = "whole_figure"
    elif subfigure_label or "subfigure" in kind or parent_chunk_id:
        figure_mode = "subfigure"
    else:
        figure_mode = "whole_figure"

    status_raw = _coerce_str(raw.get("visual_argument_status")).lower()
    if status_raw == "ok":
        status = "ok"
    elif status_raw in {"failed", "exclude", "excluded"}:
        status = "failed"
    else:
        status = "pending_multimodal_review"

    vtype = _coerce_str(raw.get("visual_argument_type"))
    if vtype not in VALID_VISUAL_ARGUMENT_TYPES:
        vtype = ""

    confidence = _coerce_str(
        raw.get("visual_argument_confidence")
        or raw.get("confidence")
        or "medium"
    ).lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    review_utility = _coerce_str(
        raw.get("review_utility")
        or (raw.get("visual_profile") or {}).get("review_utility")
        or ""
    ).lower()
    if review_utility not in {"high", "medium", "low", "exclude"}:
        review_utility = ""

    path = Path(local_image_path)
    path_exists = path.is_file()
    is_duplicate = _coerce_bool(
        raw.get("is_duplicate") or quality.get("is_duplicate")
    )
    failure_reason = _coerce_str(
        raw.get("failure_reason") or quality.get("failure_reason")
    )
    permission = _normalize_permission(raw)
    needs_human_review = _coerce_bool(
        raw.get("visual_argument_needs_human_review")
        or quality.get("needs_human_review")
    )
    snapshot_unit = raw.get("snapshot_unit")
    if not isinstance(snapshot_unit, dict):
        snapshot_unit = {}
    snapshot_figure = snapshot_unit.get("figure_identity")
    if not isinstance(snapshot_figure, dict):
        snapshot_figure = {}
    snapshot_permission = snapshot_unit.get("permission_state")
    if not isinstance(snapshot_permission, dict):
        snapshot_permission = {}
    asset_typing = raw.get("asset_typing")
    if not isinstance(asset_typing, dict):
        asset_typing = {}
    asset_kind = _coerce_str(
        raw.get("asset_kind")
        or asset_typing.get("asset_kind")
        or snapshot_figure.get("asset_kind")
        or (raw.get("source_map") or {}).get("asset_kind")
        or raw.get("asset_type")
        or kind
    ).lower()
    table_label_text = " ".join(
        [
            _coerce_str(raw.get("figure_label") or raw.get("label")),
            _coerce_str(
                raw.get("caption_original")
                or raw.get("caption")
                or raw.get("caption_preview")
            ),
            _coerce_str(raw.get("asset_type") or raw.get("chunk_kind")),
        ]
    ).lower()
    if asset_kind not in PLANNER_ASSET_KINDS:
        label_caption = table_label_text
        if re.search(r"\btable\b|\btbl\b", label_caption):
            asset_kind = "table"
        elif any(
            marker in label_caption
            for marker in ("equation", "formula")
        ):
            asset_kind = "equation"
        elif any(
            marker in label_caption
            for marker in ("diagram", "schematic", "scheme")
        ):
            asset_kind = "diagram"
        elif any(
            marker in label_caption
            for marker in ("photo", "photograph", "micrograph")
        ):
            asset_kind = "photo"
        else:
            asset_kind = "figure"
    if asset_kind != "table" and re.search(
        r"\btable\b|\btbl\b", table_label_text
    ):
        # Local Table labels are ground truth even when a cached advisor
        # payload mislabels the asset; Table 1 is never source_figure.
        asset_kind = "table"

    explicit_publication_eligible = raw.get("publication_eligible")
    if isinstance(explicit_publication_eligible, bool):
        publication_eligible = explicit_publication_eligible
        publication_reason = _coerce_str(
            raw.get("publication_eligible_reason")
            or ("explicit_publication_eligible" if publication_eligible else "explicit_publication_ineligible")
        )
    else:
        use_permission = _coerce_str(
            permission.get("use_permission")
            or raw.get("use_permission")
            or snapshot_permission.get("use_permission")
        )
        publication_eligible = bool(
            status == "ok"
            and not needs_human_review
            and str(permission.get("status") or "") == "allowed"
            and use_permission
            in {"factual_support", "contextual_or_qualified_support"}
        )
        publication_reason = (
            "approved_with_sufficient_use_permission"
            if publication_eligible
            else "pending_or_permission_not_sufficient"
        )
    usable = (
        path_exists
        and not is_duplicate
        and not failure_reason
        and review_utility != "exclude"
        and status != "failed"
    )
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "doi": _coerce_str(raw.get("doi")),
        "title": _coerce_str(raw.get("title")),
        "caption": _coerce_str(raw.get("caption")),
        "caption_missing": bool(
            _coerce_bool(raw.get("caption_missing"))
            or _is_placeholder_caption(raw.get("caption"))
            or _is_placeholder_caption(raw.get("caption_original"))
        ),
        "crop_contamination": _coerce_bool(
            raw.get("crop_contamination")
        ),
        "page_prose_contamination": _coerce_bool(
            raw.get("page_prose_contamination")
        )
        or any(
            "page_prose" in str(item).casefold()
            for item in _as_list(raw.get("warnings"))
        ),
        "caption_original": _coerce_str(
            raw.get("caption_original") or raw.get("caption")
        ),
        "caption_preview": _coerce_str(
            raw.get("caption_preview")
            or raw.get("caption")
            or raw.get("search_text")
        ),
        "chunk_kind": kind,
        "search_text": _coerce_str(raw.get("search_text")),
        "labels": _as_list(raw.get("labels") or raw.get("tags")),
        "visual_argument_type": vtype,
        "visual_argument_status": status,
        "visual_argument_confidence": confidence,
        "visual_argument_claim": _coerce_str(
            raw.get("visual_argument_claim")
        ),
        "visual_argument_needs_human_review": _coerce_bool(
            raw.get("visual_argument_needs_human_review")
            or quality.get("needs_human_review")
        ),
        "visual_role": _coerce_str(
            raw.get("visual_role") or raw.get("visual_content_type")
        ),
        "review_utility": review_utility,
        "parent_label": parent_label,
        "figure_label": _coerce_str(
            raw.get("figure_label") or raw.get("label") or parent_label
        ),
        "subfigure_label": subfigure_label,
        "parent_chunk_id": parent_chunk_id,
        "figure_mode": figure_mode,
        "local_image_path": local_image_path,
        "path_exists": path_exists,
        "path_key": _path_key(local_image_path),
        "source_file": _coerce_str(
            raw.get("source_file")
            or (raw.get("source_provenance") or {}).get("source_file")
        ),
        "source_url": _coerce_str(
            raw.get("source_url")
            or (raw.get("source_provenance") or {}).get("source_url")
        ),
        "remote_image_url": _coerce_str(
            raw.get("remote_image_url")
            or (raw.get("local_resources") or {}).get("remote_image_url")
        ),
        "parser": _coerce_str(
            raw.get("parser")
            or (raw.get("source_provenance") or {}).get("parser")
        ),
        "page": raw.get("page")
        if isinstance(raw.get("page"), (int, float))
        else (raw.get("source_provenance") or {}).get("page"),
        "checksum": _coerce_str(
            raw.get("checksum")
            or (raw.get("source_provenance") or {}).get("checksum")
        ),
        "permission": permission,
        "asset_kind": asset_kind,
        "is_table": asset_kind == "table",
        "publication_eligible": publication_eligible,
        "publication_eligible_reason": publication_reason,
        "is_duplicate": is_duplicate,
        "failure_reason": failure_reason,
        "supporting_claim_ids": _as_list(
            raw.get("supporting_claim_ids") or raw.get("claim_ids")
        ),
        "linked_text_chunk_ids": _as_list(
            raw.get("linked_text_chunk_ids")
        ),
        "body_callout_texts": _as_list(raw.get("body_callout_texts")),
        "warnings": _as_list(
            raw.get("warnings") or quality.get("warnings")
        ),
        "usable": usable,
        "source_route": (
            "generated_cache" if is_generated_visual else "local_cache"
        ),
        "generated_visual": is_generated_visual,
        "source_kind": source_kind,
        "source_map": dict(raw.get("source_map") or {})
        if isinstance(raw.get("source_map"), dict)
        else {},
        "generated_identity": (
            "article_owned_generated_visual"
            if is_generated_visual
            else ""
        ),
        "required_disclosure": (
            "AI-generated explanatory visual"
            if is_generated_visual
            else ""
        ),
    }


def build_article_visual_planner_fingerprint(
    *,
    sections: Sequence[Any],
    visual_cache_records: Sequence[Any],
    config: ArticleVisualAssetPlannerConfig,
) -> str:
    """SHA-256 fingerprint of every input that can change a plan decision."""

    normalized_sections = []
    for section in sections:
        try:
            normalized_sections.append(_normalize_section(section, 0))
        except Exception:
            normalized_sections.append({"section_id": "<invalid>"})
    normalized_records = []
    for record in visual_cache_records:
        try:
            normalized_records.append(_normalize_visual_cache_record(record, 0))
        except Exception:
            normalized_records.append({"chunk_id": "<invalid>"})
    payload = {
        "schema_version": "article_visual_planner.input_fingerprint.v1",
        "sections": normalized_sections,
        "visual_cache_records": normalized_records,
        "config": config.as_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _section_query(section: Dict[str, Any]) -> str:
    parts = [
        section.get("title") or "",
        section.get("argument_role") or "",
        section.get("text") or "",
    ]
    for claim in section.get("approved_claims") or []:
        parts.append(claim.get("statement") or "")
    return " ".join(parts)


def _preferred_visual_types(section: Dict[str, Any]) -> Set[str]:
    preferred: Set[str] = set()
    expected = section.get("expected_visual_arguments") or []
    for value in expected:
        if value in VALID_VISUAL_ARGUMENT_TYPES:
            preferred.add(value)
    text = " ".join(
        [section.get("title") or "", section.get("argument_role") or ""]
    ).lower()
    for role, keywords in GENERIC_ROLE_RULES.items():
        if any(str(keyword).lower() in text for keyword in keywords):
            preferred.update(ROLE_TO_PREFERRED_TYPES[role])
    return preferred


def _candidate_text(candidate: Dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in (
            "title",
            "caption",
            "search_text",
            "labels",
            "visual_argument_claim",
            "visual_role",
            "parent_label",
            "subfigure_label",
        )
    )


def _score_candidate(
    section_tokens: Set[str],
    section_role_text: str,
    candidate: Dict[str, Any],
    preferred_types: Set[str],
) -> Tuple[float, str]:
    candidate_tokens = _tokenize(_candidate_text(candidate))
    if not candidate_tokens or not section_tokens:
        return 0.0, "no_tokens"
    overlap = len(section_tokens & candidate_tokens)
    if overlap == 0:
        return 0.0, "no_lexical_overlap"
    score = overlap / len(section_tokens | candidate_tokens)
    reasons: List[str] = ["lexical_overlap"]

    caption_missing = bool(candidate.get("caption_missing"))
    if candidate.get("caption") and not caption_missing:
        score += 0.03
    if caption_missing:
        score -= 0.10
        reasons.append("caption_missing")
    if candidate.get("page_prose_contamination"):
        score -= 0.25
        reasons.append("page_prose_contamination")
    elif candidate.get("crop_contamination"):
        score -= 0.08
        reasons.append("crop_contamination")
    vtype = str(candidate.get("visual_argument_type") or "")
    if vtype in preferred_types:
        score += 0.10
        reasons.append("preferred_type")

    confidence = str(candidate.get("visual_argument_confidence") or "")
    if confidence == "high":
        score += 0.04
    elif confidence == "medium":
        score += 0.02

    permission_status = str(
        (candidate.get("permission") or {}).get("status") or ""
    )
    if permission_status == "allowed":
        score += 0.03
    elif permission_status == "requires_review":
        score -= 0.02

    reviewed = (
        str(candidate.get("visual_argument_status") or "") == "ok"
        and not candidate.get("visual_argument_needs_human_review")
    )
    if reviewed:
        score += 0.03
    else:
        score -= 0.05
        reasons.append("pending_review")
    if candidate.get("review_utility") == "high":
        score += 0.03
    elif candidate.get("review_utility") == "medium":
        score += 0.01
    if candidate.get("figure_mode") == "subfigure":
        score += 0.01

    role_lower = str(section_role_text or "").lower()
    if any(
        keyword in role_lower
        for keyword in ("mechanism", "process", "how", "principle")
    ) and vtype in ("mechanism_anchor", "method_or_workflow"):
        score += 0.08
    if any(
        keyword in role_lower
        for keyword in ("comparison", "benchmark", "performance", "versus")
    ) and vtype in ("quantitative_comparison", "trend_or_parameter_map"):
        score += 0.08
    if any(
        keyword in role_lower
        for keyword in ("overview", "taxonomy", "classification", "landscape")
    ) and vtype in ("taxonomy_or_roadmap", "synthesis_overview"):
        score += 0.08
    if any(
        keyword in role_lower
        for keyword in ("limitation", "challenge", "gap", "anomaly")
    ) and vtype == "anomaly_or_limitation":
        score += 0.08

    return round(score, 4), "; ".join(reasons)


def _retrieve_candidates(
    section: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    config: ArticleVisualAssetPlannerConfig,
) -> List[Dict[str, Any]]:
    query_tokens = _tokenize(_section_query(section))
    preferred_types = _preferred_visual_types(section)
    role_text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
        ]
    )
    approved_ids = set(section.get("approved_claim_ids") or [])
    scored: List[Dict[str, Any]] = []
    for record in records:
        if not record.get("usable"):
            continue
        score, reason = _score_candidate(
            query_tokens,
            role_text,
            record,
            preferred_types,
        )
        if score <= 0:
            continue
        candidate = {**record}
        candidate["score"] = score
        candidate["reason"] = reason
        candidate["approved_claim_overlap"] = bool(
            set(candidate.get("supporting_claim_ids") or []) & approved_ids
        )
        scored.append(candidate)
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return _diverse_shortlist(scored, config)


def _diverse_shortlist(
    scored: Sequence[Dict[str, Any]],
    config: ArticleVisualAssetPlannerConfig,
) -> List[Dict[str, Any]]:
    """Multi-paper diversity for the shortlist; backfill keeps capacity.

    Diversity is a ranking objective, not a deletion rule: at most
    ``shortlist_max_per_paper`` candidates per paper occupy the first pass,
    then the remaining budget is backfilled so no valid candidate is lost.
    """

    selected: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    per_paper: Dict[str, int] = {}
    max_per_paper = max(1, int(config.shortlist_max_per_paper))
    for candidate in scored:
        paper = str(candidate.get("paper_id") or "")
        if per_paper.get(paper, 0) < max_per_paper:
            selected.append(candidate)
            per_paper[paper] = per_paper.get(paper, 0) + 1
        else:
            deferred.append(candidate)
    for candidate in deferred:
        if len(selected) >= config.shortlist_top_k:
            break
        selected.append(candidate)
    return selected[: config.shortlist_top_k]


def _effective_candidate_score(
    candidate: Dict[str, Any],
    state: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> float:
    """Base score plus soft article-level diversity corrections.

    An unseen paper receives a coverage bonus, and every placement from a
    paper applies a reuse penalty that grows quadratically with the number of
    placements from that paper.  Diversity is a ranking objective, never a
    quota: candidates are never dropped, and a clearly superior candidate
    still wins when its score gap exceeds the effective penalty.
    """

    base = float(candidate.get("score") or 0.0)
    paper = str(candidate.get("paper_id") or "")
    used = int((state.get("used_papers") or {}).get(paper, 0))
    strength = float(config.paper_diversity_penalty)
    unseen_bonus = strength if used == 0 else 0.0
    reuse_penalty = strength * (used * used)
    return round(
        base + unseen_bonus - reuse_penalty,
        4,
    )


def _is_weak_filler(
    candidate: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> bool:
    if float(candidate.get("score") or 0.0) < config.min_relevance_score:
        return True
    if (
        str(candidate.get("review_utility") or "") == "low"
        and not candidate.get("approved_claim_overlap")
    ):
        return True
    if (
        str(candidate.get("visual_argument_confidence") or "") == "low"
        and float(candidate.get("score") or 0.0) < 0.12
    ):
        return True
    return False


def _paragraphs(text: str) -> List[str]:
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", str(text or ""))
        if paragraph.strip()
    ]


def _placement_anchor(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
) -> str:
    paragraphs = _paragraphs(section.get("text") or "")
    if not paragraphs:
        return "end_of_section"
    candidate_tokens = _tokenize(_candidate_text(candidate))
    best_index = -1
    best_overlap = 0
    for index, paragraph in enumerate(paragraphs):
        paragraph_tokens = _tokenize(paragraph)
        overlap = len(paragraph_tokens & candidate_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_index = index
    if best_index >= 0 and best_overlap > 0:
        return f"after_paragraph_{best_index + 1}"
    return "end_of_section"


def _bind_claims(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
) -> List[Dict[str, Any]]:
    approved = section.get("approved_claims") or []
    if not approved:
        return []
    supporting_ids = set(candidate.get("supporting_claim_ids") or [])
    bindings: List[Dict[str, Any]] = []
    for claim in approved:
        claim_id = str(claim.get("claim_id") or "")
        if claim_id in supporting_ids:
            bindings.append(
                {"claim_id": claim_id, "binding_type": "direct"}
            )
            continue
    candidate_tokens = _tokenize(_candidate_text(candidate))
    existing = {row["claim_id"] for row in bindings}
    for claim in approved:
        claim_id = str(claim.get("claim_id") or "")
        if claim_id in existing:
            continue
        claim_tokens = _tokenize(claim.get("statement") or "")
        if len(claim_tokens & candidate_tokens) >= 2:
            bindings.append(
                {"claim_id": claim_id, "binding_type": "contextual"}
            )
            existing.add(claim_id)
        if len(bindings) >= 3:
            break
    return bindings


def _section_binding(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "section_id": section.get("section_id", ""),
        "binding_strength": (
            "direct"
            if float(candidate.get("score") or 0.0) >= 0.12
            else "contextual"
        ),
    }


def _provenance(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chunk_id": candidate.get("chunk_id", ""),
        "paper_id": candidate.get("paper_id", ""),
        "doi": candidate.get("doi", ""),
        "local_image_path": candidate.get("local_image_path", ""),
        "remote_image_url": candidate.get("remote_image_url", ""),
        "source_file": candidate.get("source_file", ""),
        "source_url": candidate.get("source_url", ""),
        "parser": candidate.get("parser", ""),
        "page": candidate.get("page"),
        "checksum": candidate.get("checksum", ""),
        "source_route": candidate.get("source_route", "local_cache"),
        "figure_label": candidate.get("figure_label", ""),
        "subfigure_label": candidate.get("subfigure_label", ""),
        "asset_kind": candidate.get("asset_kind", ""),
    }


def _source_map_for_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    existing = candidate.get("source_map")
    if isinstance(existing, dict) and not validate_visual_source_map(existing):
        return dict(existing)
    return build_visual_source_map(
        unit_id=str(candidate.get("chunk_id") or ""),
        source_identity={
            "paper_id": candidate.get("paper_id", ""),
            "doi": candidate.get("doi", ""),
            "title": candidate.get("title", ""),
        },
        figure_identity={
            "asset_id": candidate.get("chunk_id", ""),
            "asset_kind": candidate.get("asset_kind", "")
            or ("diagram" if candidate.get("generated_visual") else "figure"),
            "figure_label": candidate.get("figure_label", ""),
            "subfigure_label": candidate.get("subfigure_label", ""),
            "parent_label": candidate.get("parent_label", ""),
            "page": candidate.get("page"),
        },
        caption={
            "original": candidate.get("caption_original", ""),
            "clean": candidate.get("caption", ""),
            "confidence": candidate.get(
                "visual_argument_confidence", "medium"
            ),
        },
        semantic={
            "nearby_text": candidate.get("search_text", ""),
            "linked_text_chunk_ids": candidate.get(
                "linked_text_chunk_ids", []
            ),
            "body_callout_texts": candidate.get(
                "body_callout_texts", []
            ),
        },
        provenance={
            "page": candidate.get("page"),
            "source_url": candidate.get("source_url", ""),
            "source_file": candidate.get("source_file", ""),
        },
        paths={
            "image_ref": {
                "root": "resolved",
                "relative": candidate.get("local_image_path", ""),
            }
        },
    )


def _source_attribution(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "paper_id": str(candidate.get("paper_id") or ""),
        "doi": str(candidate.get("doi") or ""),
        "figure_label": str(candidate.get("figure_label") or ""),
        "subfigure_label": str(candidate.get("subfigure_label") or ""),
        "source_url": str(candidate.get("source_url") or ""),
    }


def _source_caption(candidate: Dict[str, Any]) -> str:
    """Original source caption when present, otherwise the clean caption."""

    return str(
        candidate.get("caption_original")
        or candidate.get("caption")
        or ""
    )


def _caption_proposal(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    composite: bool = False,
    separate_attribution: bool = True,
) -> str:
    purpose = _coerce_str(candidate.get("visual_argument_claim"))
    if not purpose:
        purpose = _coerce_str(candidate.get("caption"))
    if not purpose:
        purpose = f"Source visual supporting {section.get('section_id', '')}."
    prefix = "Combined coherent subfigure panels. " if composite else ""
    paper = (
        ""
        if candidate.get("generated_visual")
        else str(candidate.get("paper_id") or "").strip()
    )
    if separate_attribution or not paper:
        return f"{prefix}{purpose}"
    return f"{prefix}{purpose} Source: {paper}."


def _permission_requires_review(candidate: Dict[str, Any]) -> bool:
    return (
        str((candidate.get("permission") or {}).get("status") or "")
        == "requires_review"
    )


def _review_state_for_candidate(candidate: Dict[str, Any]) -> Tuple[str, bool, str]:
    if (
        str(candidate.get("visual_argument_status") or "") == "ok"
        and not candidate.get("visual_argument_needs_human_review")
        and not _permission_requires_review(candidate)
    ):
        return "verified_existing", False, "previously_verified_by_vision_model"
    return (
        "traceable_source_pending_review",
        True,
        "final_candidate_requires_image_and_permission_review",
    )


def _factory_placement(
    section_id: str,
    candidate: Dict[str, Any],
    *,
    status: str,
    composite_group_id: str = "",
    panel_role: str = "",
    purpose: str = "",
    anchor: str = "",
    generated: bool = False,
) -> Dict[str, Any]:
    asset_kind = _coerce_str(candidate.get("asset_kind")) or (
        "diagram" if generated else "figure"
    )
    if asset_kind not in PLANNER_ASSET_KINDS:
        asset_kind = "diagram" if generated else "figure"
    return {
        "section_id": section_id,
        "visual_chunk_id": str(candidate.get("chunk_id") or ""),
        "paper_id": str(candidate.get("paper_id") or ""),
        "doi": str(candidate.get("doi") or ""),
        "local_image_path": str(candidate.get("local_image_path") or ""),
        "visual_argument_type": str(
            candidate.get("visual_argument_type") or ""
        ),
        "caption_preview": str(candidate.get("caption_preview") or ""),
        "argumentative_purpose": purpose,
        "placement_guidance": anchor,
        "composite_group_id": composite_group_id,
        "panel_role": panel_role,
        "priority": "high"
        if float(candidate.get("score") or 0.0) >= 0.18
        else "medium",
        "figure_kind": (
            "generated_explanatory_visual"
            if generated
            else ("source_table" if asset_kind == "table" else "source_figure")
        ),
        "asset_kind": asset_kind,
        "is_table": asset_kind == "table",
        "publication_eligible": bool(
            candidate.get("publication_eligible")
        ),
        "generated_visual": generated,
        "status": status,
    }


def _composite_group_id(candidate: Dict[str, Any]) -> str:
    base = str(
        candidate.get("parent_chunk_id") or candidate.get("chunk_id")
        or "composite"
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-")[:60]
    return f"COMP-{slug or 'composite'}"


def _coherent_subfigure_siblings(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
    shortlist: Sequence[Dict[str, Any]],
    state: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> List[Dict[str, Any]]:
    if not config.allow_subfigure_composites:
        return []
    section_text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
            section.get("text") or "",
            " ".join(
                str(value)
                for value in section.get("expected_visual_arguments") or []
            ),
        ]
    ).lower()
    prefers_composite = any(
        keyword in section_text
        for keyword in (
            "overview",
            "compare",
            "comparison",
            "combined",
            "together",
            "mechanism",
            "process",
            "workflow",
            "architecture",
        )
    ) or bool(
        set(section.get("expected_visual_arguments") or [])
        & {
            "mechanism_anchor",
            "method_or_workflow",
            "synthesis_overview",
            "taxonomy_or_roadmap",
        }
    )
    if not prefers_composite:
        return []
    parent_id = str(candidate.get("parent_chunk_id") or "")
    if not parent_id:
        return []
    members: List[Dict[str, Any]] = []
    for item in shortlist:
        if str(item.get("parent_chunk_id") or "") != parent_id:
            continue
        if item.get("figure_mode") != "subfigure":
            continue
        if (
            str((item.get("permission") or {}).get("status") or "")
            == "restricted"
        ):
            continue
        if _candidate_blocked(item, state):
            continue
        if _is_weak_filler(item, config):
            continue
        if item.get("path_key") in state["used_paths"]:
            continue
        members.append(item)
    members.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    if len(members) < 2:
        return []
    return members[: config.max_subfigure_composite_panels]


def _candidate_blocked(
    candidate: Dict[str, Any],
    state: Dict[str, Any],
) -> bool:
    if candidate.get("chunk_id") in state["used_chunk_ids"]:
        return True
    if candidate.get("path_key") in state["used_paths"]:
        return True
    lineage_key = str(
        candidate.get("parent_chunk_id") or candidate.get("chunk_id") or ""
    )
    if lineage_key in state["used_lineages"]:
        return True
    return False


def _mark_consumed(
    candidates: Sequence[Dict[str, Any]],
    state: Dict[str, Any],
) -> None:
    for candidate in candidates:
        state["used_chunk_ids"].add(str(candidate.get("chunk_id") or ""))
        state["used_paths"].add(str(candidate.get("path_key") or ""))
        state["used_lineages"].add(
            str(
                candidate.get("parent_chunk_id")
                or candidate.get("chunk_id")
                or ""
            )
        )


def _build_single_placement(
    section: Dict[str, Any],
    candidate: Dict[str, Any],
    state: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> Dict[str, Any]:
    del state
    section_id = str(section.get("section_id") or "")
    status, image_review_required, review_reason = (
        _review_state_for_candidate(candidate)
    )
    purpose = _caption_proposal(
        section,
        candidate,
        separate_attribution=config.separate_caption_attribution,
    )
    anchor = _placement_anchor(section, candidate)
    bindings = _bind_claims(section, candidate)
    source_map = (
        _source_map_for_candidate(candidate)
        if config.emit_source_maps
        else {}
    )
    attribution = _source_attribution(candidate)
    generated_visual = bool(candidate.get("generated_visual"))
    asset_kind = _coerce_str(candidate.get("asset_kind")) or (
        "diagram" if generated_visual else "figure"
    )
    if asset_kind not in PLANNER_ASSET_KINDS:
        asset_kind = "diagram" if generated_visual else "figure"
    transformation = {
        "required": False,
        "kind": "none"
        if candidate.get("figure_mode") == "whole_figure"
        else "reuse_cached_subfigure_crop",
        "note": (
            "Use the source image as-is."
            if candidate.get("figure_mode") == "whole_figure"
            else "Reuse the cached subfigure crop and verify the panel label."
        ),
    }
    placement = {
        "placement_id": "",
        "section_id": section_id,
        "section_title": section.get("title", ""),
        "visual_chunk_id": str(candidate.get("chunk_id") or ""),
        "paper_id": str(candidate.get("paper_id") or ""),
        "doi": str(candidate.get("doi") or ""),
        "local_image_path": str(candidate.get("local_image_path") or ""),
        "caption_preview": str(candidate.get("caption_preview") or ""),
        "caption_proposal": purpose,
        "source_caption": _source_caption(candidate),
        "source_attribution": attribution,
        "argumentative_purpose": purpose,
        "argumentative_role": str(
            candidate.get("visual_argument_type") or "reader_explanation"
        ),
        "claim_binding": bindings,
        "source_map": source_map,
        "section_binding": _section_binding(section, candidate),
        "placement_anchor": anchor,
        "placement_guidance": anchor,
        "figure_mode": candidate.get("figure_mode", "whole_figure"),
        "lineage": {
            "parent_chunk_id": candidate.get("parent_chunk_id", ""),
            "parent_label": candidate.get("parent_label", ""),
            "subfigure_label": candidate.get("subfigure_label", ""),
            "chunk_kind": candidate.get("chunk_kind", ""),
            "composite": False,
        },
        "panel_manifest": [
            {
                "panel_id": "a",
                "visual_chunk_id": candidate.get("chunk_id", ""),
                "paper_id": candidate.get("paper_id", ""),
                "local_image_path": candidate.get("local_image_path", ""),
                "subfigure_label": candidate.get("subfigure_label", ""),
            }
        ],
        "composite_group_id": "",
        "panel_role": "",
        "provenance": _provenance(candidate),
        "permission": dict(candidate.get("permission") or {}),
        "transformation_need": transformation,
        "priority": "high"
        if float(candidate.get("score") or 0.0) >= 0.18
        else "medium",
        "figure_kind": (
            "generated_explanatory_visual"
            if generated_visual
            else ("source_table" if asset_kind == "table" else "source_figure")
        ),
        "asset_kind": asset_kind,
        "is_table": asset_kind == "table",
        "publication_eligible": bool(
            candidate.get("publication_eligible")
        ),
        "publication_eligible_reason": _coerce_str(
            candidate.get("publication_eligible_reason")
        ),
        "generated_visual": generated_visual,
        "generated_identity": candidate.get("generated_identity", ""),
        "source_kind": candidate.get("source_kind", ""),
        "required_disclosure": candidate.get("required_disclosure", ""),
        "status": status,
        "image_review_required": image_review_required,
        "review_state": status,
        "review_required_reason": review_reason,
        "retrieval": {
            "score": candidate.get("score", 0.0),
            "reason": candidate.get("reason", ""),
            "shortlist_first": True,
            "image_inspection_stage": (
                "final_candidate_review"
                if image_review_required
                else "already_verified"
            ),
        },
        "factory_compatible_source_request": _factory_placement(
            section_id,
            candidate,
            status=status,
            purpose=purpose,
            anchor=anchor,
            generated=generated_visual,
        ),
    }
    if config.emit_figure_contracts:
        figure_contract = build_figure_contract(
            contract_id=f"FC-{section_id}-{candidate.get('chunk_id', '')}",
            section_id=section_id,
            figure_kind=placement["figure_kind"],
            asset_kind=asset_kind,
            argumentative_purpose=purpose,
            claim_bindings=bindings,
            panel_manifest=placement["panel_manifest"],
            source_map=source_map,
            source_caption=placement["source_caption"],
            editorial_caption=placement["caption_proposal"],
            attribution=attribution,
            permission=placement["permission"],
            evidence_ceiling=str(
                placement["permission"].get("evidence_ceiling") or ""
            ),
            transformation=transformation,
            review_state=status,
            required_disclosure=placement["required_disclosure"],
        )
        placement["figure_contract"] = figure_contract
        placement["factory_compatible_source_request"].update(
            {
                "source_caption": placement["source_caption"],
                "source_attribution": attribution,
                "source_map": source_map,
                "figure_contract": figure_contract,
            }
        )
    return placement


def _build_composite_placement(
    section: Dict[str, Any],
    members: Sequence[Dict[str, Any]],
    candidate: Dict[str, Any],
    state: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> Dict[str, Any]:
    del state
    section_id = str(section.get("section_id") or "")
    status, image_review_required, review_reason = (
        _review_state_for_candidate(candidate)
    )
    for member in members:
        member_status, member_review_required, _ = (
            _review_state_for_candidate(member)
        )
        if member_review_required:
            status = "traceable_source_pending_review"
            image_review_required = True
            review_reason = member_status
    purpose = _caption_proposal(
        section,
        candidate,
        composite=True,
        separate_attribution=config.separate_caption_attribution,
    )
    anchor = _placement_anchor(section, candidate)
    bindings = _bind_claims(section, candidate)
    source_map = (
        _source_map_for_candidate(candidate)
        if config.emit_source_maps
        else {}
    )
    attribution = _source_attribution(candidate)
    group_id = _composite_group_id(candidate)
    member_kinds = [
        _coerce_str(member.get("asset_kind")) or "figure"
        for member in members
    ]
    composite_asset_kind = (
        "table"
        if member_kinds and all(kind == "table" for kind in member_kinds)
        else "figure"
    )
    composite_publication_eligible = all(
        bool(member.get("publication_eligible")) for member in members
    )
    panel_manifest: List[Dict[str, Any]] = []
    factory_placements: List[Dict[str, Any]] = []
    for index, member in enumerate(members):
        panel_id = chr(ord("a") + index)
        panel_role = f"panel_{panel_id}"
        panel_manifest.append(
            {
                "panel_id": panel_id,
                "panel_role": panel_role,
                "visual_chunk_id": member.get("chunk_id", ""),
                "paper_id": member.get("paper_id", ""),
                "doi": member.get("doi", ""),
                "local_image_path": member.get("local_image_path", ""),
                "subfigure_label": member.get("subfigure_label", ""),
                "score": member.get("score", 0.0),
                "permission": dict(member.get("permission") or {}),
                "provenance": _provenance(member),
                "source_caption": _source_caption(member),
                "source_attribution": _source_attribution(member),
                "source_map": (
                    _source_map_for_candidate(member)
                    if config.emit_source_maps
                    else {}
                ),
            }
        )
        factory_placements.append(
            _factory_placement(
                section_id,
                member,
                status=status,
                composite_group_id=group_id,
                panel_role=panel_role,
                purpose=purpose,
                anchor=anchor,
            )
        )
    placement = {
        "placement_id": "",
        "section_id": section_id,
        "section_title": section.get("title", ""),
        "visual_chunk_id": str(candidate.get("chunk_id") or ""),
        "paper_id": str(candidate.get("paper_id") or ""),
        "doi": str(candidate.get("doi") or ""),
        "local_image_path": str(candidate.get("local_image_path") or ""),
        "caption_preview": str(candidate.get("caption_preview") or ""),
        "caption_proposal": purpose,
        "source_caption": _source_caption(candidate),
        "source_attribution": attribution,
        "argumentative_purpose": purpose,
        "argumentative_role": str(
            candidate.get("visual_argument_type")
            or "coherent_subfigure_composite"
        ),
        "claim_binding": bindings,
        "source_map": source_map,
        "section_binding": _section_binding(section, candidate),
        "placement_anchor": anchor,
        "placement_guidance": anchor,
        "figure_mode": "subfigure",
        "lineage": {
            "parent_chunk_id": candidate.get("parent_chunk_id", ""),
            "parent_label": candidate.get("parent_label", ""),
            "subfigure_label": "",
            "chunk_kind": candidate.get("chunk_kind", ""),
            "composite": True,
        },
        "panel_manifest": panel_manifest,
        "composite_group_id": group_id,
        "panel_role": "composite",
        "provenance": _provenance(candidate),
        "permission": dict(candidate.get("permission") or {}),
        "transformation_need": {
            "required": True,
            "kind": "compose_subfigures",
            "note": (
                f"Compose {len(members)} coherent subfigure panels into "
                "one composite figure."
            ),
        },
        "priority": "high"
        if float(candidate.get("score") or 0.0) >= 0.18
        else "medium",
        "figure_kind": (
            "source_table"
            if composite_asset_kind == "table"
            else "source_composite"
        ),
        "asset_kind": composite_asset_kind,
        "is_table": composite_asset_kind == "table",
        "publication_eligible": composite_publication_eligible,
        "publication_eligible_reason": (
            "all_composite_members_publication_eligible"
            if composite_publication_eligible
            else "composite_contains_pending_or_not_publication_eligible_member"
        ),
        "status": status,
        "image_review_required": image_review_required,
        "review_state": status,
        "review_required_reason": review_reason,
        "retrieval": {
            "score": candidate.get("score", 0.0),
            "reason": candidate.get("reason", ""),
            "shortlist_first": True,
            "image_inspection_stage": (
                "final_candidate_review"
                if image_review_required
                else "already_verified"
            ),
        },
        "factory_compatible_placements": factory_placements,
        "factory_compatible_source_request": factory_placements[0],
    }
    if config.emit_figure_contracts:
        figure_contract = build_figure_contract(
            contract_id=f"FC-{section_id}-{group_id}",
            section_id=section_id,
            figure_kind=(
                "source_table"
                if composite_asset_kind == "table"
                else "source_composite"
            ),
            asset_kind=composite_asset_kind,
            argumentative_purpose=purpose,
            claim_bindings=bindings,
            panel_manifest=panel_manifest,
            source_map=source_map,
            source_caption=placement["source_caption"],
            editorial_caption=placement["caption_proposal"],
            attribution=attribution,
            permission=placement["permission"],
            evidence_ceiling=str(
                placement["permission"].get("evidence_ceiling") or ""
            ),
            transformation=placement["transformation_need"],
            review_state=status,
        )
        placement["figure_contract"] = figure_contract
        for member, factory_placement in zip(members, factory_placements):
            factory_placement.update(
                {
                    "source_caption": _source_caption(member),
                    "source_attribution": _source_attribution(member),
                    "source_map": (
                        _source_map_for_candidate(member)
                        if config.emit_source_maps
                        else {}
                    ),
                    "figure_contract": figure_contract,
                }
            )
    return placement


def _section_has_conceptual_need(section: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
            section.get("text") or "",
        ]
    ).lower()
    if any(signal in text for signal in CONCEPTUAL_SIGNALS):
        return True
    expected = set(section.get("expected_visual_arguments") or [])
    return bool(
        expected
        & {
            "mechanism_anchor",
            "method_or_workflow",
            "taxonomy_or_roadmap",
            "synthesis_overview",
        }
    )


def _section_has_empirical_only_need(section: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
            section.get("text") or "",
            " ".join(
                claim.get("statement") or ""
                for claim in section.get("claims") or []
            ),
        ]
    ).lower()
    has_empirical = any(signal in text for signal in EMPIRICAL_SIGNALS)
    has_conceptual = any(signal in text for signal in CONCEPTUAL_SIGNALS)
    return bool(has_empirical and not has_conceptual)


def _generation_safety(
    section: Dict[str, Any],
    figure_kind: str,
) -> Dict[str, Any]:
    text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
            section.get("text") or "",
            " ".join(
                claim.get("statement") or ""
                for claim in section.get("claims") or []
            ),
        ]
    ).lower()
    expected = set(section.get("expected_visual_arguments") or [])
    empirical_kinds = {
        "data_infographic",
        "trend_schematic",
        "comparison_diagram",
        "spectrum",
        "micrograph",
        "measurement_plot",
        "simulation_plot",
    }
    if figure_kind in empirical_kinds:
        return {
            "eligible": False,
            "reason": "empirical_or_quantitative_generation_forbidden",
            "policy": (
                "Curves, microscopy, spectra, measurements and simulation "
                "results may not be generated."
            ),
        }
    if figure_kind not in ALLOWED_CONCEPTUAL_FIGURE_KINDS:
        return {
            "eligible": False,
            "reason": "figure_kind_not_generatable",
            "policy": "Only non-empirical explanatory visuals can be generated.",
        }
    expected_empirical = expected & {
        "quantitative_comparison",
        "trend_or_parameter_map",
        "representative_example",
    }
    has_empirical = any(signal in text for signal in EMPIRICAL_SIGNALS)
    has_conceptual = any(signal in text for signal in CONCEPTUAL_SIGNALS)
    if expected_empirical and not has_conceptual:
        return {
            "eligible": False,
            "reason": "expected_role_is_empirical",
            "policy": "Empirical/quantitative roles cannot be generated.",
        }
    if has_empirical and not has_conceptual:
        return {
            "eligible": False,
            "reason": "section_need_is_empirical",
            "policy": (
                "The section asks for measured/simulated content, which "
                "cannot be generated."
            ),
        }
    return {
        "eligible": True,
        "reason": "non_empirical_explanatory_visual",
        "policy": (
            "Only conceptual relationships are drawn; no measurements or "
            "simulation output are invented."
        ),
    }


def _concept_phrase(section: Dict[str, Any]) -> str:
    phrase = _coerce_str(
        section.get("argument_role") or section.get("title")
    )
    phrase = re.sub(r"\s+", " ", phrase).rstrip(".")
    if not phrase:
        return "the section's core scientific concept"
    return phrase[:180]


def _generation_brief(
    section: Dict[str, Any],
    figure_kind: str,
) -> str:
    kind_label = figure_kind.replace("_", " ")
    return (
        f"Create a clean scientific {kind_label} for "
        f"{_concept_phrase(section)}. Show relationships, structure and "
        "labels only. Do not plot curves, spectra, microscopy, measurements "
        "or simulation results, and do not invent any quantitative data."
    )


def _conceptual_anchor(section: Dict[str, Any]) -> str:
    paragraphs = _paragraphs(section.get("text") or "")
    if not paragraphs:
        return "end_of_section"
    best_index = -1
    best_hits = 0
    for index, paragraph in enumerate(paragraphs):
        lower = paragraph.lower()
        hits = sum(
            1 for signal in CONCEPTUAL_SIGNALS if signal in lower
        )
        if hits > best_hits:
            best_hits = hits
            best_index = index
    if best_index >= 0 and best_hits > 0:
        return f"after_paragraph_{best_index + 1}"
    return "end_of_section"


def _section_matches_coverage(
    section: Dict[str, Any],
    requirement: str,
) -> bool:
    text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
            " ".join(
                str(value)
                for value in section.get("expected_visual_arguments") or []
            ),
        ]
    ).lower()
    if requirement == "mechanism_or_conceptual_diagram":
        return any(
            signal in text
            for signal in (
                "mechanism",
                "principle",
                "concept",
                "taxonomy",
                "overview",
                "architecture",
            )
        )
    if requirement == "workflow_or_process_diagram":
        return any(
            signal in text
            for signal in (
                "workflow",
                "process",
                "fabrication",
                "pipeline",
                "method",
                "setup",
            )
        )
    return False


def _request_kind_for_requirement(requirement: str) -> str:
    if requirement == "mechanism_or_conceptual_diagram":
        return "mechanism_schematic"
    if requirement == "workflow_or_process_diagram":
        return "workflow_schematic"
    return "concept_map"


def _role_based_request_kind(section: Dict[str, Any]) -> str:
    text = " ".join(
        [
            section.get("title") or "",
            section.get("argument_role") or "",
        ]
    ).lower()
    if any(
        signal in text
        for signal in (
            "workflow",
            "process",
            "fabrication",
            "pipeline",
            "method",
            "setup",
        )
    ):
        return "workflow_schematic"
    if any(
        signal in text
        for signal in (
            "mechanism",
            "principle",
            "physical",
            "explain",
            "how",
        )
    ):
        return "mechanism_schematic"
    if any(
        signal in text
        for signal in (
            "taxonomy",
            "roadmap",
            "landscape",
            "overview",
            "classification",
        )
    ):
        return "taxonomy_diagram"
    return "concept_map"


def _build_generation_request(
    section: Dict[str, Any],
    figure_kind: str,
    *,
    coverage_requirement: str = "",
    priority: str = "medium",
) -> Optional[Dict[str, Any]]:
    safety = _generation_safety(section, figure_kind)
    if not safety["eligible"]:
        return None
    section_id = str(section.get("section_id") or "")
    purpose = (
        f"Explain the {figure_kind.replace('_', ' ')} of "
        f"{_concept_phrase(section)} without presenting any measured or "
        "simulated results."
    )
    brief = _generation_brief(section, figure_kind)
    anchor = _conceptual_anchor(section)
    approved = section.get("approved_claims") or []
    concept_tokens = _tokenize(_concept_phrase(section))
    claim_binding: List[Dict[str, Any]] = []
    for claim in approved:
        claim_tokens = _tokenize(claim.get("statement") or "")
        statement_lower = str(claim.get("statement") or "").lower()
        if (
            claim_tokens & concept_tokens
            or any(signal in statement_lower for signal in CONCEPTUAL_SIGNALS)
        ):
            claim_binding.append(
                {
                    "claim_id": str(claim.get("claim_id") or ""),
                    "binding_type": "contextual",
                }
            )
        if len(claim_binding) >= 3:
            break
    request = {
        "request_id": "",
        "section_id": section_id,
        "section_title": section.get("title", ""),
        "figure_kind": figure_kind,
        "asset_kind": "diagram",
        "is_table": False,
        "argumentative_role": "explanatory_concept",
        "argumentative_purpose": purpose,
        "claim_binding": claim_binding,
        "section_binding": {
            "section_id": section_id,
            "binding_strength": "direct",
        },
        "placement_anchor": anchor,
        "placement_guidance": anchor,
        "caption_proposal": f"AI-generated explanatory visual: {purpose}",
        "generation_brief": brief,
        "data_provenance_level": "schematic",
        "approximate_data_allowed": True,
        "input_data": {},
        "priority": priority,
        "required_disclosure": "AI-generated explanatory visual",
        "ai_disclosure": {
            "required": True,
            "label": "AI-generated explanatory visual",
        },
        "empirical_content": False,
        "generation_safety": safety,
        "coverage_requirement": coverage_requirement,
        "status": "pending_generation_and_review",
        "image_review_required": True,
        "review_state": "generated_visual_review_required",
        "factory_compatible_request": {
            "section_id": section_id,
            "figure_kind": figure_kind,
            "asset_kind": "diagram",
            "argumentative_purpose": purpose,
            "generation_brief": brief,
            "placement_guidance": anchor,
            "data_provenance_level": "schematic",
            "input_data": {},
            "approximate_data_allowed": True,
            "priority": priority,
            "required_disclosure": "AI-generated explanatory visual",
            "status": "pending_generation_and_review",
        },
    }
    return request


def _plan_section(
    section: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    state: Dict[str, Any],
    config: ArticleVisualAssetPlannerConfig,
) -> Tuple[
    Dict[str, Any],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[str],
]:
    section_id = str(section.get("section_id") or "")
    errors: List[str] = []
    placements: List[Dict[str, Any]] = []
    unfilled: List[Dict[str, Any]] = []
    shortlist = _retrieve_candidates(section, records, config)
    # Record restricted-permission candidates even if they rank below the
    # placement capacity; their exclusion must remain auditable.
    for candidate in shortlist:
        if (
            str((candidate.get("permission") or {}).get("status") or "")
            == "restricted"
        ):
            unfilled.append(
                {
                    "need_id": "",
                    "section_id": section_id,
                    "section_title": section.get("title", ""),
                    "need_kind": "section_visual_opportunity",
                    "argumentative_purpose": (
                        "Source visual was excluded because its permission "
                        "state is restricted."
                    ),
                    "reason": "permission_restricted_candidate_excluded",
                    "status": "unfilled_requires_editorial_decision",
                    "never_blocks_prose": True,
                }
            )
            break
    fallback_candidate: Optional[Dict[str, Any]] = None
    working = list(shortlist)
    while working and len(placements) < config.max_placements_per_section:
        if config.paper_diversity_penalty > 0:
            working.sort(
                key=lambda candidate: (
                    -_effective_candidate_score(candidate, state, config),
                    candidate["chunk_id"],
                )
            )
        candidate = working.pop(0)
        if _candidate_blocked(candidate, state):
            continue
        if (
            str((candidate.get("permission") or {}).get("status") or "")
            == "restricted"
        ):
            continue
        if _is_weak_filler(candidate, config):
            # Fail-open boundary: when the section has no placement yet, keep
            # the best lexically relevant candidate that was weak-filtered
            # (missing-caption/pending/low-utility) so traceable assets are
            # never silently lost.  It becomes an explicit
            # pending/review-required placement.
            if fallback_candidate is None and not placements:
                fallback_candidate = candidate
            continue
        try:
            if candidate.get("figure_mode") == "subfigure":
                siblings = _coherent_subfigure_siblings(
                    section,
                    candidate,
                    shortlist,
                    state,
                    config,
                )
                if siblings:
                    placement = _build_composite_placement(
                        section,
                        siblings,
                        candidate,
                        state,
                        config,
                    )
                    consumed = siblings
                else:
                    placement = _build_single_placement(
                        section,
                        candidate,
                        state,
                        config,
                    )
                    consumed = [candidate]
            else:
                placement = _build_single_placement(
                    section,
                    candidate,
                    state,
                    config,
                )
                consumed = [candidate]
        except Exception as exc:
            errors.append(
                f"placement_selection_failed:{type(exc).__name__}:{exc}"
            )
            continue
        placement["placement_id"] = f"PL-{section_id}-{len(placements) + 1:02d}"
        placements.append(placement)
        _mark_consumed(consumed, state)
        state["used_papers"][str(candidate.get("paper_id") or "")] += 1
    if (
        not placements
        and fallback_candidate is not None
        and config.max_placements_per_section >= 1
    ):
        try:
            placement = _build_single_placement(
                section,
                fallback_candidate,
                state,
                config,
            )
        except Exception as exc:
            errors.append(
                f"placement_selection_failed:{type(exc).__name__}:{exc}"
            )
        else:
            placement["placement_id"] = (
                f"PL-{section_id}-{len(placements) + 1:02d}"
            )
            placement["status"] = "traceable_source_pending_review"
            placement["review_state"] = "traceable_source_pending_review"
            placement["image_review_required"] = True
            placement["review_required_reason"] = (
                "fail_open_weak_or_pending_only_relevant_candidate"
            )
            factory_request = placement.get(
                "factory_compatible_source_request"
            )
            if isinstance(factory_request, dict):
                factory_request["status"] = placement["status"]
            placements.append(placement)
            _mark_consumed([fallback_candidate], state)
            state["used_papers"][
                str(fallback_candidate.get("paper_id") or "")
            ] += 1
    return (
        {
            "section_id": section_id,
            "title": section.get("title", ""),
            "text_present": bool(section.get("text")),
            "word_count": len(str(section.get("text") or "").split()),
            "argument_role": section.get("argument_role", ""),
            "approved_claim_count": len(section.get("approved_claims") or []),
            "placements": placements,
            "generation_requests": [],
            "unfilled_needs": unfilled,
            "placement_count": len(placements),
            "request_count": 0,
            "item_count": len(placements),
            "capacity": {
                "max_placements_per_section": (
                    config.max_placements_per_section
                ),
                "max_total_visual_items_per_section": (
                    config.max_total_visual_items_per_section
                ),
            },
            "rationale": (
                f"Selected {len(placements)} source placement(s); "
                f"{len(unfilled)} unfilled section need(s) recorded."
            ),
        },
        placements,
        unfilled,
        errors,
    )


def _coverage_audit(
    placements: Sequence[Dict[str, Any]],
    requests: Sequence[Dict[str, Any]],
    config: ArticleVisualAssetPlannerConfig,
) -> Dict[str, Any]:
    satisfied: List[str] = []
    for placement in placements:
        vtype = str(placement.get("argumentative_role") or "")
        if vtype in {
            "mechanism_anchor",
            "taxonomy_or_roadmap",
            "synthesis_overview",
        }:
            satisfied.append("mechanism_or_conceptual_diagram")
        if vtype == "method_or_workflow":
            satisfied.append("workflow_or_process_diagram")
    for request in requests:
        kind = str(request.get("figure_kind") or "")
        if kind in {
            "mechanism_schematic",
            "concept_map",
            "taxonomy_diagram",
        }:
            satisfied.append("mechanism_or_conceptual_diagram")
        if kind == "workflow_schematic":
            satisfied.append("workflow_or_process_diagram")
    satisfied = list(dict.fromkeys(satisfied))
    unsatisfied = [
        requirement
        for requirement in config.coverage_requirements
        if requirement not in satisfied
    ]
    return {
        "required": list(config.coverage_requirements),
        "satisfied": satisfied,
        "unsatisfied": unsatisfied,
        "status": "satisfied" if not unsatisfied else "degraded",
        "policy": (
            "Missing visuals are recorded as unfilled needs and never "
            "block otherwise sound prose."
        ),
    }


def _build_image_review_queue(
    placements: Sequence[Dict[str, Any]],
    requests: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for placement in placements:
        panels = placement.get("panel_manifest") or []
        for panel in panels:
            items.append(
                {
                    "visual_chunk_id": str(
                        panel.get("visual_chunk_id") or ""
                    ),
                    "paper_id": str(panel.get("paper_id") or ""),
                    "local_image_path": str(
                        panel.get("local_image_path") or ""
                    ),
                    "section_id": placement.get("section_id", ""),
                    "review_required": bool(
                        placement.get("image_review_required")
                    ),
                    "review_state": placement.get("review_state", ""),
                    "reason": placement.get("review_required_reason", ""),
                }
            )
    for request in requests:
        items.append(
            {
                "visual_chunk_id": str(request.get("request_id") or ""),
                "paper_id": "",
                "local_image_path": "",
                "section_id": request.get("section_id", ""),
                "review_required": True,
                "review_state": "generated_visual_review_required",
                "reason": (
                    "Generated explanatory visual must be image-reviewed "
                    "before publication."
                ),
            }
        )
    return {
        "schema_version": IMAGE_REVIEW_SCHEMA_VERSION,
        "policy": (
            "Tag/vector shortlist first; final candidates carry an explicit "
            "image-review-required state before acceptance."
        ),
        "items": items,
        "item_count": len(items),
    }


def _unfilled_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(item.get("section_id") or ""),
        str(item.get("need_kind") or ""),
        str(item.get("reason") or ""),
    )


def validate_visual_construction_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic structural validation.  Errors never discard partial output."""

    errors: List[str] = []
    warnings: List[str] = []
    if plan.get("schema_version") != CONSTRUCTION_PLAN_SCHEMA_VERSION:
        errors.append("plan_schema_version_mismatch")
    sections = plan.get("sections", [])
    placements = plan.get("placements", [])
    requests = plan.get("conceptual_figure_requests", [])
    unfilled = plan.get("unfilled_visual_needs", [])
    if not isinstance(sections, list) or not sections:
        errors.append("no_valid_sections")
    for name, value in (
        ("placements", placements),
        ("conceptual_figure_requests", requests),
        ("unfilled_visual_needs", unfilled),
    ):
        if not isinstance(value, list):
            errors.append(f"{name}_not_a_list")

    config = plan.get("config", {})
    require_source_maps = bool(config.get("emit_source_maps", False))
    require_figure_contracts = bool(
        config.get("emit_figure_contracts", False)
    )
    max_placements = int(config.get("max_placements_per_section", 2))
    counts = Counter(
        str(placement.get("section_id") or "")
        for placement in placements
        if isinstance(placement, dict)
    )
    for section_id, count in counts.items():
        if count > max_placements:
            errors.append(
                f"section_{section_id}_placement_limit_exceeded:{count}"
            )

    required_placement_fields = (
        "section_id",
        "visual_chunk_id",
        "paper_id",
        "local_image_path",
        "argumentative_purpose",
        "placement_anchor",
        "figure_mode",
        "permission",
        "provenance",
        "transformation_need",
        "factory_compatible_source_request",
        "status",
    )
    seen_chunks: Set[str] = set()
    seen_paths: Set[str] = set()
    for index, placement in enumerate(placements):
        if not isinstance(placement, dict):
            errors.append(f"placement[{index}]_not_object")
            continue
        for field_name in required_placement_fields:
            if (
                field_name == "paper_id"
                and placement.get("generated_visual")
            ):
                continue
            if not placement.get(field_name):
                errors.append(f"placement[{index}]_missing_{field_name}")
        if require_source_maps:
            source_map_errors = validate_visual_source_map(
                placement.get("source_map")
            )
            errors.extend(
                f"placement[{index}]_{error}"
                for error in source_map_errors
            )
        if require_figure_contracts:
            contract_errors = validate_figure_contract(
                placement.get("figure_contract")
            )
            errors.extend(
                f"placement[{index}]_{error}"
                for error in contract_errors
            )
        if (
            str(placement.get("status") or "")
            not in {
                "verified_existing",
                "traceable_source_pending_review",
            }
        ):
            errors.append(f"placement[{index}]_invalid_status")
        for panel in placement.get("panel_manifest") or []:
            chunk_id = str(panel.get("visual_chunk_id") or "")
            if chunk_id in seen_chunks:
                errors.append(
                    f"placement[{index}]_duplicate_chunk:{chunk_id}"
                )
            seen_chunks.add(chunk_id)
            path_key = _path_key(panel.get("local_image_path"))
            if path_key in seen_paths:
                errors.append(
                    f"placement[{index}]_duplicate_image:{path_key}"
                )
            seen_paths.add(path_key)

    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            errors.append(f"request[{index}]_not_object")
            continue
        kind = str(request.get("figure_kind") or "")
        if kind not in ALLOWED_CONCEPTUAL_FIGURE_KINDS:
            errors.append(f"request[{index}]_kind_not_generatable:{kind}")
        if request.get("required_disclosure") != (
            "AI-generated explanatory visual"
        ):
            errors.append(f"request[{index}]_missing_ai_disclosure")
        if not (request.get("generation_safety") or {}).get("eligible"):
            errors.append(f"request[{index}]_generation_safety_blocked")
        if str(request.get("data_provenance_level") or "") != "schematic":
            errors.append(f"request[{index}]_provenance_not_schematic")
        if request.get("input_data"):
            errors.append(f"request[{index}]_empirical_input_forbidden")
        if request.get("status") != "pending_generation_and_review":
            errors.append(f"request[{index}]_invalid_status")
        if require_figure_contracts:
            contract_errors = validate_figure_contract(
                request.get("figure_contract")
            )
            errors.extend(
                f"request[{index}]_{error}"
                for error in contract_errors
            )

    coverage = plan.get("coverage_audit") or {}
    for requirement in coverage.get("unsatisfied") or []:
        warnings.append(f"coverage_requirement_unmet:{requirement}")

    if not isinstance(plan.get("image_review_queue") or {}, dict):
        errors.append("image_review_queue_missing")
    if not isinstance(plan.get("visual_editorial_plan") or {}, dict):
        errors.append("visual_editorial_plan_missing")

    if errors:
        status = (
            "failed"
            if not sections or (not placements and not requests)
            else "degraded"
        )
    else:
        status = "passed"
    return {
        "status": status,
        "errors": errors[:20],
        "warnings": warnings[:20],
        "fail_open": True,
    }


def to_visual_editorial_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the factory-compatible Visual Editorial Plan subset."""

    placements: List[Dict[str, Any]] = []
    for placement in plan.get("placements") or []:
        if placement.get("figure_mode") == "subfigure" and placement.get(
            "factory_compatible_placements"
        ):
            placements.extend(placement["factory_compatible_placements"])
        else:
            placements.append(placement["factory_compatible_source_request"])
    requests = [
        request.get("factory_compatible_request")
        for request in plan.get("conceptual_figure_requests") or []
        if isinstance(request, dict)
        and isinstance(request.get("factory_compatible_request"), dict)
    ]
    unfilled = [
        {
            "section_id": item.get("section_id", ""),
            "argumentative_purpose": item.get("argumentative_purpose", ""),
            "reason": item.get("reason", ""),
            "status": "unfilled_requires_editorial_decision",
            "priority": item.get("priority", "low"),
        }
        for item in plan.get("unfilled_visual_needs") or []
        if isinstance(item, dict)
    ]
    placement_asset_kinds = Counter(
        _coerce_str(placement.get("asset_kind")) or "figure"
        for placement in placements
    )
    return {
        "schema_version": EDITORIAL_PLAN_SCHEMA_VERSION,
        "input_fingerprint": plan.get("input_fingerprint", ""),
        "placements": placements,
        "conceptual_figure_requests": requests,
        "unfilled_visual_needs": unfilled,
        "asset_kind_counts": dict(placement_asset_kinds),
        "table_count": int(placement_asset_kinds.get("table", 0)),
        "figure_count": int(
            placement_asset_kinds.get("figure", 0)
            + placement_asset_kinds.get("diagram", 0)
        ),
        "policy": {
            "reader_explanation_mode": True,
            "approximate_or_schematic_data_requires_disclosure": True,
            "missing_visual_does_not_invalidate_text": True,
        },
    }


def plan_article_visual_assets(
    *,
    sections: Sequence[Any],
    visual_cache_records: Sequence[Any] = (),
    config: Optional[ArticleVisualAssetPlannerConfig] = None,
    input_fingerprint: str = "",
) -> Dict[str, Any]:
    """Build a deterministic, factory-compatible visual construction plan."""

    cfg = config or ArticleVisualAssetPlannerConfig()
    errors: List[str] = []
    warnings: List[str] = []
    normalized_sections: List[Dict[str, Any]] = []
    for index, section in enumerate(sections or ()):
        try:
            normalized_sections.append(_normalize_section(section, index))
        except Exception as exc:
            errors.append(
                f"section[{index}]_invalid:{type(exc).__name__}:{exc}"
            )
    if not normalized_sections:
        errors.append("no_valid_sections")

    normalized_records: List[Dict[str, Any]] = []
    for index, record in enumerate(visual_cache_records or ()):
        try:
            normalized_records.append(
                _normalize_visual_cache_record(record, index)
            )
        except Exception as exc:
            errors.append(
                "visual_cache_record["
                f"{index}]_invalid:{type(exc).__name__}:{exc}"
            )
    usable_records = [
        record for record in normalized_records if record.get("usable")
    ]
    if len(normalized_records) > len(usable_records):
        warnings.append(
            "visual_cache_records_excluded:"
            f"{len(normalized_records) - len(usable_records)}"
        )

    if not input_fingerprint:
        input_fingerprint = build_article_visual_planner_fingerprint(
            sections=sections,
            visual_cache_records=visual_cache_records,
            config=cfg,
        )

    state: Dict[str, Any] = {
        "used_chunk_ids": set(),
        "used_paths": set(),
        "used_lineages": set(),
        "used_papers": Counter(),
    }
    section_plans: List[Dict[str, Any]] = []
    placements: List[Dict[str, Any]] = []
    unfilled: List[Dict[str, Any]] = []
    unfilled_keys: Set[Tuple[str, str, str]] = set()

    for section in normalized_sections:
        (
            section_plan,
            section_placements,
            section_unfilled,
            section_errors,
        ) = _plan_section(section, usable_records, state, cfg)
        errors.extend(section_errors)
        section_plans.append(section_plan)
        placements.extend(section_placements)
        for item in section_unfilled:
            key = _unfilled_key(item)
            if key in unfilled_keys:
                continue
            unfilled_keys.add(key)
            unfilled.append(item)

    # Pass 2: generation requests, preferring unmet article coverage needs.
    coverage_before_requests = _coverage_audit(
        placements,
        [],
        cfg,
    )
    pending_coverage = list(coverage_before_requests["unsatisfied"])
    requests: List[Dict[str, Any]] = []
    requests_by_section: Dict[str, int] = Counter(
        placement.get("section_id") or ""
        for placement in placements
    )
    section_plan_by_id = {
        section_plan.get("section_id"): section_plan
        for section_plan in section_plans
    }
    for section in normalized_sections:
        section_id = str(section.get("section_id") or "")
        if not _section_has_conceptual_need(section):
            continue
        item_count = requests_by_section.get(section_id, 0)
        if item_count >= cfg.max_total_visual_items_per_section:
            continue
        if len(requests) >= cfg.max_generation_requests_per_section * len(
            normalized_sections
        ):
            break
        kind = ""
        coverage_requirement = ""
        if pending_coverage:
            for requirement in list(pending_coverage):
                if _section_matches_coverage(section, requirement):
                    kind = _request_kind_for_requirement(requirement)
                    coverage_requirement = requirement
                    break
            if not kind and pending_coverage and section is normalized_sections[0]:
                requirement = pending_coverage[0]
                kind = _request_kind_for_requirement(requirement)
                coverage_requirement = requirement
        if not kind:
            kind = _role_based_request_kind(section)
        request = _build_generation_request(
            section,
            kind,
            coverage_requirement=coverage_requirement,
            priority="high" if coverage_requirement else "medium",
        )
        if request is None:
            safety = _generation_safety(section, kind)
            item = {
                "need_id": "",
                "section_id": section_id,
                "section_title": section.get("title", ""),
                "need_kind": (
                    "conceptual_or_explanatory_diagram"
                    if kind in ALLOWED_CONCEPTUAL_FIGURE_KINDS
                    else "empirical_visual_need"
                ),
                "argumentative_purpose": (
                    "A generation request was considered but rejected by "
                    "the non-empirical visual policy."
                ),
                "reason": safety.get("reason", "generation_blocked"),
                "status": "unfilled_requires_editorial_decision",
                "never_blocks_prose": True,
            }
            key = _unfilled_key(item)
            if key not in unfilled_keys:
                unfilled_keys.add(key)
                unfilled.append(item)
            continue
        request["request_id"] = f"CR-{section_id}-{len(requests) + 1:02d}"
        if cfg.emit_figure_contracts:
            figure_contract = build_figure_contract(
                contract_id=f"FC-{request['request_id']}",
                section_id=section_id,
                figure_kind=kind,
                asset_kind="diagram",
                argumentative_purpose=request["argumentative_purpose"],
                claim_bindings=request.get("claim_binding") or [],
                source_caption="",
                editorial_caption=request["caption_proposal"],
                attribution={"paper_id": "generated:pending"},
                permission={
                    "status": "article_owned",
                    "evidence_ceiling": "explanatory_only",
                },
                evidence_ceiling="explanatory_only",
                data_provenance_level="schematic",
                review_state=request["review_state"],
                required_disclosure=request["required_disclosure"],
            )
            request["figure_contract"] = figure_contract
            request["factory_compatible_request"]["figure_contract"] = (
                figure_contract
            )
        requests.append(request)
        requests_by_section[section_id] += 1
        section_plan = section_plan_by_id.get(section_id)
        if section_plan is not None:
            section_plan["generation_requests"].append(request)
            section_plan["request_count"] += 1
            section_plan["item_count"] += 1
        if coverage_requirement in pending_coverage:
            pending_coverage.remove(coverage_requirement)

    # Empirical-only sections: generation is forbidden, so the need is
    # recorded explicitly instead of being silently dropped.
    for section in normalized_sections:
        section_id = str(section.get("section_id") or "")
        has_placements = any(
            placement.get("section_id") == section_id
            for placement in placements
        )
        has_requests = any(
            request.get("section_id") == section_id
            for request in requests
        )
        if has_placements or has_requests:
            continue
        if not _section_has_empirical_only_need(section):
            continue
        item = {
            "need_id": "",
            "section_id": section_id,
            "section_title": section.get("title", ""),
            "need_kind": "empirical_visual_need",
            "argumentative_purpose": (
                "The section asks for measured, spectral, imaging, or "
                "simulated content; such visuals may only come from a "
                "verified source, never from generation."
            ),
            "reason": "empirical_visual_generation_forbidden",
            "status": "unfilled_requires_editorial_decision",
            "never_blocks_prose": True,
        }
        key = _unfilled_key(item)
        if key not in unfilled_keys:
            unfilled_keys.add(key)
            unfilled.append(item)

    # Zero-visual accounting: every section without a *source placement* is
    # represented in the editorial plan.  A pending conceptual generation
    # request is not yet an existing visual, so the zero-placement decision
    # still gets an explicit, non-blocking audit entry.
    for section in normalized_sections:
        section_id = str(section.get("section_id") or "")
        has_placements = any(
            placement.get("section_id") == section_id
            for placement in placements
        )
        if has_placements:
            continue
        purpose = ""
        for claim in section.get("approved_claims") or []:
            purpose = str(claim.get("statement") or "")
            if purpose:
                break
        if not purpose:
            purpose = str(section.get("argument_role") or "") or (
                "Section has no verified existing visual placement; a "
                "conceptual generation request may still be pending."
            )
        item = {
            "need_id": "",
            "section_id": section_id,
            "section_title": section.get("title", ""),
            "need_kind": "section_visual_opportunity",
            "argumentative_purpose": purpose,
            "reason": "zero_visual_selected_acceptable",
            "status": "unfilled_requires_editorial_decision",
            "never_blocks_prose": True,
        }
        key = _unfilled_key(item)
        if key not in unfilled_keys:
            unfilled_keys.add(key)
            unfilled.append(item)

    coverage = _coverage_audit(placements, requests, cfg)
    for requirement in coverage["unsatisfied"]:
        target_section = next(
            (
                section
                for section in normalized_sections
                if _section_matches_coverage(section, requirement)
            ),
            normalized_sections[0] if normalized_sections else None,
        )
        if target_section is None:
            break
        purpose = (
            "Provide a mechanism or conceptual diagram for the article."
            if requirement == "mechanism_or_conceptual_diagram"
            else "Provide a workflow or process diagram for the article."
        )
        item = {
            "need_id": "",
            "section_id": str(target_section.get("section_id") or ""),
            "section_title": target_section.get("title", ""),
            "need_kind": requirement,
            "argumentative_purpose": purpose,
            "reason": "article_coverage_requirement_unmet",
            "status": "unfilled_requires_editorial_decision",
            "never_blocks_prose": True,
        }
        key = _unfilled_key(item)
        if key not in unfilled_keys:
            unfilled_keys.add(key)
            unfilled.append(item)

    # Deterministic sequential ids for unfilled needs.
    for index, item in enumerate(unfilled, 1):
        item["need_id"] = f"UNF-{index:03d}"

    # Keep section unfilled lists in sync with the global list.
    unfilled_by_section: Dict[str, List[Dict[str, Any]]] = {}
    for item in unfilled:
        unfilled_by_section.setdefault(
            str(item.get("section_id") or ""), []
        ).append(item)
    for section_plan in section_plans:
        section_plan["unfilled_needs"] = unfilled_by_section.get(
            section_plan["section_id"], []
        )

    image_review_queue = _build_image_review_queue(placements, requests)
    placement_asset_kinds = Counter(
        _coerce_str(placement.get("asset_kind"))
        or ("diagram" if placement.get("generated_visual") else "figure")
        for placement in placements
    )
    plan: Dict[str, Any] = {
        "schema_version": CONSTRUCTION_PLAN_SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "mode": "article_visual_asset_planner.deterministic_sidecar",
        "planner": {
            "role": "backend_sidecar_adapter",
            "model_calls": 0,
            "retrieval_strategy": "labels_and_lexical_vectors_first",
        },
        "config": cfg.as_dict(),
        "sections": section_plans,
        "placements": placements,
        "conceptual_figure_requests": requests,
        "unfilled_visual_needs": unfilled,
        "coverage_audit": coverage,
        "asset_kind_counts": dict(placement_asset_kinds),
        "table_count": int(placement_asset_kinds.get("table", 0)),
        "figure_count": int(
            placement_asset_kinds.get("figure", 0)
            + placement_asset_kinds.get("diagram", 0)
        ),
        "generation_safety": {
            "schema_version": "research_harness.generation_safety.v1",
            "policy": (
                "Only non-empirical explanatory visuals may become "
                "generation requests; curves, microscopy, spectra, "
                "measurements and simulation results are never generated."
            ),
            "allowed_kinds": sorted(ALLOWED_CONCEPTUAL_FIGURE_KINDS),
            "forbidden_kinds": sorted(FORBIDDEN_GENERATION_KINDS),
            "approved_request_count": len(requests),
            "blocked_request_count": sum(
                1
                for item in unfilled
                if "forbidden" in str(item.get("reason") or "")
                or "empirical" in str(item.get("reason") or "")
            ),
        },
        "image_review_queue": image_review_queue,
        "policy": {
            "zero_visual_allowed": True,
            "max_placements_per_section": cfg.max_placements_per_section,
            "weak_filler_forbidden": True,
            "duplicate_image_reuse_forbidden": True,
            "local_source_preferred": True,
            "whole_vs_subfigure_decided": True,
            "missing_visual_never_blocks_prose": True,
            "empirical_generation_forbidden": True,
            "ai_disclosure_required": True,
            "image_review_required_for_final_candidates": True,
        },
        "validation": {},
        "visual_editorial_plan": {},
    }
    plan["visual_editorial_plan"] = to_visual_editorial_plan(plan)
    plan["validation"] = validate_visual_construction_plan(plan)
    if errors:
        validation = plan["validation"]
        if validation["status"] == "passed":
            validation["status"] = "degraded"
        validation["errors"] = list(dict.fromkeys(
            [*validation["errors"], *errors]
        ))[:20]
    if warnings:
        plan["validation"]["warnings"] = list(dict.fromkeys(
            [*plan["validation"]["warnings"], *warnings]
        ))[:20]
    return plan


__all__ = [
    "ArticleClaimInput",
    "ArticleCitationInput",
    "ArticleSectionInput",
    "ArticleVisualAssetPlannerConfig",
    "ALLOWED_CONCEPTUAL_FIGURE_KINDS",
    "CONSTRUCTION_PLAN_SCHEMA_VERSION",
    "EDITORIAL_PLAN_SCHEMA_VERSION",
    "PLANNER_ASSET_KINDS",
    "build_article_visual_planner_fingerprint",
    "plan_article_visual_assets",
    "to_visual_editorial_plan",
    "validate_visual_construction_plan",
]
