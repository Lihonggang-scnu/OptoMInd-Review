"""Materialize an article-level visual plan into renderable review figures.

The Visual Editor decides *what would help the article*.  This module owns the
deterministic and bounded work that follows:

* admit traceable source figures, including a small pending shortlist;
* audit only selected pending figures instead of the whole visual corpus;
* audit each selected source figure against the actual target section
  context (title, full text or a documented generous context window,
  argument role, intended anchor), with a cache key that binds section
  context and auditor prompt content;
* apply a structural visual policy (Abstract/Conclusion zero visuals,
  Introduction at most one total visual, body sections 0-2 as a ceiling)
  before materialization, and prefer mechanism + workflow/decision
  schematics when the generation budget allows at least two;
* compose explicitly grouped panels without losing provenance;
* generate bounded explanatory schematics when requested;
* apply the configured headless/human-review decision;
* write one FINAL_VISUAL_PACKAGE.json consumed by publication renderers.

The module is deliberately topic-agnostic.  It builds a reusable factory, not
special-case assets for one optical review.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from llm.qwen_vision_client import call_qwen_vision
from optomind_research.conceptual_visual_generator import (
    AUDITOR_PROMPT,
    ConceptualVisualGenerator,
)

from .artifact_store import atomic_write_json
from .conceptual_diagram_renderer import ConceptualDiagramRenderer
from .cost_ledger import estimate_call_cost_cny
from .human_decision_gate import request_decision
from .visual_review_queue import (
    apply_visual_review_queue,
    build_visual_review_queue,
)

logger = logging.getLogger(__name__)

_GENERATION_AUDIT_RESERVE_CNY = 0.05
_GENERATION_AUDIT_INPUT_RESERVE_TOKENS = 64_000
_GENERATION_AUDIT_OUTPUT_RESERVE_TOKENS = 1_200
# The structured-diagram fallback bills one text-spec call plus one vision
# audit; Graphviz rendering is local and free.  Its reservation must cover
# those two billable calls without reserving the raster image-generation
# reference cost, otherwise an affordable fallback is skipped even though
# the shared budget still has room for it.
_STRUCTURED_SPEC_RESERVE_CNY = 0.10
_STRUCTURED_SPEC_INPUT_RESERVE_TOKENS = 8_000
_STRUCTURED_SPEC_OUTPUT_RESERVE_TOKENS = 2_000


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_AUDITOR_PROMPT = (
    PROJECT_ROOT / "prompts" / "Source Visual Shortlist Auditor.txt"
)

_CACHE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def derive_visual_cache_namespace(
    blueprint: Mapping[str, Any] | None = None,
    explicit: str = "",
) -> str:
    """Return a filesystem-safe, topic-scoped visual-cache namespace.

    An explicit namespace is accepted only when it is path-safe.  Otherwise
    the topic identity contract (or a compact fallback projection of the
    blueprint) is hashed.  This keeps the cache reusable across runs of the
    same topic while preventing one topic's source audits or diagrams from
    becoming another topic's candidates.
    """

    requested = str(explicit or "").strip()
    if requested:
        if _CACHE_NAMESPACE_RE.fullmatch(requested):
            return requested
        return "topic-" + hashlib.sha256(
            requested.encode("utf-8", errors="replace")
        ).hexdigest()[:32]

    source = dict(blueprint or {})
    identity = source.get("topic_identity")
    identity = dict(identity) if isinstance(identity, Mapping) else {}
    projection = {
        "fingerprint": str(identity.get("fingerprint") or ""),
        "normalized_question": str(
            identity.get("normalized_question")
            or source.get("review_thesis")
            or source.get("full_review_argument")
            or ""
        ),
        "core_anchor_tokens": list(
            identity.get("core_anchor_tokens") or []
        ),
        "anchor_phrases": list(identity.get("anchor_phrases") or []),
        "sections": [
            {
                "section_id": str(row.get("section_id") or ""),
                "title": str(row.get("title") or ""),
                "argument_role": str(row.get("argument_role") or ""),
            }
            for row in source.get("sections", []) or []
            if isinstance(row, Mapping)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return "topic-" + digest[:32]


def scoped_visual_cache_dir(
    base_dir: Path,
    blueprint: Mapping[str, Any] | None = None,
    *,
    namespace: str = "",
) -> Path:
    """Place a visual cache below ``topics/<safe-namespace>``."""

    resolved = derive_visual_cache_namespace(blueprint, namespace)
    return Path(base_dir) / "topics" / resolved

ACCEPTED_REVIEW_STATES = {
    "human_approved",
    "timeout_accepted_for_draft",
    "system_approved_test_mode",
    "system_approved_test_mode_with_warnings",
}
SAFE_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
}
FINAL_ASSET_SUBDIR = "final_assets"
MAX_GENERATION_TOTAL_ATTEMPTS = 3
STRUCTURED_DIAGRAM_KINDS = frozenset(
    {
        "mechanism_schematic",
        "workflow_schematic",
        "concept_map",
        "taxonomy_diagram",
        "comparison_diagram",
    }
)
_STRUCTURED_ROUTE_ALIASES = frozenset(
    {
        "structured",
        "structured_diagram",
        "structured_spec_graphviz",
        "qwen_spec_graphviz",
    }
)
_RASTER_ROUTE_ALIASES = frozenset(
    {
        "raster",
        "raster_image_generation",
        "qwen_image",
        "image_generation",
    }
)

# Structural visual policy: manuscript front/back matter gets no visuals,
# the introduction gets at most one total visual, and body sections keep
# the planner's 0-2 ceiling.  This is generic manuscript structure, never a
# topic-specific rule.
STRUCTURAL_ZERO_VISUAL_SECTION_IDS = frozenset({"abstract", "conclusion"})
STRUCTURAL_INTRODUCTION_SECTION_ID = "introduction"
STRUCTURAL_POLICY_ZERO_REASON = (
    "excluded_structural_policy_zero_visuals"
)
STRUCTURAL_POLICY_INTRO_CAP_REASON = (
    "excluded_structural_policy_introduction_cap"
)

# The source-figure auditor receives the actual target section context.
# When the full section text is longer than this generous window it is
# deterministically truncated (never summarized) so the audit stays local
# and bounded.
SECTION_CONTEXT_CHAR_LIMIT = 12000
SECTION_CONTEXT_TRUNCATION_MARKER = (
    "\n[... section context truncated for the visual audit ...]"
)

_MECHANISM_KIND_MARKERS = ("mechanism", "concept")
_WORKFLOW_KIND_MARKERS = (
    "workflow",
    "process",
    "decision",
    "taxonomy",
    "roadmap",
    "pipeline",
)


def _section_policy_kind(section_id: str) -> str:
    folded = str(section_id or "").casefold()
    if folded in STRUCTURAL_ZERO_VISUAL_SECTION_IDS:
        return "zero"
    if folded == STRUCTURAL_INTRODUCTION_SECTION_ID:
        return "introduction"
    return "body"


def _section_context_text(section: Mapping[str, Any]) -> str:
    """Full section text, or a documented generous deterministic window."""

    text = " ".join(
        str(
            section.get("full_text")
            or section.get("text")
            or ""
        ).split()
    ).strip()
    if len(text) <= SECTION_CONTEXT_CHAR_LIMIT:
        return text
    return text[:SECTION_CONTEXT_CHAR_LIMIT] + (
        SECTION_CONTEXT_TRUNCATION_MARKER
    )


def _source_audit_section_context(
    section: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    """Compact, deterministic section context for the source-figure audit."""

    return {
        "section_id": str(
            section.get("section_id") or item.get("section_id") or ""
        ),
        "title": str(section.get("title") or ""),
        "full_text_or_context_window": _section_context_text(section),
        "argument_role": str(section.get("argument_role") or ""),
        "intended_anchor": str(
            item.get("placement_guidance")
            or item.get("section_id")
            or ""
        ),
    }


def build_source_audit_cache_key(
    *,
    image_sha256: str,
    section_context: Mapping[str, Any],
    purpose: str,
    caption_preview: str,
    prompt_sha256: str,
    cache_namespace: str = "",
) -> str:
    """Cache key for a source-figure vision audit.

    The key binds the image hash, the actual target section context, the
    intended purpose/caption, and the auditor prompt content, so an older
    caption-only approval can never be reused for a different section or a
    changed prompt.
    """

    payload = {
        "image_sha256": str(image_sha256),
        "section_context": dict(section_context),
        "purpose": str(purpose),
        "caption_preview": str(caption_preview),
        "prompt_sha256": str(prompt_sha256),
        "cache_namespace": str(cache_namespace or ""),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _policy_coverage_audit(
    placements: Iterable[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute the article coverage audit after structural filtering."""

    kinds = [
        str(row.get("figure_kind") or "")
        for row in placements
    ] + [
        str(row.get("figure_kind") or "")
        for row in requests
    ]
    joined = " ".join(kinds).casefold()

    def satisfied(markers: tuple[str, ...]) -> bool:
        return any(marker in joined for marker in markers)

    required = (
        "mechanism_or_conceptual_diagram",
        "workflow_or_process_diagram",
    )
    satisfied_list = []
    if satisfied(_MECHANISM_KIND_MARKERS):
        satisfied_list.append("mechanism_or_conceptual_diagram")
    if satisfied(_WORKFLOW_KIND_MARKERS):
        satisfied_list.append("workflow_or_process_diagram")
    return {
        "required": list(required),
        "satisfied": satisfied_list,
        "unsatisfied": [
            requirement
            for requirement in required
            if requirement not in satisfied_list
        ],
        "status": (
            "satisfied"
            if not [
                requirement
                for requirement in required
                if requirement not in satisfied_list
            ]
            else "degraded"
        ),
        "policy": (
            "Missing visuals are recorded as unfilled needs and never "
            "block otherwise sound prose."
        ),
    }


def _policy_image_review_queue(
    placements: Iterable[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild the image review queue from the policy-filtered plan."""

    items: list[dict[str, Any]] = []
    for placement in placements:
        items.append(
            {
                "visual_chunk_id": str(
                    placement.get("visual_chunk_id") or ""
                ),
                "paper_id": str(placement.get("paper_id") or ""),
                "local_image_path": str(
                    placement.get("local_image_path") or ""
                ),
                "section_id": str(placement.get("section_id") or ""),
                "review_required": bool(
                    placement.get("image_review_required")
                ),
                "review_state": str(placement.get("review_state") or ""),
                "reason": str(
                    placement.get("review_required_reason") or ""
                ),
            }
        )
    for request in requests:
        items.append(
            {
                "visual_chunk_id": str(request.get("request_id") or ""),
                "paper_id": "",
                "local_image_path": "",
                "section_id": str(request.get("section_id") or ""),
                "review_required": True,
                "review_state": "generated_visual_review_required",
                "reason": (
                    "Generated explanatory visual must be image-reviewed "
                    "before publication."
                ),
            }
        )
    return {
        "schema_version": (
            "research_harness.article_visual_image_review.v1"
        ),
        "policy": (
            "Tag/vector shortlist first; final candidates carry an explicit "
            "image-review-required state before acceptance."
        ),
        "items": items,
        "item_count": len(items),
    }


def apply_structural_visual_policy(
    visual_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the structural visual policy to a visual plan.

    ``Abstract`` and ``Conclusion`` receive zero placements and zero
    generation requests; ``Introduction`` receives at most one total visual
    (placements + generation requests).  Body sections keep the planner's
    0-2 ceiling.  Every excluded item is recorded as an unfilled opportunity
    with an explicit policy reason so no visual decision is silent and no
    missing visual ever blocks text.
    """

    plan: dict[str, Any] = dict(visual_plan)
    plan["placements"] = [
        dict(row)
        for row in (plan.get("placements") or [])
        if isinstance(row, Mapping)
    ]
    plan["conceptual_figure_requests"] = [
        dict(row)
        for row in (plan.get("conceptual_figure_requests") or [])
        if isinstance(row, Mapping)
    ]
    plan["unfilled_visual_needs"] = [
        dict(row)
        for row in (plan.get("unfilled_visual_needs") or [])
        if isinstance(row, Mapping)
    ]
    plan["sections"] = [
        dict(row)
        for row in (plan.get("sections") or [])
        if isinstance(row, Mapping)
    ]

    retained_placements: list[dict[str, Any]] = []
    retained_requests: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    intro_total = 0

    def drop_item(
        item: Mapping[str, Any],
        section_id: str,
        reason: str,
        kind: str,
    ) -> None:
        dropped.append(
            {
                "section_id": section_id,
                "section_title": str(item.get("section_title") or ""),
                "reason": reason,
                "need_kind": "structural_visual_policy_exclusion",
                "argumentative_purpose": str(
                    item.get("argumentative_purpose") or ""
                ),
                "kind": kind,
            }
        )

    for placement in plan["placements"]:
        section_id = str(placement.get("section_id") or "")
        kind = _section_policy_kind(section_id)
        if kind == "zero":
            drop_item(
                placement,
                section_id,
                STRUCTURAL_POLICY_ZERO_REASON,
                "placement",
            )
            continue
        if kind == "introduction" and intro_total >= 1:
            drop_item(
                placement,
                section_id,
                STRUCTURAL_POLICY_INTRO_CAP_REASON,
                "placement",
            )
            continue
        retained_placements.append(placement)
        intro_total += 1

    for request in plan["conceptual_figure_requests"]:
        section_id = str(request.get("section_id") or "")
        kind = _section_policy_kind(section_id)
        if kind == "zero":
            drop_item(
                request,
                section_id,
                STRUCTURAL_POLICY_ZERO_REASON,
                "generation_request",
            )
            continue
        if kind == "introduction" and intro_total >= 1:
            drop_item(
                request,
                section_id,
                STRUCTURAL_POLICY_INTRO_CAP_REASON,
                "generation_request",
            )
            continue
        retained_requests.append(request)
        intro_total += 1

    unfilled = plan["unfilled_visual_needs"]
    seen_keys: set[tuple[str, str, str]] = set()
    for entry in unfilled:
        seen_keys.add(
            (
                str(entry.get("section_id") or ""),
                str(entry.get("need_kind") or ""),
                str(entry.get("reason") or ""),
            )
        )
    for entry in dropped:
        key = (
            entry["section_id"],
            entry["need_kind"],
            entry["reason"],
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unfilled.append(
            {
                "need_id": "",
                "section_id": entry["section_id"],
                "section_title": entry["section_title"],
                "need_kind": entry["need_kind"],
                "argumentative_purpose": (
                    entry["argumentative_purpose"]
                    or (
                        "Abstract and Conclusion are structural "
                        "front/back matter with zero default visuals."
                        if entry["reason"] == STRUCTURAL_POLICY_ZERO_REASON
                        else (
                            "Introduction accepts at most one total visual "
                            "by structural policy."
                        )
                    )
                ),
                "reason": entry["reason"],
                "status": "unfilled_requires_editorial_decision",
                "never_blocks_prose": True,
            }
        )

    # Zero-visual sections are always explicitly recorded, even when the
    # raw plan already had no placement there, so the policy decision is
    # auditable rather than silent.
    for section in plan["sections"]:
        section_id = str(section.get("section_id") or "")
        if _section_policy_kind(section_id) != "zero":
            continue
        key = (
            section_id,
            "structural_visual_policy_exclusion",
            STRUCTURAL_POLICY_ZERO_REASON,
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unfilled.append(
            {
                "need_id": "",
                "section_id": section_id,
                "section_title": str(section.get("title") or ""),
                "need_kind": "structural_visual_policy_exclusion",
                "argumentative_purpose": (
                    "Abstract and Conclusion are structural front/back "
                    "matter with zero default visuals."
                ),
                "reason": STRUCTURAL_POLICY_ZERO_REASON,
                "status": "unfilled_requires_editorial_decision",
                "never_blocks_prose": True,
            }
        )

    for index, entry in enumerate(unfilled, 1):
        entry["need_id"] = f"UNF-{index:03d}"

    plan["placements"] = retained_placements
    plan["conceptual_figure_requests"] = retained_requests
    plan["unfilled_visual_needs"] = unfilled

    unfilled_by_section: dict[str, list[dict[str, Any]]] = {}
    for entry in unfilled:
        unfilled_by_section.setdefault(
            str(entry.get("section_id") or ""),
            [],
        ).append(entry)
    for section_plan in plan["sections"]:
        section_id = str(section_plan.get("section_id") or "")
        section_placements = [
            placement
            for placement in retained_placements
            if str(placement.get("section_id") or "") == section_id
        ]
        section_requests = [
            request
            for request in retained_requests
            if str(request.get("section_id") or "") == section_id
        ]
        section_plan["placements"] = section_placements
        section_plan["generation_requests"] = section_requests
        section_plan["placement_count"] = len(section_placements)
        section_plan["request_count"] = len(section_requests)
        section_plan["item_count"] = (
            len(section_placements) + len(section_requests)
        )
        section_plan["unfilled_needs"] = unfilled_by_section.get(
            section_id,
            [],
        )

    plan["coverage_audit"] = _policy_coverage_audit(
        retained_placements,
        retained_requests,
    )
    plan["image_review_queue"] = _policy_image_review_queue(
        retained_placements,
        retained_requests,
    )
    return plan


def _request_kind(request: Mapping[str, Any]) -> str:
    return str(request.get("figure_kind") or "").casefold()


def _is_mechanism_request(request: Mapping[str, Any]) -> bool:
    return any(
        marker in _request_kind(request)
        for marker in _MECHANISM_KIND_MARKERS
    )


def _is_workflow_request(request: Mapping[str, Any]) -> bool:
    return any(
        marker in _request_kind(request)
        for marker in _WORKFLOW_KIND_MARKERS
    )


_GENERATION_PRIORITY_RANK = {
    # Lower rank sorts first.  Unknown/missing values deliberately land on
    # medium: an unreadable priority must neither win the race nor be
    # buried behind explicit lows.
    "high": 0,
    "medium": 1,
    "low": 2,
}


def _generation_priority_rank(request: Mapping[str, Any]) -> int:
    raw = str(request.get("priority") or "").strip().lower()
    return _GENERATION_PRIORITY_RANK.get(
        raw, _GENERATION_PRIORITY_RANK["medium"]
    )


def _mechanism_workflow_first(
    group: list[tuple[int, Dict[str, Any]]],
) -> list[tuple[int, Dict[str, Any]]]:
    """Apply the historical pairing preference inside one priority group."""

    if len(group) < 2:
        return list(group)
    mechanism = next(
        (row for row in group if _is_mechanism_request(row[1])),
        None,
    )
    workflow = next(
        (
            row
            for row in group
            if _is_workflow_request(row[1])
            and (mechanism is None or row[0] != mechanism[0])
        ),
        None,
    )
    if mechanism is None or workflow is None:
        return list(group)
    head = [mechanism, workflow]
    chosen_indexes = {mechanism[0], workflow[0]}
    head.extend(row for row in group if row[0] not in chosen_indexes)
    return head


def _prioritized_generation_order(
    requests: Iterable[Dict[str, Any]],
    max_generated_images: int,
) -> list[tuple[int, Dict[str, Any]]]:
    """Order generation requests by declared priority, then kind pairing.

    P1-5: the planner priority field is now the primary sort key
    (high -> medium -> low -> unknown treated as medium).  Within one
    priority level the historical mechanism-first / workflow-second
    preference is kept as the secondary key, and original file order
    breaks remaining ties (stable).  Requests excluded from eligibility
    keep their original relative order at the tail.  This is a preference,
    never a hard quota: early exits below are unchanged, and nothing here
    touches the cap -- overflow requests still overflow and still emit
    their event.
    """

    rows = [
        (index, request)
        for index, request in enumerate(requests)
        if isinstance(request, dict)
    ]
    if max_generated_images < 2 or len(rows) < 2:
        return rows
    eligible = [
        row
        for row in rows
        if _section_policy_kind(
            str(row[1].get("section_id") or "")
        )
        != "zero"
    ]
    if len(eligible) < 2:
        return rows
    ordered: list[tuple[int, Dict[str, Any]]] = []
    for rank in sorted({_generation_priority_rank(row[1]) for row in eligible}):
        group = [
            row
            for row in eligible
            if _generation_priority_rank(row[1]) == rank
        ]
        ordered.extend(_mechanism_workflow_first(group))
    ordered_indexes = {row[0] for row in ordered}
    ordered.extend(row for row in rows if row[0] not in ordered_indexes)
    return ordered


def _resolve_generation_route(
    request: Dict[str, Any],
    *,
    custom_generator_seam: bool = False,
) -> str:
    """Choose the structured-spec route or the raster generator route.

    An explicit route field wins.  Otherwise text-heavy scientific diagram
    kinds default to the structured route (Qwen semantic spec + local
    Graphviz), while other eligible kinds keep the raster generator.  When a
    caller supplied a custom ``conceptual_generator_factory`` without also
    supplying a renderer factory, the historical raster/custom-generator
    extension seam is preserved for text-heavy kinds as well.
    """

    explicit = str(
        request.get("visual_route")
        or request.get("preferred_visual_route")
        or ""
    ).strip().lower()
    if explicit in _STRUCTURED_ROUTE_ALIASES:
        return "structured_diagram"
    if explicit in _RASTER_ROUTE_ALIASES:
        return "raster_image_generation"
    kind = str(request.get("figure_kind") or "").strip().lower()
    if kind in STRUCTURED_DIAGRAM_KINDS:
        if custom_generator_seam:
            return "raster_image_generation"
        return "structured_diagram"
    return "raster_image_generation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def normalize_visual_factory_plan(
    payload: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Normalize legacy plans and staged-remount factory references.

    The staged remount artifact is a relocatable reference envelope whose
    canonical factory input lives under ``editorial_plan``.  Older factory
    inputs put the same fields at the top level.  This adapter accepts both
    without treating generation requests as materialized figures.
    """

    raw = dict(payload) if isinstance(payload, Mapping) else {}
    schema_version = str(raw.get("schema_version") or "")
    direct_plan_keys = {
        "placements",
        "conceptual_figure_requests",
        "unfilled_visual_needs",
    }
    nested = raw.get("editorial_plan")
    is_remount_reference = bool(
        isinstance(nested, Mapping)
        and (
            "factory_plan_reference" in schema_version
            or not any(key in raw for key in direct_plan_keys)
        )
    )
    if is_remount_reference:
        plan = dict(nested)
        source_container = "editorial_plan"
    else:
        plan = raw
        source_container = "top_level"

    # Support the descriptive alias used by some planner section contracts,
    # while keeping conceptual requests separate from generated assets.
    if (
        "conceptual_figure_requests" not in plan
        and isinstance(plan.get("generation_requests"), list)
    ):
        plan["conceptual_figure_requests"] = list(
            plan.get("generation_requests") or []
        )

    ingestion = {
        "input_schema_version": schema_version,
        "canonical_plan_schema_version": str(
            plan.get("schema_version") or ""
        ),
        "source_container": source_container,
        "placement_count": len(plan.get("placements") or []),
        "generation_request_count": len(
            plan.get("conceptual_figure_requests") or []
        ),
        "unfilled_need_count": len(
            plan.get("unfilled_visual_needs") or []
        ),
        "generation_requests_are_materialized_figures": False,
    }
    return plan, ingestion


def _safe_id(value: Any, fallback: str = "visual") -> str:
    cleaned = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        str(value or ""),
    ).strip("_.")
    return cleaned or fallback


def _safe_asset_stem(value: Any, fallback: str = "figure") -> str:
    cleaned = _safe_id(value, fallback)
    return cleaned[:80]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _usage_cost(result: Dict[str, Any], fallback_model: str) -> Dict[str, Any]:
    usage = dict(result.get("_llm_usage") or {})
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("estimated_input_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("estimated_output_tokens")
        or 0
    )
    model_name = str(usage.get("model_name") or fallback_model)
    return {
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_cny": estimate_call_cost_cny(
            model_name,
            input_tokens,
            output_tokens,
        ),
    }


def _safe_json_from_text(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(str(text or ""))
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def build_visual_factory_input_fingerprint(
    *,
    visual_plan: Dict[str, Any],
    blueprint: Dict[str, Any],
    real_visual_audit: bool,
    real_image_generation: bool,
    test_mode: bool,
    vision_model_tier: str,
    image_model: str,
    max_generated_images: int,
    structural_visual_policy_enabled: bool = True,
    cache_namespace: str = "",
) -> str:
    """Fingerprint every input that can change the materialized visual set."""

    prompt_paths = (
        SOURCE_AUDITOR_PROMPT,
        AUDITOR_PROMPT,
        (
            PROJECT_ROOT
            / "prompts"
            / "Conceptual Scientific Figure Generator.txt"
        ),
        (
            PROJECT_ROOT
            / "prompts"
            / "Conceptual Diagram Spec Generator.txt"
        ),
    )
    payload = {
        "schema_version": "visual_factory.input_fingerprint.v2",
        "visual_plan": visual_plan,
        "blueprint": blueprint,
        "real_visual_audit": bool(real_visual_audit),
        "real_image_generation": bool(real_image_generation),
        "test_mode": bool(test_mode),
        "vision_model_tier": str(vision_model_tier),
        "image_model": str(image_model),
        "max_generated_images": int(max_generated_images),
        "structural_visual_policy_enabled": bool(
            structural_visual_policy_enabled
        ),
        "cache_namespace": str(cache_namespace or ""),
        "prompt_sha256": {
            path.name: (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else ""
            )
            for path in prompt_paths
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def build_article_visual_contract(
    *,
    blueprint: Dict[str, Any],
    visual_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a deterministic article-level visual contract.

    It records useful opportunities without inventing a rigid one-figure-per-
    section quota.  The Visual Editor remains responsible for intellectual
    selection; this contract makes its decisions auditable and publishable.
    """

    sections = {
        str(row.get("section_id") or ""): row
        for row in blueprint.get("sections", []) or []
        if isinstance(row, dict) and row.get("section_id")
    }
    slots: List[Dict[str, Any]] = []
    figure_contracts: List[Dict[str, Any]] = []
    seen_contract_ids: set[str] = set()

    def append_slot(
        *,
        section_id: str,
        source: str,
        purpose: str,
        preferred_route: str,
        priority: str = "medium",
        figure_kind: str = "",
        item: Optional[Dict[str, Any]] = None,
    ) -> None:
        section = sections.get(section_id, {})
        slot_index = len(slots) + 1
        figure_contract = (
            dict((item or {}).get("figure_contract") or {})
            if isinstance((item or {}).get("figure_contract"), dict)
            else {}
        )
        contract_id = str(figure_contract.get("contract_id") or "")
        if contract_id and contract_id not in seen_contract_ids:
            seen_contract_ids.add(contract_id)
            figure_contracts.append(figure_contract)
        slots.append(
            {
                "visual_slot_id": f"VSL-{slot_index:03d}",
                "section_id": section_id,
                "target_section_ids": list(
                    dict.fromkeys(
                        [
                            section_id,
                            *[
                                str(value)
                                for value in (
                                    (item or {}).get(
                                        "target_section_ids",
                                        [],
                                    )
                                    or []
                                )
                                if str(value)
                            ],
                        ]
                    )
                ),
                "section_title": str(
                    section.get("section_title")
                    or section.get("title")
                    or ""
                ),
                "source": source,
                "intended_reader_takeaway": purpose,
                "preferred_visual_route": preferred_route,
                "figure_kind": figure_kind,
                "priority": priority,
                "skip_allowed": True,
                "real_data_required": preferred_route == "source_derived",
                "approximate_data_allowed": bool(
                    (item or {}).get("approximate_data_allowed", False)
                ),
                "estimated_information_gain": str(
                    (item or {}).get("estimated_information_gain")
                    or "medium"
                ),
                "single_or_composite_preference": str(
                    (item or {}).get(
                        "single_or_composite_preference"
                    )
                    or (
                        "composite"
                        if (item or {}).get("composite_group_id")
                        else "single_or_best_explanatory_form"
                    )
                ),
                "preferred_visual_roles": list(
                    (item or {}).get("preferred_visual_roles")
                    or []
                ),
                "estimated_cost_cny": float(
                    (item or {}).get("estimated_cost_cny")
                    or (
                        0.0
                        if preferred_route == "source_derived"
                        else 0.5
                    )
                ),
                "figure_contract_id": contract_id,
                "source_map_ref": dict(
                    (item or {}).get("source_map_ref")
                    or figure_contract.get("source_map_ref")
                    or {}
                ),
            }
        )

    for item in visual_plan.get("placements", []) or []:
        if not isinstance(item, dict):
            continue
        append_slot(
            section_id=str(item.get("section_id") or ""),
            source="selected_source_placement",
            purpose=str(item.get("argumentative_purpose") or ""),
            preferred_route="source_derived",
            priority=str(item.get("priority") or "medium"),
            figure_kind=str(item.get("figure_kind") or "source_figure"),
            item=item,
        )
    for item in visual_plan.get("conceptual_figure_requests", []) or []:
        if not isinstance(item, dict):
            continue
        append_slot(
            section_id=str(item.get("section_id") or ""),
            source="conceptual_request",
            purpose=str(item.get("argumentative_purpose") or ""),
            preferred_route=str(
                item.get("preferred_visual_route")
                or "conceptual_generated"
            ),
            priority=str(item.get("priority") or "medium"),
            figure_kind=str(item.get("figure_kind") or ""),
            item=item,
        )
    for item in visual_plan.get("unfilled_visual_needs", []) or []:
        if not isinstance(item, dict):
            continue
        append_slot(
            section_id=str(item.get("section_id") or ""),
            source="unfilled_visual_opportunity",
            purpose=str(item.get("argumentative_purpose") or ""),
            preferred_route="best_available_or_skip",
            priority=str(item.get("priority") or "low"),
            item=item,
        )

    return {
        "schema_version": "research_harness.article_visual_contract.v1",
        "created_at": _now(),
        "mode": "reader_explanation",
        "slots": slots,
        "slot_count": len(slots),
        "figure_contracts": figure_contracts,
        "figure_contract_count": len(figure_contracts),
        "policy": {
            "one_figure_per_section_required": False,
            "missing_figure_invalidates_text": False,
            "source_library_first": True,
            "pending_requests_are_not_rendered_figures": True,
        },
    }


MAX_EDITORIAL_CAPTION_CHARS = 500
FALLBACK_CAPTION_MAX_CHARS = 280


def _valid_editorial_caption(value: Any) -> str:
    """Normalize and validate a model-provided editorial caption.

    The caption must be a short non-empty string.  Invalid or overly long
    values are treated as absent so local code always owns the fallback.
    """

    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    if len(text) > MAX_EDITORIAL_CAPTION_CHARS:
        return ""
    return text


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars at the last word boundary when possible."""

    if len(text) <= limit:
        return text
    head = text[:limit]
    boundary = head.rfind(" ")
    if boundary > 0:
        return head[:boundary]
    return head


def _fallback_caption_for_source(item: Dict[str, Any]) -> str:
    """Deterministically short fallback; never includes the raw source caption.

    The caption is whitespace-normalized and capped at
    ``FALLBACK_CAPTION_MAX_CHARS`` characters including the optional source
    identity.  Long purposes are cut cleanly at a word boundary so the
    fail-open path never recreates a garbled long caption.
    """

    purpose = re.sub(
        r"\s+",
        " ",
        str(item.get("argumentative_purpose") or ""),
    ).strip()
    paper_id = str(item.get("paper_id") or "").strip()
    suffix = f" Source: {paper_id}." if paper_id else ""
    base = purpose or "Source-derived scientific figure."
    if len(base) + len(suffix) <= FALLBACK_CAPTION_MAX_CHARS:
        return f"{base}{suffix}"
    base = _truncate_at_word_boundary(
        base,
        FALLBACK_CAPTION_MAX_CHARS - len(suffix),
    )
    return f"{base}{suffix}"


def _review_decision(*, test_mode: bool) -> str:
    return (
        "system_approved_test_mode"
        if test_mode
        else "timeout_accepted_for_draft"
    )


def _source_audit_fallback(
    item: Dict[str, Any],
    *,
    reason: str,
) -> Dict[str, Any]:
    return {
        "verdict": "approve",
        "usefulness": "usable explanatory source figure",
        "misleading_risk": "low",
        "editorial_caption": "",
        "audit_mode": "deterministic_traceability_fallback",
        "reason": reason,
        "warnings": [reason],
    }


def _compose_panels(
    *,
    figure_id: str,
    panels: List[Dict[str, Any]],
    output_dir: Path,
) -> Optional[Path]:
    """Create a simple, reversible review composite from selected panels."""

    loaded: List[tuple[Image.Image, Dict[str, Any]]] = []
    for panel in panels:
        path = Path(str(panel.get("local_path") or ""))
        if not path.is_file():
            continue
        try:
            with Image.open(path) as source:
                loaded.append((source.convert("RGB").copy(), panel))
        except Exception:
            continue
    if len(loaded) < 2:
        return None

    count = min(6, len(loaded))
    loaded = loaded[:count]
    columns = 2 if count <= 4 else 3
    rows = int(math.ceil(count / columns))
    panel_width = 1000
    panel_height = 720
    margin = 36
    label_band = 58
    canvas = Image.new(
        "RGB",
        (
            columns * panel_width + (columns + 1) * margin,
            rows * (panel_height + label_band) + (rows + 1) * margin,
        ),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    for index, (image, _) in enumerate(loaded):
        row = index // columns
        column = index % columns
        x = margin + column * (panel_width + margin)
        y = margin + row * (panel_height + label_band + margin)
        fitted = ImageOps.contain(image, (panel_width, panel_height))
        px = x + (panel_width - fitted.width) // 2
        py = y + (panel_height - fitted.height) // 2
        canvas.paste(fitted, (px, py))
        draw.text(
            (x + 12, y + panel_height + 12),
            f"({chr(ord('a') + index)})",
            fill="black",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_safe_id(figure_id)}.png"
    canvas.save(path, format="PNG", optimize=True)
    return path


def _numeric_list(value: Any) -> List[float]:
    if not isinstance(value, list):
        return []
    result: List[float] = []
    for item in value:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            return []
    return result


def _render_explanatory_data_visual(
    *,
    figure_id: str,
    request: Dict[str, Any],
    output_dir: Path,
) -> Optional[Path]:
    """Render a conservative plot when structured data are already supplied.

    Accepted input is intentionally small and auditable:
    ``{"series": [{"label": "...", "x": [...], "y": [...]}]}`` or
    ``{"categories": [...], "values": [...]}``.  Unknown schemas return None
    and fall through to the disclosed image-generation route.
    """

    data = request.get("input_data") or {}
    if not isinstance(data, dict) or not data:
        return None
    series: List[Dict[str, Any]] = []
    for row in data.get("series", []) or []:
        if not isinstance(row, dict):
            continue
        x_values = _numeric_list(row.get("x"))
        y_values = _numeric_list(row.get("y"))
        if x_values and len(x_values) == len(y_values):
            series.append(
                {
                    "label": str(row.get("label") or ""),
                    "x": x_values,
                    "y": y_values,
                }
            )
    if not series:
        values = _numeric_list(data.get("values"))
        categories = data.get("categories") or []
        if values and len(values) == len(categories):
            series = [
                {
                    "label": str(data.get("label") or ""),
                    "x": list(range(len(values))),
                    "y": values,
                    "categories": [str(value) for value in categories],
                }
            ]
    if not series:
        return None

    width, height = 1600, 1000
    left, right, top, bottom = 180, 90, 125, 185
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("arial.ttf", 34)
        label_font = ImageFont.truetype("arial.ttf", 25)
        tick_font = ImageFont.truetype("arial.ttf", 21)
        note_font = ImageFont.truetype("arial.ttf", 20)
    except OSError:
        title_font = ImageFont.load_default()
        label_font = title_font
        tick_font = title_font
        note_font = title_font
    all_x = [value for row in series for value in row["x"]]
    all_y = [value for row in series for value in row["y"]]
    x_min, x_max = min(all_x), max(all_x)
    y_min, y_max = min(all_y), max(all_y)
    if x_min == x_max:
        x_max = x_min + 1.0
    if y_min == y_max:
        y_max = y_min + 1.0
    y_pad = max((y_max - y_min) * 0.08, 1e-9)
    y_min -= y_pad
    y_max += y_pad

    def point(x_value: float, y_value: float) -> tuple[int, int]:
        px = left + int(
            (x_value - x_min) / (x_max - x_min)
            * (width - left - right)
        )
        py = top + int(
            (y_max - y_value) / (y_max - y_min)
            * (height - top - bottom)
        )
        return px, py

    plot_right = width - right
    plot_bottom = height - bottom
    for index in range(6):
        ratio = index / 5
        y_value = y_min + (y_max - y_min) * ratio
        y_pixel = point(x_min, y_value)[1]
        draw.line(
            [(left, y_pixel), (plot_right, y_pixel)],
            fill="#E5E7EB",
            width=2,
        )
        draw.text(
            (left - 105, y_pixel - 12),
            f"{y_value:.3g}",
            fill="#374151",
            font=tick_font,
        )
        x_value = x_min + (x_max - x_min) * ratio
        x_pixel = point(x_value, y_min)[0]
        draw.line(
            [(x_pixel, top), (x_pixel, plot_bottom)],
            fill="#F0F2F4",
            width=1,
        )
        draw.text(
            (x_pixel - 25, plot_bottom + 16),
            f"{x_value:.4g}",
            fill="#374151",
            font=tick_font,
        )
    draw.line(
        [(left, top), (left, plot_bottom), (plot_right, plot_bottom)],
        fill="#111827",
        width=4,
    )
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]
    for index, row in enumerate(series[:4]):
        points = [
            point(x_value, y_value)
            for x_value, y_value in zip(row["x"], row["y"])
        ]
        color = colors[index % len(colors)]
        if len(points) >= 2:
            draw.line(points, fill=color, width=6)
        for px, py in points:
            draw.ellipse(
                (px - 8, py - 8, px + 8, py + 8),
                fill=color,
            )
        label = row.get("label") or f"Series {index + 1}"
        draw.text(
            (left + 30, top + 16 + index * 42),
            str(label),
            fill=color,
            font=label_font,
        )
    title = str(
        request.get("title")
        or request.get("argumentative_purpose")
        or "Explanatory data visual"
    )
    draw.text(
        (left, 42),
        title[:150],
        fill="#111827",
        font=title_font,
    )
    x_label = str(data.get("x_label") or "Independent variable")
    y_label = str(data.get("y_label") or "Response")
    draw.text(
        (width // 2 - 130, plot_bottom + 85),
        x_label[:55],
        fill="#111827",
        font=label_font,
    )
    draw.text(
        (35, top - 50),
        y_label[:45],
        fill="#111827",
        font=label_font,
    )
    provenance = str(
        request.get("data_provenance_level") or "approximate"
    )
    disclosure = (
        "Data-derived visualization"
        if provenance == "exact"
        else "Approximate synthesis; not a unified-condition ranking"
    )
    draw.text(
        (left, height - 52),
        disclosure,
        fill="#555555",
        font=note_font,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_safe_id(figure_id)}.png"
    image.save(path, format="PNG", optimize=True)
    atomic_write_json(
        path.with_suffix(".provenance.json"),
        {
            "schema_version": "explanatory_data_visual.v1",
            "figure_id": figure_id,
            "rendering": "deterministic_pillow_plot",
            "data_provenance_level": provenance,
            "input_data": data,
            "disclosure": disclosure,
        },
    )
    return path


@dataclass
class VisualEvidenceFactoryConfig:
    output_dir: Path
    run_id: str = ""
    # Four conceptual requests need room for their generation/audit
    # reservations.  Three CNY left too little headroom after planning and
    # made the fourth request (and often all requests) fail closed before any
    # image-generation call.  Keep the library default aligned with the
    # production CLI's quality-first visual envelope.
    cost_budget_cny: float = 5.0
    # Global-only harness runs do not own a visual-stage allowance.  They pass
    # the live balance of the shared run ledger here solely so this factory
    # cannot overspend the run while it is the active stage.  This is a
    # balance snapshot, not a stage allocation; keep it separate from the
    # legacy ``cost_budget_cny`` compatibility field.
    global_budget_remaining_cny: Optional[float] = None
    real_visual_audit: bool = False
    real_image_generation: bool = False
    test_mode: bool = True
    human_timeout_seconds: int = 30
    vision_model_tier: str = "vision_plus_model"
    image_model: str = "qwen-image-2.0-pro"
    # Aligned with MAX_CONCEPTUAL_FIGURE_REQUESTS (4) in
    # visual_editor_tool_provider.  A cap of 2 against an editor allowed to
    # request 4 silently dropped the last two requests every run
    # (be780761: S04/S05 -> generation_task_budget_or_lower_priority) even
    # though the reviewer never objected to them.
    max_generated_images: int = 4
    max_generation_retries: int = MAX_GENERATION_TOTAL_ATTEMPTS - 1
    workers: int = 1
    image_generation_reference_cost_cny: float = 0.5
    shared_cache_dir: Optional[Path] = None
    # The orchestrator supplies a topic-scoped namespace.  Empty preserves
    # backwards-compatible behavior for isolated unit callers that provide
    # their own temporary cache directory.
    cache_namespace: str = ""
    # P2-1: when set, an approved-pending figure auto-accepts after this
    # many seconds via the human decision gate (default option "accept").
    # None means wait indefinitely outside pytest; under pytest the gate
    # itself hard-guards visual_review to zero-second auto-accept.
    visual_review_auto_accept_seconds: Optional[float] = None
    # Submission runs must not place a figure whose automated audit reports
    # fabricated empirical content, fake attribution, or a wrong trend.  The
    # default library/private-study mode retains the existing warning/salvage
    # behavior for compatibility with explanatory diagrams.
    execution_profile: str = "library_offline"

    def __post_init__(self) -> None:
        try:
            requested = int(self.max_generation_retries)
        except Exception:
            requested = MAX_GENERATION_TOTAL_ATTEMPTS - 1
        self.max_generation_retries = max(
            0,
            min(MAX_GENERATION_TOTAL_ATTEMPTS - 1, requested),
        )


class VisualEvidenceFactory:
    """Convert a visual editorial plan into one renderable visual package."""

    def __init__(
        self,
        config: VisualEvidenceFactoryConfig,
        *,
        vision_call: Callable[..., Dict[str, Any]] = call_qwen_vision,
        conceptual_generator_factory: Optional[
            Callable[..., ConceptualVisualGenerator]
        ] = None,
        diagram_renderer_factory: Optional[
            Callable[..., ConceptualDiagramRenderer]
        ] = None,
    ) -> None:
        self.config = config
        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.figure_dir = self.output_dir / "figures"
        self.figure_dir.mkdir(parents=True, exist_ok=True)
        self.shared_cache_dir = Path(
            config.shared_cache_dir or self.output_dir
        )
        self.shared_cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_namespace = str(config.cache_namespace or "").strip()
        if self.cache_namespace and not _CACHE_NAMESPACE_RE.fullmatch(
            self.cache_namespace
        ):
            self.cache_namespace = derive_visual_cache_namespace(
                explicit=self.cache_namespace
            )
        self.vision_call = vision_call
        self.conceptual_generator_factory = (
            conceptual_generator_factory or ConceptualVisualGenerator
        )
        self._conceptual_generator_factory_explicit = (
            conceptual_generator_factory is not None
        )
        self.diagram_renderer_factory = (
            diagram_renderer_factory or ConceptualDiagramRenderer
        )
        self._diagram_renderer_factory_explicit = (
            diagram_renderer_factory is not None
        )
        self.cost: Dict[str, Any] = {
            "schema_version": "research_harness.visual_cost.v1",
            "budget_policy": (
                "global_remaining_snapshot"
                if config.global_budget_remaining_cny is not None
                else "legacy_stage_cap"
            ),
            "estimated_cost_cny": 0.0,
            "vision_calls": 0,
            "vision_input_tokens": 0,
            "vision_output_tokens": 0,
            "diagram_spec_calls": 0,
            "diagram_spec_input_tokens": 0,
            "diagram_spec_output_tokens": 0,
            "diagram_spec_estimated_cost_cny": 0.0,
            "image_generation_calls": 0,
            "image_generation_reference_cost_cny": 0.0,
            "cache_hits": 0,
            "reserved_generation_cost_cny": 0.0,
            "source_figures": 0,
            "composite_figures": 0,
            "generated_figures": 0,
            "cache_namespace": self.cache_namespace,
        }
        if config.global_budget_remaining_cny is not None:
            self.cost["global_remaining_cny"] = round(
                max(0.0, float(config.global_budget_remaining_cny)),
                6,
            )
        else:
            # Preserve the old library contract for callers that explicitly
            # construct a local visual-stage cap.
            self.cost["budget_cny"] = float(config.cost_budget_cny)
        self.events: List[Dict[str, Any]] = []
        self._blueprint_sections: dict[str, Any] = {}
        self.audit_cache_snapshot_path = (
            self.output_dir / "VISUAL_AUDIT_CACHE.json"
        )
        self.audit_cache_path = (
            self.shared_cache_dir / "source_visual_audits.json"
        )
        self.audit_cache: Dict[str, Dict[str, Any]] = _read_json(
            self.audit_cache_path
        )
        if not self.audit_cache:
            self.audit_cache = _read_json(
                self.audit_cache_snapshot_path
            )
        self._lock = threading.RLock()
        self.reserved_generation_cost_cny = 0.0
        self.generation_reservations = 0

    def _bump_cost(self, **changes: float) -> None:
        """Atomic cost accounting for shared concurrent figure work."""

        with self._lock:
            for key, delta in changes.items():
                current = float(self.cost.get(key) or 0.0)
                self.cost[key] = round(current + float(delta), 6)

    def _generation_allowance(self) -> float:
        return (
            float(self.config.image_generation_reference_cost_cny)
            + self._generation_audit_reserve_cny()
        )

    def _structured_fallback_allowance(self) -> float:
        """Conservative allowance for one structured-diagram fallback.

        The fallback bills only the text-spec generation call and one
        vision audit; Graphviz rendering is local and free.  Reserving the
        raster reference cost here would skip affordable fallbacks without
        improving budget safety, so the allowance covers the two billable
        calls (plus a floor for unknown pricing).
        """

        try:
            from config.qwen_config import get_model_name

            model_name = str(
                get_model_name("standard_model") or ""
            ).strip()
        except Exception:
            model_name = ""
        spec_reserve = 0.0
        if model_name:
            try:
                spec_reserve = estimate_call_cost_cny(
                    model_name,
                    _STRUCTURED_SPEC_INPUT_RESERVE_TOKENS,
                    _STRUCTURED_SPEC_OUTPUT_RESERVE_TOKENS,
                )
            except Exception:
                spec_reserve = 0.0
        return round(
            max(_STRUCTURED_SPEC_RESERVE_CNY, spec_reserve)
            + self._generation_audit_reserve_cny(),
            6,
        )

    def _generation_audit_reserve_cny(self) -> float:
        try:
            from config.qwen_config import get_model_name

            model_name = str(
                get_model_name(self.config.vision_model_tier) or ""
            ).strip()
        except Exception:
            model_name = str(self.config.vision_model_tier or "").strip()
        try:
            estimated = estimate_call_cost_cny(
                model_name,
                _GENERATION_AUDIT_INPUT_RESERVE_TOKENS,
                _GENERATION_AUDIT_OUTPUT_RESERVE_TOKENS,
            )
        except Exception:
            estimated = 0.0
        return round(
            max(_GENERATION_AUDIT_RESERVE_CNY, float(estimated or 0.0)),
            6,
        )

    def _reserve_generation_allowance(self) -> bool:
        """Atomically check and reserve one generation allowance.

        The check and reservation are one lock-protected operation, so
        concurrent figure workers can never all pass a preflight check and
        then exceed the configured budget together.
        """

        return self._reserve_allowance(self._generation_allowance())

    def _reserve_fallback_allowance(self) -> bool:
        """Atomically reserve the smaller structured-fallback allowance."""

        return self._reserve_allowance(self._structured_fallback_allowance())

    def _reserve_allowance(self, allowance: float) -> bool:
        with self._lock:
            if self._remaining_budget() < allowance:
                return False
            self.reserved_generation_cost_cny += allowance
            self.generation_reservations += 1
            self.cost["reserved_generation_cost_cny"] = round(
                self.reserved_generation_cost_cny, 6
            )
            return True

    def _release_generation_allowance(self) -> None:
        """Release an unused or reconciled generation allowance."""

        self._release_allowance(self._generation_allowance())

    def _release_fallback_allowance(self) -> None:
        """Release an unused or reconciled structured-fallback allowance."""

        self._release_allowance(self._structured_fallback_allowance())

    def _release_allowance(self, allowance: float) -> None:
        with self._lock:
            self.reserved_generation_cost_cny = max(
                0.0,
                self.reserved_generation_cost_cny
                - allowance,
            )
            if self.reserved_generation_cost_cny < 1e-9:
                self.reserved_generation_cost_cny = 0.0
            self.cost["reserved_generation_cost_cny"] = round(
                self.reserved_generation_cost_cny, 6
            )

    def _persist_audit_cache(self) -> None:
        with self._lock:
            atomic_write_json(self.audit_cache_path, self.audit_cache)
            atomic_write_json(
                self.audit_cache_snapshot_path,
                self.audit_cache,
            )

    def _event(self, event: str, **details: Any) -> None:
        with self._lock:
            self.events.append(
                {"timestamp": _now(), "event": event, **details}
            )

    def _remaining_budget(self) -> float:
        with self._lock:
            ceiling = (
                self.config.global_budget_remaining_cny
                if self.config.global_budget_remaining_cny is not None
                else self.config.cost_budget_cny
            )
            return max(
                0.0,
                float(ceiling)
                - float(self.cost["estimated_cost_cny"])
                - float(self.reserved_generation_cost_cny),
            )

    def _audit_selected_source(
        self,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        status = str(item.get("status") or "")
        if status == "verified_existing":
            return _source_audit_fallback(
                item,
                reason="already_verified_existing",
            )
        image_path = Path(str(item.get("local_image_path") or ""))
        prompt = SOURCE_AUDITOR_PROMPT.read_text(encoding="utf-8")
        prompt_sha256 = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        section = self._blueprint_sections.get(
            str(item.get("section_id") or ""),
            {},
        )
        section_context = _source_audit_section_context(section, item)
        section_context_sha256 = hashlib.sha256(
            json.dumps(
                section_context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_key = build_source_audit_cache_key(
            image_sha256=_sha256_file(image_path),
            section_context=section_context,
            purpose=str(item.get("argumentative_purpose") or ""),
            caption_preview=str(
                item.get("source_caption")
                or item.get("caption_preview")
                or ""
            ),
            prompt_sha256=prompt_sha256,
            cache_namespace=self.cache_namespace,
        )
        cached = self.audit_cache.get(cache_key)
        if isinstance(cached, dict) and cached:
            cached_namespace = str(cached.get("cache_namespace") or "")
            if cached_namespace == self.cache_namespace:
                self._bump_cost(cache_hits=1)
                self._event(
                    "source_visual_audit_cache_hit",
                    visual_chunk_id=item.get("visual_chunk_id", ""),
                    section_id=section_context.get("section_id", ""),
                )
                return dict(cached)
            self._event(
                "source_visual_audit_cache_rejected",
                visual_chunk_id=item.get("visual_chunk_id", ""),
                section_id=section_context.get("section_id", ""),
                reason="cache_namespace_mismatch",
            )
        if (
            not self.config.real_visual_audit
            or self._remaining_budget() <= 0.02
        ):
            return _source_audit_fallback(
                item,
                reason=(
                    "test_mode_or_budgeted_shortlist_audit_skipped"
                ),
            )
        payload = {
            "argumentative_purpose": item.get("argumentative_purpose", ""),
            "source_caption": item.get("source_caption")
            or item.get("caption_preview", ""),
            "paper_id": item.get("paper_id", ""),
            "doi": item.get("doi", ""),
            "section_context": section_context,
            "task": (
                "Decide what this traceable source figure actually explains "
                "in the supplied target section context. Exact atomic-claim "
                "support is not required."
            ),
        }
        try:
            response = self.vision_call(
                "SourceVisualShortlistAuditor",
                prompt + "\n\nTASK JSON:\n"
                + json.dumps(payload, ensure_ascii=False),
                local_image_path=image_path,
                model_tier=self.config.vision_model_tier,
                max_retries=0,
                temperature=0,
                max_tokens=700,
                response_format={"type": "json_object"},
                force_mock=False,
                timeout_seconds=120,
                max_transport_key_candidates=1,
                allow_model_fallback=True,
            )
            usage = _usage_cost(
                response,
                self.config.vision_model_tier,
            )
            self._bump_cost(
                vision_calls=1,
                vision_input_tokens=float(usage["input_tokens"]),
                vision_output_tokens=float(usage["output_tokens"]),
                estimated_cost_cny=float(
                    usage["estimated_cost_cny"]
                ),
            )
            parsed = _safe_json_from_text(
                str(response.get("content") or "")
            )
            if not parsed:
                return _source_audit_fallback(
                    item,
                    reason="vision_audit_invalid_json_soft_fallback",
                )
            known_fields = (
                "verdict",
                "section_fit",
                "usefulness",
                "misleading_risk",
                "editorial_caption",
                "reason",
            )
            compact = {
                key: parsed.get(key)
                for key in known_fields
                if parsed.get(key) is not None
            }
            compact["audit_mode"] = "qwen_vision_shortlist"
            compact["section_id"] = section_context.get("section_id", "")
            compact["section_context_sha256"] = section_context_sha256
            compact["cache_namespace"] = self.cache_namespace
            with self._lock:
                self.audit_cache[cache_key] = dict(compact)
                self._persist_audit_cache()
            self._event(
                "source_visual_audited",
                visual_chunk_id=item.get("visual_chunk_id", ""),
                section_id=section_context.get("section_id", ""),
                verdict=compact.get("verdict", ""),
                section_fit=compact.get("section_fit", ""),
                estimated_cost_cny=usage["estimated_cost_cny"],
            )
            return compact
        except Exception as exc:
            return _source_audit_fallback(
                item,
                reason=f"vision_audit_failed_soft_fallback:{type(exc).__name__}",
            )

    def _source_figures(
        self,
        placements: Iterable[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for index, item in enumerate(placements, 1):
            if not isinstance(item, dict):
                continue
            source = Path(str(item.get("local_image_path") or ""))
            if (
                not source.is_file()
                or source.suffix.lower() not in SAFE_IMAGE_SUFFIXES
                or not item.get("paper_id")
            ):
                rejected.append(
                    {
                        "section_id": item.get("section_id", ""),
                        "visual_chunk_id": item.get(
                            "visual_chunk_id", ""
                        ),
                        "reason": "source_path_or_identity_invalid",
                    }
                )
                continue
            audit = self._audit_selected_source(item)
            verdict = str(audit.get("verdict") or "approve").lower()
            section_fit = str(
                audit.get("section_fit") or ""
            ).lower()
            # An explicit reject or an unrelated section fit is a placement
            # rejection regardless of the model's risk estimate: risk is
            # informational, never a gate that silently admits a reject.
            if verdict == "reject" or section_fit == "unrelated":
                rejected.append(
                    {
                        "section_id": item.get("section_id", ""),
                        "visual_chunk_id": item.get(
                            "visual_chunk_id", ""
                        ),
                        "reason": "selected_source_failed_shortlist_audit",
                        "audit": audit,
                        "section_context_sha256": str(
                            audit.get("section_context_sha256") or ""
                        ),
                    }
                )
                self._event(
                    "source_visual_rejected",
                    visual_chunk_id=item.get("visual_chunk_id", ""),
                    section_id=item.get("section_id", ""),
                    section_fit=section_fit,
                )
                continue
            figure_id = str(
                item.get("figure_id")
                or f"FIG-SRC-{index:03d}"
            )
            figure_contract = dict(item.get("figure_contract") or {})
            permission = dict(
                item.get("permission")
                or figure_contract.get("permission")
                or {}
            )
            accepted.append(
                {
                    "figure_id": figure_id,
                    "section_id": str(item.get("section_id") or ""),
                    "purpose": str(
                        item.get("argumentative_purpose") or ""
                    ),
                    "figure_type": "source_single",
                    "local_path": str(source.resolve()),
                    "placement_anchor": str(
                        item.get("placement_guidance")
                        or item.get("section_id")
                        or ""
                    ),
                    "panel_manifest": [
                        {
                            "panel_id": "a",
                            "visual_chunk_id": item.get(
                                "visual_chunk_id", ""
                            ),
                            "paper_id": item.get("paper_id", ""),
                            "doi": item.get("doi", ""),
                            "source_local_path": str(source.resolve()),
                            "image_sha256": _sha256_file(source),
                            "generated_status": "source_derived",
                            "target_section_ids": [
                                str(item.get("section_id") or "")
                            ],
                        }
                    ],
                    "caption_en": (
                        _valid_editorial_caption(
                            audit.get("editorial_caption")
                        )
                        or _fallback_caption_for_source(item)
                    ),
                    "source_caption": str(
                        item.get("source_caption")
                        or item.get("caption_preview")
                        or ""
                    ),
                    "source_attribution": dict(
                        item.get("source_attribution") or {}
                    ),
                    "source_map": dict(item.get("source_map") or {}),
                    "figure_contract": figure_contract,
                    "permission": permission,
                    "publication_eligible": bool(
                        item.get("publication_eligible")
                    ),
                    "publication_eligible_reason": str(
                        item.get("publication_eligible_reason") or ""
                    ),
                    "rights_notice": (
                        ""
                        if bool(item.get("publication_eligible"))
                        else (
                            "Internal-study use only; publication requires "
                            "explicit permission from the source rights "
                            "holder."
                        )
                    ),
                    "caption_zh_optional": "",
                    "source_route": "source_derived",
                    "data_provenance_level": "source_figure",
                    "generated_or_source": "source",
                    "section_context_sha256": str(
                        audit.get("section_context_sha256") or ""
                    ),
                    "review_decision": _review_decision(
                        test_mode=self.config.test_mode
                    ),
                    "review_flags": (
                        []
                        if status_is_verified(item)
                        else [
                            "selected_from_pending_multimodal_review"
                        ]
                    ),
                    "source_audit": audit,
                    "render_status": "ready",
                    "composite_group_id": str(
                        item.get("composite_group_id") or ""
                    ),
                    "panel_role": str(item.get("panel_role") or ""),
                }
            )
            self._bump_cost(source_figures=1)
            self._event(
                "source_visual_ready",
                figure_id=figure_id,
                section_id=item.get("section_id", ""),
            )
        return accepted, rejected

    def _compose_groups(
        self,
        source_figures: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        singles: List[Dict[str, Any]] = []
        for figure in source_figures:
            group_id = str(figure.get("composite_group_id") or "")
            if not group_id:
                singles.append(figure)
                continue
            groups.setdefault(group_id, []).append(figure)
        composites: List[Dict[str, Any]] = []
        for group_id, members in groups.items():
            if len(members) < 2:
                singles.extend(members)
                continue
            path = _compose_panels(
                figure_id=group_id,
                panels=members,
                output_dir=self.figure_dir / "composites",
            )
            if path is None:
                singles.extend(members)
                continue
            panel_manifest: List[Dict[str, Any]] = []
            caption_parts: List[str] = []
            section_ids: List[str] = []
            for index, member in enumerate(members):
                panel_id = chr(ord("a") + index)
                section_ids.append(str(member.get("section_id") or ""))
                for panel in member.get("panel_manifest", []) or []:
                    panel_manifest.append(
                        {**dict(panel), "panel_id": panel_id}
                    )
                caption_parts.append(
                    f"({panel_id}) {member.get('caption_en', '')}"
                )
            composites.append(
                {
                    "figure_id": group_id,
                    "section_id": next(
                        (value for value in section_ids if value),
                        "",
                    ),
                    "target_section_ids": list(
                        dict.fromkeys(
                            value for value in section_ids if value
                        )
                    ),
                    "purpose": " ".join(
                        str(row.get("purpose") or "")
                        for row in members
                    ).strip(),
                    "figure_type": "source_composite",
                    "local_path": str(path.resolve()),
                    "placement_anchor": next(
                        (
                            str(row.get("placement_anchor") or "")
                            for row in members
                            if row.get("placement_anchor")
                        ),
                        "",
                    ),
                    "panel_manifest": panel_manifest,
                    "caption_en": " ".join(caption_parts),
                    "source_captions": [
                        str(row.get("source_caption") or "")
                        for row in members
                        if str(row.get("source_caption") or "")
                    ],
                    "source_attributions": [
                        dict(row.get("source_attribution") or {})
                        for row in members
                        if isinstance(row.get("source_attribution"), dict)
                    ],
                    "source_maps": [
                        dict(row.get("source_map") or {})
                        for row in members
                        if isinstance(row.get("source_map"), dict)
                    ],
                    "figure_contracts": [
                        dict(row.get("figure_contract") or {})
                        for row in members
                        if isinstance(row.get("figure_contract"), dict)
                    ],
                    "caption_zh_optional": "",
                    "source_route": "source_derived",
                    "data_provenance_level": "source_figure",
                    "generated_or_source": "source",
                    "review_decision": _review_decision(
                        test_mode=self.config.test_mode
                    ),
                    "review_flags": [],
                    "render_status": "ready",
                }
            )
            self._bump_cost(composite_figures=1)
            self._event(
                "source_composite_ready",
                figure_id=group_id,
                panel_count=len(panel_manifest),
            )
        return singles + composites

    def _account_generation_result(
        self,
        result: Dict[str, Any],
    ) -> None:
        with self._lock:
            if result.get("generation_cache_hit"):
                self.cost["cache_hits"] += 1
            elif self.config.real_image_generation:
                self.cost["image_generation_calls"] += 1
                self.cost[
                    "image_generation_reference_cost_cny"
                ] += self.config.image_generation_reference_cost_cny
                self.cost["estimated_cost_cny"] += (
                    self.config.image_generation_reference_cost_cny
                )
            audit_usage = dict(result.get("model_review_usage") or {})
            if audit_usage:
                audit_cost = _usage_cost(
                    {"_llm_usage": audit_usage},
                    self.config.vision_model_tier,
                )
                self.cost["vision_calls"] += 1
                self.cost["vision_input_tokens"] += audit_cost[
                    "input_tokens"
                ]
                self.cost["vision_output_tokens"] += audit_cost[
                    "output_tokens"
                ]
                self.cost["estimated_cost_cny"] += audit_cost[
                    "estimated_cost_cny"
                ]

    @staticmethod
    def _generated_visual_has_technical_failure(
        result: Dict[str, Any],
    ) -> bool:
        review = dict(result.get("model_review") or {})
        return bool(
            review.get("contains_fabricated_empirical_content")
            or review.get("fake_paper_attribution")
            or review.get("trend_direction_correct") is False
            or str(review.get("label_legibility") or "").lower()
            == "low"
            or str(review.get("scientific_coherence") or "").lower()
            == "low"
            or str(review.get("verdict") or "").lower() == "reject"
        )

    GENERATION_FAILED_STATUSES = frozenset(
        {
            "generation_failed",
            "download_failed",
            "structured_fallback_failed",
            "image_generation_skipped_by_budget",
        }
    )

    @staticmethod
    def _generated_visual_integrity_flags(
        result: Dict[str, Any],
    ) -> List[str]:
        """Reviewer objections that impugn the figure's scientific content.

        These are the subset of technical-failure signals a reader must be
        warned about explicitly: the figure may assert something the
        literature does not support.  Legibility and coherence complaints are
        deliberately excluded -- they degrade usefulness, not truthfulness,
        and the caption already discloses the AI-generated route.  Exclusion
        from *this* list is not permission to place: illegibility blocks
        placement outright via ``_generated_visual_is_illegible`` below.
        """

        review = dict(result.get("model_review") or {})
        flags: List[str] = []
        if review.get("contains_fabricated_empirical_content"):
            flags.append("fabricated_empirical_content")
        if review.get("fake_paper_attribution"):
            flags.append("fake_paper_attribution")
        if review.get("trend_direction_correct") is False:
            flags.append("incorrect_trend_direction")
        return flags

    @staticmethod
    def _generated_visual_is_illegible(
        result: Dict[str, Any],
    ) -> bool:
        """Whether the figure carries no readable information at all.

        This is deliberately *not* an integrity flag.  Integrity flags mark a
        figure that asserts something untrue, and they travel into the caption
        as a warning the reader can act on.  Illegibility is a different
        failure: the figure asserts nothing, so there is nothing to warn
        about -- and the caption, which describes what the figure is supposed
        to show, becomes an unbacked claim the image does not support.

        The private-study relaxation above was scoped to traceability and
        empirical grounding: an untraceable figure can still be a correct,
        useful figure.  An illegible one cannot serve any content, so it falls
        outside that relaxation and must not reach placement.  Dropping it is
        safe by policy -- ``missing_figure_invalidates_text`` is False, so the
        section keeps its validated text and simply carries no figure.

        Threshold and field name are reused verbatim from
        ``_generated_visual_has_technical_failure``; this introduces no new
        review vocabulary and no new cutoff.
        """

        review = dict(result.get("model_review") or {})
        return (
            str(review.get("label_legibility") or "").lower() == "low"
        )

    @staticmethod
    def _generated_visual_requires_revision(
        result: Dict[str, Any],
    ) -> bool:
        """Whether the attempt should be retried after review.

        Retry covers failed transport/materialization statuses and model
        reject/revise verdicts.  ``needs_human_review`` is not an automatic
        retry trigger: the human decision path should remain untouched.
        """

        status = str(result.get("generation_status") or "").lower()
        if status in VisualEvidenceFactory.GENERATION_FAILED_STATUSES:
            return True
        if status == "model_rejected_or_revision_required":
            review = dict(result.get("model_review") or {})
            verdict = str(review.get("verdict") or "").lower()
            return verdict != "needs_human_review"
        return VisualEvidenceFactory._generated_visual_has_technical_failure(
            result
        )

    @staticmethod
    def _review_feedback_text(review: Dict[str, Any]) -> str:
        """Extract actionable reviewer feedback for the retry prompt."""

        parts: List[str] = []
        for key in (
            "required_revisions",
            "misleading_elements",
            "reviewer_feedback",
            "feedback",
        ):
            value = review.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, list):
                parts.extend(
                    str(item).strip()
                    for item in value
                    if str(item).strip()
                )
        verdict = str(review.get("verdict") or "").lower()
        if verdict and verdict not in {"approve", "approved", ""}:
            parts.append(f"Review verdict: {verdict}")
        if str(review.get("label_legibility") or "").lower() == "low":
            parts.append("Improve label legibility.")
        if review.get("trend_direction_correct") is False:
            parts.append(
                "Correct the trend direction to match the supplied data."
            )
        if review.get("contains_fabricated_empirical_content"):
            parts.append(
                "Remove fabricated empirical content; show only "
                "explanatory relationships."
            )
        return " | ".join(
            dict.fromkeys(part for part in parts if part)
        )

    @staticmethod
    def _build_generation_revision_brief(
        plan: Dict[str, Any],
        review: Dict[str, Any],
    ) -> str:
        """Build a retry brief from actual reviewer feedback when available."""

        base = str(plan.get("generation_brief") or "")
        feedback = VisualEvidenceFactory._review_feedback_text(review)
        if feedback:
            revision = (
                "Revise the scientific figure to address the reviewer "
                "feedback: " + feedback
            )
        else:
            revision = (
                "Redraw the scientific figure from scratch, correcting the "
                "issues identified in review."
            )
        constraints = (
            "Do not add a title, paragraphs, metadata, prompt wording, or "
            "instructions. Use at most six short labels, keep every label "
            "correctly spelled and legible, and never invent data, spectra, "
            "measured values, or citations."
        )
        return " ".join(
            part for part in (base, revision, constraints) if part
        ).strip()

    def _account_structured_spec_usage(
        self,
        usage: Dict[str, Any],
    ) -> None:
        if not usage:
            return
        with self._lock:
            call_cost = _usage_cost(
                {"_llm_usage": usage},
                "standard_model",
            )
            self.cost["diagram_spec_calls"] += 1
            self.cost["diagram_spec_input_tokens"] += call_cost[
                "input_tokens"
            ]
            self.cost["diagram_spec_output_tokens"] += call_cost[
                "output_tokens"
            ]
            self.cost["diagram_spec_estimated_cost_cny"] += call_cost[
                "estimated_cost_cny"
            ]
            self.cost["estimated_cost_cny"] += call_cost[
                "estimated_cost_cny"
            ]

    def _account_structured_vision_usage(
        self,
        usage: Dict[str, Any],
    ) -> None:
        if not usage:
            return
        with self._lock:
            call_cost = _usage_cost(
                {"_llm_usage": usage},
                self.config.vision_model_tier,
            )
            self.cost["vision_calls"] += 1
            self.cost["vision_input_tokens"] += call_cost["input_tokens"]
            self.cost["vision_output_tokens"] += call_cost["output_tokens"]
            self.cost["estimated_cost_cny"] += call_cost[
                "estimated_cost_cny"
            ]

    def _audit_structured_image(
        self,
        image_path: Path,
        *,
        plan: Dict[str, Any],
        section: Dict[str, Any],
    ) -> Dict[str, Any]:
        audit_payload = {
            "argument_role": plan.get("argument_role", ""),
            "section_title": section.get(
                "section_title",
                section.get("title", ""),
            ),
            "figure_kind": plan.get("figure_kind", "concept_map"),
            "generation_brief": plan.get("generation_brief", ""),
            "data_provenance_level": plan.get(
                "data_provenance_level",
                "schematic",
            ),
            "input_data": plan.get("input_data") or {},
            "required_label": "AI-assisted explanatory diagram",
        }
        try:
            response = self.vision_call(
                "StructuredConceptualFigureAuditor",
                AUDITOR_PROMPT.read_text(encoding="utf-8")
                + "\n\nTASK JSON:\n"
                + json.dumps(audit_payload, ensure_ascii=False),
                local_image_path=str(image_path),
                model_tier=self.config.vision_model_tier,
                max_retries=0,
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
                force_mock=not self.config.real_image_generation,
                timeout_seconds=120,
                max_transport_key_candidates=1,
                allow_model_fallback=True,
            )
            audit = _safe_json_from_text(
                str(response.get("content") or "")
            )
            usage = dict(response.get("_llm_usage") or {})
        except Exception as exc:
            audit = {
                "verdict": "needs_human_review",
                "scientific_coherence": "medium",
                "label_legibility": "high",
                "trend_direction_correct": True,
                "misleading_elements": [
                    f"Vision audit unavailable: {type(exc).__name__}"
                ],
            }
            usage = {}
        return {
            "audit": audit,
            "usage": usage,
            "vision_used": bool(usage),
        }

    @staticmethod
    def _structured_review_requires_revision(
        review: Dict[str, Any],
    ) -> bool:
        verdict = str(review.get("verdict") or "").lower()
        if verdict in {"revise", "reject"}:
            return True
        return bool(
            review.get("contains_fabricated_empirical_content")
            or review.get("fake_paper_attribution")
            or review.get("trend_direction_correct") is False
            or str(review.get("label_legibility") or "").lower()
            == "low"
            or str(review.get("scientific_coherence") or "").lower()
            == "low"
        )

    def _structured_result(
        self,
        *,
        plan: Dict[str, Any],
        rendered: Dict[str, Any],
        review: Dict[str, Any],
        audit_result: Dict[str, Any],
        attempts: List[Dict[str, Any]],
        approved: bool,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            **dict(plan),
            "generation_status": (
                "model_approved_human_pending"
                if approved
                else "model_rejected_or_revision_required"
            ),
            "generation_model_used": "qwen_text_spec_plus_graphviz",
            "source_label": "AI-assisted explanatory diagram",
            "evidence_status": "explanatory_visual_not_paper_original",
            "model_review": review,
            "model_review_usage": dict(
                audit_result.get("usage") or {}
            ),
            "model_review_vision_used": bool(
                audit_result.get("vision_used")
            ),
            "needs_human_review": (
                not approved
                or str(review.get("verdict") or "").lower()
                == "needs_human_review"
            ),
            "structured_route": True,
            "structured_attempts": attempts,
            "generation_total_attempts": len(attempts),
        }
        if approved:
            result["local_image_path"] = str(
                rendered.get("local_image_path") or ""
            )
            result["provenance_path"] = str(
                rendered.get("provenance_path") or ""
            )
            result["structured_spec"] = dict(
                rendered.get("spec") or {}
            )
            return result
        result["generation_attempts_exhausted"] = True
        last_status = (
            str(attempts[-1].get("status") or "")
            if attempts
            else ""
        )
        # Reader-explanation salvage: an unapproved attempt that still
        # rendered is carried forward instead of discarded.  Withholding the
        # path here made the placement gate structurally unable to consider a
        # finished image -- the be780761 run rendered three S01 attempts and
        # threw all three away.  Carrying the path does not place the figure;
        # the gate below still decides, and every reviewer objection travels
        # with the result for caption disclosure.
        if last_status == "ready":
            result["local_image_path"] = str(
                rendered.get("local_image_path") or ""
            )
            result["provenance_path"] = str(
                rendered.get("provenance_path") or ""
            )
            result["structured_spec"] = dict(
                rendered.get("spec") or {}
            )
            result["generation_salvaged_last_attempt"] = True
        if last_status != "ready":
            result["generation_retry_stop_reason"] = (
                "structured_render_failed"
            )
        elif len(attempts) < MAX_GENERATION_TOTAL_ATTEMPTS:
            result["generation_retry_stop_reason"] = (
                "reviewer_feedback_unavailable"
            )
        else:
            result["generation_retry_stop_reason"] = (
                "attempts_exhausted"
            )
        result["generation_error"] = str(
            rendered.get("error")
            or rendered.get("status")
            or "structured_diagram_not_approved"
        )
        return result

    def _structured_diagram_route(
        self,
        *,
        plan: Dict[str, Any],
        section: Dict[str, Any],
        plan_id: str,
    ) -> Dict[str, Any]:
        """Run the text-spec + Graphviz route with bounded semantic revision.

        The first attempt generates a semantic spec; reviewer feedback is fed
        back into ``revise_spec`` for at most three total attempts.  Raster
        image generation is never called from this route.
        """

        renderer = self.diagram_renderer_factory(
            output_dir=self.shared_cache_dir / "structured_diagrams",
            real_llm=self.config.real_image_generation,
            model_tier="standard_model",
        )
        attempts: List[Dict[str, Any]] = []
        feedback = ""
        previous_spec: Optional[Dict[str, Any]] = None
        review: Dict[str, Any] = {}
        audit_result: Dict[str, Any] = {
            "audit": {},
            "usage": {},
            "vision_used": False,
        }
        rendered: Dict[str, Any] = {}
        for attempt_number in range(
            1,
            MAX_GENERATION_TOTAL_ATTEMPTS + 1,
        ):
            if attempt_number == 1:
                rendered = renderer.render(
                    plan=plan,
                    section=section,
                    figure_id=plan_id,
                )
            elif feedback and previous_spec:
                rendered = renderer.revise_spec(
                    previous_spec=previous_spec,
                    reviewer_feedback=feedback,
                    plan=plan,
                    section=section,
                    figure_id=f"{plan_id}_rev{attempt_number - 1}",
                    revision=attempt_number - 1,
                )
            else:
                rendered = renderer.render(
                    plan=plan,
                    section=section,
                    figure_id=f"{plan_id}_retry{attempt_number - 1}",
                )
            usage = dict(rendered.get("model_usage") or {})
            self._account_structured_spec_usage(usage)
            attempt: Dict[str, Any] = {
                "attempt_number": attempt_number,
                "status": str(rendered.get("status") or "unknown"),
                "spec_origin": str(
                    rendered.get("spec_origin")
                    or (
                        "revision"
                        if attempt_number > 1
                        else "generated"
                    )
                ),
                "spec": rendered.get("spec"),
                "model_usage": usage,
                "review": {},
                "reviewer_feedback": feedback,
                "local_image_path": str(
                    rendered.get("local_image_path") or ""
                ),
                "provenance_path": str(
                    rendered.get("provenance_path") or ""
                ),
            }
            attempts.append(attempt)
            if str(rendered.get("status") or "") != "ready":
                if (
                    isinstance(rendered.get("spec"), dict)
                    and rendered["spec"]
                ):
                    previous_spec = rendered["spec"]
                continue
            previous_spec = rendered.get("spec")
            audit_result = self._audit_structured_image(
                Path(str(rendered.get("local_image_path") or "")),
                plan=plan,
                section=section,
            )
            self._account_structured_vision_usage(
                audit_result.get("usage") or {}
            )
            review = dict(audit_result.get("audit") or {})
            attempt["review"] = review
            if not self._structured_review_requires_revision(review):
                return self._structured_result(
                    plan=plan,
                    rendered=rendered,
                    review=review,
                    audit_result=audit_result,
                    attempts=attempts,
                    approved=True,
                )
            feedback = self._review_feedback_text(review)
            attempt["reviewer_feedback"] = feedback
            if not feedback:
                break
        return self._structured_result(
            plan=plan,
            rendered=rendered,
            review=review,
            audit_result=audit_result,
            attempts=attempts,
            approved=False,
        )

    def _render_structured_diagram_fallback(
        self,
        *,
        plan: Dict[str, Any],
        section: Dict[str, Any],
        figure_id: str,
    ) -> Dict[str, Any]:
        """Use Qwen for structure and Graphviz for reliable spelling/layout."""

        cache_payload = {
            "schema_version": "structured_diagram_cache.v1",
            "cache_namespace": self.cache_namespace,
            "plan": plan,
            "section": {
                "section_id": section.get("section_id", ""),
                "title": section.get(
                    "section_title",
                    section.get("title", ""),
                ),
                "argument": section.get(
                    "chapter_argument",
                    section.get("argument_role", ""),
                ),
            },
            "prompt_sha256": hashlib.sha256(
                (
                    PROJECT_ROOT
                    / "prompts"
                    / "Conceptual Diagram Spec Generator.txt"
                ).read_bytes()
            ).hexdigest(),
            "model_tier": "standard_model",
        }
        cache_key = hashlib.sha256(
            json.dumps(
                cache_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cache_dir = (
            self.shared_cache_dir
            / "structured_diagrams"
            / cache_key
        )
        cached_manifest = _read_json(cache_dir / "manifest.json")
        cached_image = cache_dir / "diagram.png"
        cached_provenance = cache_dir / "diagram.provenance.json"
        if (
            cached_manifest.get("status") == "ready"
            and cached_image.is_file()
            and cached_provenance.is_file()
        ):
            current_dir = self.figure_dir / "structured_diagrams"
            current_dir.mkdir(parents=True, exist_ok=True)
            stem = _safe_id(figure_id)
            current_image = current_dir / f"{stem}.png"
            current_provenance = (
                current_dir / f"{stem}.provenance.json"
            )
            shutil.copy2(cached_image, current_image)
            shutil.copy2(cached_provenance, current_provenance)
            self._bump_cost(cache_hits=1)
            self._event(
                "structured_diagram_cache_hit",
                figure_id=figure_id,
                cache_key=cache_key,
            )
            return {
                **dict(plan),
                "generation_status": "model_approved_human_pending",
                "generation_model_used": (
                    "qwen_text_spec_plus_graphviz_cache"
                ),
                "local_image_path": str(current_image),
                "provenance_path": str(current_provenance),
                "source_label": "AI-assisted explanatory diagram",
                "evidence_status": (
                    "explanatory_visual_not_paper_original"
                ),
                "model_review": dict(
                    cached_manifest.get("model_review") or {}
                ),
                "model_review_usage": {},
                "model_review_vision_used": bool(
                    cached_manifest.get(
                        "model_review_vision_used",
                        False,
                    )
                ),
                "structured_fallback": dict(
                    cached_manifest.get("structured_fallback") or {}
                ),
                "generation_cache_hit": True,
                "needs_human_review": True,
            }
        renderer = self.diagram_renderer_factory(
            output_dir=self.figure_dir / "structured_diagrams",
            real_llm=self.config.real_image_generation,
            model_tier="standard_model",
        )
        rendered = renderer.render(
            plan=plan,
            section=section,
            figure_id=figure_id,
        )
        usage = dict(rendered.get("model_usage") or {})
        if usage:
            call_cost = _usage_cost(
                {"_llm_usage": usage},
                "standard_model",
            )
            self._bump_cost(
                diagram_spec_calls=1,
                diagram_spec_input_tokens=call_cost["input_tokens"],
                diagram_spec_output_tokens=call_cost["output_tokens"],
                diagram_spec_estimated_cost_cny=call_cost[
                    "estimated_cost_cny"
                ],
                estimated_cost_cny=call_cost["estimated_cost_cny"],
            )
        path = Path(str(rendered.get("local_image_path") or ""))
        if rendered.get("status") != "ready" or not path.is_file():
            return {
                "generation_status": "structured_fallback_failed",
                "generation_error": rendered.get("status", ""),
                "structured_fallback": rendered,
            }
        audit_payload = {
            "argument_role": plan.get("argument_role", ""),
            "section_title": section.get(
                "section_title",
                section.get("title", ""),
            ),
            "figure_kind": plan.get(
                "figure_kind",
                "concept_map",
            ),
            "generation_brief": plan.get("generation_brief", ""),
            "data_provenance_level": plan.get(
                "data_provenance_level",
                "schematic",
            ),
            "input_data": plan.get("input_data") or {},
            "required_label": "AI-assisted explanatory diagram",
        }
        try:
            audit_response = self.vision_call(
                "StructuredConceptualFigureAuditor",
                AUDITOR_PROMPT.read_text(encoding="utf-8")
                + "\n\nTASK JSON:\n"
                + json.dumps(audit_payload, ensure_ascii=False),
                local_image_path=path,
                model_tier=self.config.vision_model_tier,
                max_retries=0,
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
                force_mock=not self.config.real_image_generation,
                timeout_seconds=120,
                max_transport_key_candidates=1,
                allow_model_fallback=True,
            )
            audit = _safe_json_from_text(
                str(audit_response.get("content") or "")
            )
            audit_usage = dict(
                audit_response.get("_llm_usage") or {}
            )
        except Exception as exc:
            audit = {
                "verdict": "needs_human_review",
                "scientific_coherence": "medium",
                "label_legibility": "high",
                "trend_direction_correct": True,
                "misleading_elements": [
                    f"Vision audit unavailable: {type(exc).__name__}"
                ],
            }
            audit_usage = {}
        result = {
            **dict(plan),
            "generation_status": "model_approved_human_pending",
            "generation_model_used": (
                "qwen_text_spec_plus_graphviz"
            ),
            "local_image_path": str(path),
            "provenance_path": str(
                rendered.get("provenance_path") or ""
            ),
            "source_label": "AI-assisted explanatory diagram",
            "evidence_status": (
                "explanatory_visual_not_paper_original"
            ),
            "model_review": audit,
            "model_review_usage": audit_usage,
            "model_review_vision_used": bool(audit_usage),
            "structured_fallback": rendered,
            "needs_human_review": True,
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, cached_image)
        provenance_path = Path(
            str(rendered.get("provenance_path") or "")
        )
        if provenance_path.is_file():
            shutil.copy2(provenance_path, cached_provenance)
        else:
            atomic_write_json(
                cached_provenance,
                {
                    "schema_version": "conceptual_diagram.v1",
                    "source": "structured_diagram_cache",
                },
            )
        atomic_write_json(
            cache_dir / "manifest.json",
            {
                "status": "ready",
                "cache_key": cache_key,
                "cache_namespace": self.cache_namespace,
                "model_review": audit,
                "model_review_vision_used": bool(audit_usage),
                "structured_fallback": rendered,
            },
        )
        return result

    def _generated_figures(
        self,
        requests: Iterable[Dict[str, Any]],
        *,
        blueprint: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        generated: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        if self.config.max_generated_images <= 0:
            return generated, [dict(row) for row in requests]
        sections = {
            str(row.get("section_id") or ""): row
            for row in blueprint.get("sections", []) or []
            if isinstance(row, dict)
        }
        ordered = list(
            _prioritized_generation_order(
                requests,
                self.config.max_generated_images,
            )
        )
        selected = ordered[: self.config.max_generated_images]
        overflow_unresolved: List[Dict[str, Any]] = []
        # P1-3: the cap silently absorbed these requests before -- the
        # accounting was honest but nobody was ever told about the gap
        # between planned requests and max_generated_images.  Emit one event
        # per overflowed request so the deficit is visible in the event log.
        total_generation_requests = len(ordered)
        generation_cap = int(self.config.max_generated_images)
        for index, request in ordered[generation_cap:]:
            overflow_unresolved.append(
                {
                    **dict(request),
                    "reason": "generation_task_budget_or_lower_priority",
                }
            )
            self._event(
                "conceptual_visual_generation_overflow",
                visual_plan_id=str(
                    request.get("visual_plan_id")
                    or request.get("figure_id")
                    or ""
                ),
                section_id=str(request.get("section_id") or ""),
                figure_kind=str(request.get("figure_kind") or ""),
                reason="generation_task_budget_or_lower_priority",
                total_conceptual_requests=total_generation_requests,
                max_generated_images=generation_cap,
            )
        workers = (
            max(1, min(int(self.config.workers), len(selected)))
            if selected
            else 1
        )
        if workers == 1:
            results = []
            for index, request in selected:
                try:
                    results.append(
                        self._process_one_generation(
                            index, request, sections
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one figure fails open
                    results.append(
                        self._generation_worker_failure(
                            index, request, exc
                        )
                    )
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="visual-gen",
            ) as pool:
                futures = {
                    pool.submit(
                        self._process_one_generation,
                        index,
                        request,
                        sections,
                    ): (index, request)
                    for index, request in selected
                }
                by_index = {}
                for future in as_completed(futures):
                    index, request = futures[future]
                    try:
                        by_index[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - one figure fails open
                        by_index[index] = self._generation_worker_failure(
                            index, request, exc
                        )
            results = [by_index[index] for index, _ in selected]
        for local_generated, local_unresolved in results:
            generated.extend(local_generated)
            unresolved.extend(local_unresolved)
        unresolved.extend(overflow_unresolved)
        return generated, unresolved

    def _generation_worker_failure(
        self,
        index: int,
        request: Dict[str, Any],
        exc: BaseException,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        plan_id = str(
            request.get("visual_plan_id")
            or f"FIG-GEN-{index + 1:03d}"
        )
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
        self._event(
            "generated_visual_worker_failed",
            visual_plan_id=plan_id,
            error=error,
        )
        return [], [
            {
                **dict(request),
                "reason": "generation_worker_exception",
                "generation_error": error,
            }
        ]

    def _process_one_generation(
        self,
        index: int,
        request: Dict[str, Any],
        sections: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Process one prioritized figure: generation -> audit -> retry.

        Called from the bounded worker pool in ``_generated_figures``; the
        whole per-figure sequence stays serial. Cost and audit-cache
        mutations are lock-guarded; the figure ID is derived from the
        priority index so results are deterministic regardless of finish
        order.
        """

        local_generated: List[Dict[str, Any]] = []
        local_unresolved: List[Dict[str, Any]] = []
        plan_id = str(
            request.get("visual_plan_id")
            or f"FIG-GEN-{index + 1:03d}"
        )
        provenance_level = str(
            request.get("data_provenance_level") or "schematic"
        )
        if (
            provenance_level in {"exact", "approximate"}
            and request.get("input_data")
        ):
            data_path = _render_explanatory_data_visual(
                figure_id=plan_id,
                request=request,
                output_dir=self.figure_dir / "data_visuals",
            )
            if data_path is not None:
                disclosure = (
                    "Data-derived visualization."
                    if provenance_level == "exact"
                    else (
                        "Approximate synthesis; not a "
                        "unified-condition ranking."
                    )
                )
                local_generated.append(
                    {
                        "figure_id": plan_id,
                        "section_id": str(
                            request.get("section_id") or ""
                        ),
                        "purpose": str(
                            request.get(
                                "argumentative_purpose"
                            )
                            or ""
                        ),
                        "figure_type": "explanatory_data_visual",
                        "local_path": str(data_path.resolve()),
                        "placement_anchor": str(
                            request.get("placement_guidance")
                            or request.get("section_id")
                            or ""
                        ),
                        "panel_manifest": [],
                        "caption_en": (
                            str(
                                request.get(
                                    "argumentative_purpose"
                                )
                                or ""
                            )
                            + " "
                            + disclosure
                        ).strip(),
                        "caption_zh_optional": "",
                        "source_route": (
                            "explanatory_data_visual"
                        ),
                        "data_provenance_level": provenance_level,
                        "generated_or_source": "deterministic_plot",
                        "review_decision": _review_decision(
                            test_mode=self.config.test_mode
                        ),
                        "review_flags": [],
                        "render_status": "ready",
                    }
                )
                self._bump_cost(generated_figures=1)
                self._event(
                    "explanatory_data_visual_ready",
                    figure_id=plan_id,
                    data_provenance_level=provenance_level,
                    estimated_cost_cny=0.0,
                )
                return local_generated, local_unresolved
        generator = self.conceptual_generator_factory(
            output_dir=self.shared_cache_dir / "generated_images",
            model=self.config.image_model,
            vision_model_tier=self.config.vision_model_tier,
            real_llm=self.config.real_image_generation,
        )
        plan = {
            **dict(request),
            "visual_plan_id": plan_id,
            "creation_class": (
                "author_synthesized_conceptual_schematic"
            ),
            "argument_role": str(
                request.get("argumentative_purpose") or ""
            ),
            "prohibited_use": (
                "Do not attribute this image to a paper or present it as "
                "measured empirical evidence."
            ),
        }
        custom_generator_seam = bool(
            self._conceptual_generator_factory_explicit
            and not self._diagram_renderer_factory_explicit
        )
        route = _resolve_generation_route(
            request,
            custom_generator_seam=custom_generator_seam,
        )
        structured_route = bool(
            route == "structured_diagram"
            and self.config.real_image_generation
        )
        if structured_route:
            if not self._reserve_generation_allowance():
                result = {
                    **plan,
                    "generation_status": (
                        "structured_generation_skipped_by_budget"
                    ),
                    "model_review": {},
                    "structured_route": True,
                }
            else:
                try:
                    result = self._structured_diagram_route(
                        plan=plan,
                        section=sections.get(
                            str(request.get("section_id") or ""),
                            {},
                        ),
                        plan_id=plan_id,
                    )
                finally:
                    self._release_generation_allowance()
        elif self._reserve_generation_allowance():
            try:
                result = generator.generate(
                    plan=plan,
                    section=sections.get(
                        str(request.get("section_id") or ""),
                        {},
                    ),
                )
                self._account_generation_result(result)
            finally:
                self._release_generation_allowance()
        else:
            result = {
                **plan,
                "generation_status": (
                    "image_generation_skipped_by_budget"
                ),
                "model_review": {},
            }
        retry_history: List[Dict[str, Any]] = []
        if not structured_route:
            max_retries = max(
                0,
                int(self.config.max_generation_retries),
            )
            for retry_index in range(max_retries):
                if (
                    not self.config.real_image_generation
                    or not self._generated_visual_requires_revision(
                        result
                    )
                    or not self._reserve_generation_allowance()
                ):
                    break
                review = dict(result.get("model_review") or {})
                retry_history.append(
                    {
                        "generation_status": result.get(
                            "generation_status",
                            "",
                        ),
                        "model_review": review,
                        "local_image_path": result.get(
                            "local_image_path",
                            "",
                        ),
                    }
                )
                revision_brief = self._build_generation_revision_brief(
                    plan=plan,
                    review=review,
                )
                retry_plan = {
                    **plan,
                    "visual_plan_id": (
                        f"{plan_id}_retry{retry_index + 1}"
                    ),
                    "generation_brief": revision_brief,
                }
                self._event(
                    "generation_retry",
                    visual_plan_id=plan_id,
                    retry_index=retry_index + 1,
                    generation_brief=revision_brief,
                )
                try:
                    result = generator.generate(
                        plan=retry_plan,
                        section=sections.get(
                            str(request.get("section_id") or ""),
                            {},
                        ),
                    )
                    self._account_generation_result(result)
                except Exception as exc:
                    # Preserve the previous attempt, its review history and
                    # the spend already recorded for it; fail open with an
                    # explicit retry error instead of discarding the figure.
                    retry_error = (
                        f"{type(exc).__name__}: {str(exc)[:240]}"
                    )
                    if retry_history:
                        retry_history[-1]["retry_error"] = retry_error
                    result["generation_retry_error"] = retry_error
                    self._event(
                        "generation_retry_failed",
                        visual_plan_id=plan_id,
                        retry_index=retry_index + 1,
                        error=retry_error,
                    )
                    break
                finally:
                    self._release_generation_allowance()
            if retry_history:
                result["retry_history"] = retry_history
            result["generation_total_attempts"] = (
                len(retry_history) + 1
            )
            if (
                retry_history
                and self._generated_visual_requires_revision(result)
            ):
                result["generation_attempts_exhausted"] = True
                result["generation_retry_stop_reason"] = (
                    "budget_exhausted"
                    if self._remaining_budget()
                    < self._generation_allowance()
                    else (
                        "retry_call_failed"
                        if result.get("generation_retry_error")
                        else "attempts_exhausted"
                    )
                )
        image_status = str(
            result.get("generation_status") or ""
        )
        attempts_exhausted = bool(
            result.get("generation_attempts_exhausted")
        )
        # Submission mode treats scientific-integrity flags as terminal for
        # this request.  A fallback may be useful for ordinary rendering
        # failures, but it must not erase an audited fabrication/attribution/
        # trend finding before the placement gate sees it.
        submission_integrity_rejection = (
            str(self.config.execution_profile or "").strip().lower()
            == "submission"
            and bool(self._generated_visual_integrity_flags(result))
        )
        needs_structured_fallback = bool(
            self.config.real_image_generation
            and not submission_integrity_rejection
            and not attempts_exhausted
            and not result.get("structured_route")
            and (
                image_status
                not in {
                    "model_approved_human_pending",
                    "model_rejected_or_revision_required",
                }
                or self._generated_visual_has_technical_failure(
                    result
                )
            )
        )
        if (
            needs_structured_fallback
            and self._remaining_budget()
            < self._structured_fallback_allowance()
        ):
            local_unresolved.append(
                {
                    **dict(request),
                    "reason": (
                        "visual_budget_exhausted_before_"
                        "structured_fallback"
                    ),
                }
            )
            self._event(
                "structured_diagram_fallback_skipped_by_budget",
                visual_plan_id=plan_id,
                remaining_budget_cny=self._remaining_budget(),
                fallback_allowance_cny=(
                    self._structured_fallback_allowance()
                ),
            )
            return local_generated, local_unresolved
        if needs_structured_fallback:
            self._event(
                "structured_diagram_fallback_started",
                visual_plan_id=plan_id,
                fallback_allowance_cny=(
                    self._structured_fallback_allowance()
                ),
            )
            if not self._reserve_fallback_allowance():
                local_unresolved.append(
                    {
                        **dict(request),
                        "reason": (
                            "visual_budget_exhausted_before_"
                            "structured_fallback"
                        ),
                    }
                )
                self._event(
                    "structured_diagram_fallback_skipped_by_budget",
                    visual_plan_id=plan_id,
                    remaining_budget_cny=self._remaining_budget(),
                )
                return local_generated, local_unresolved
            try:
                fallback_result = (
                    self._render_structured_diagram_fallback(
                        plan=plan,
                        section=sections.get(
                            str(request.get("section_id") or ""),
                            {},
                        ),
                        figure_id=f"{plan_id}_structured",
                    )
                )
            finally:
                self._release_fallback_allowance()
            fallback_result["image_generation_attempts"] = [
                result
            ]
            result = fallback_result
            # Only the text-spec and vision-audit token calls are billed;
            # Graphviz rendering itself is local and free.
            fallback_audit_usage = dict(
                result.get("model_review_usage") or {}
            )
            if fallback_audit_usage:
                fallback_audit_cost = _usage_cost(
                    {"_llm_usage": fallback_audit_usage},
                    self.config.vision_model_tier,
                )
                self._bump_cost(
                    vision_calls=1,
                    vision_input_tokens=(
                        fallback_audit_cost["input_tokens"]
                    ),
                    vision_output_tokens=(
                        fallback_audit_cost["output_tokens"]
                    ),
                    estimated_cost_cny=(
                        fallback_audit_cost["estimated_cost_cny"]
                    ),
                )
        image_path = Path(
            str(result.get("local_image_path") or "")
        )
        status = str(result.get("generation_status") or "")
        technical_veto = (
            self._generated_visual_has_technical_failure(result)
        )
        integrity_flags = self._generated_visual_integrity_flags(result)
        illegible = self._generated_visual_is_illegible(result)
        submission_integrity_block = (
            str(self.config.execution_profile or "").strip().lower()
            == "submission"
            and bool(integrity_flags)
        )
        # Private-study relaxation: a rendered figure is no longer discarded
        # because the reviewer declined to approve it.  Only three conditions
        # still block placement -- there is no renderable file, generation
        # never materialized at all (transport/budget failure statuses), or
        # the figure is illegible.  A reviewer objection now travels into the
        # caption as an explicit warning instead of deleting the figure.
        # ``attempts_exhausted`` keeps its retry/fallback meaning upstream; it
        # simply no longer vetoes placement.
        #
        # Illegibility is the one objection the relaxation must not swallow.
        # The visA_stage2b run placed FIG-GEN-002 -- a qwen-image-2.0-pro
        # raster whose title rendered as "CRITAR DIEAL REORLIDOMCEB TLS
        # FRDMINE: DVON" and whose every axis label was garbled -- under a
        # caption promising "the dispersion of reported cooling powers across
        # test protocols".  The reviewer named the garbling on all three
        # attempts.  Nothing in the figure supported the caption, so the
        # relaxation had converted an honest failure into a false claim.
        salvaged = bool(
            (technical_veto or attempts_exhausted)
            and image_path.is_file()
            and status
            in {
                "model_approved_human_pending",
                "model_rejected_or_revision_required",
            }
        )
        # P1-3: when the structured fallback already retried with reviewer
        # feedback and still failed every attempt, say so explicitly instead
        # of leaving a bare exhausted flag.  Computed before the placement
        # gate so a salvaged figure carries the same provenance an unresolved
        # need would have carried -- the note is the only record of how many
        # feedback rounds the figure survived.
        structured_attempts = [
            row
            for row in (
                result.get("structured_attempts") or []
            )
            if isinstance(row, dict)
        ]
        raster_retry_history = [
            row
            for row in (
                result.get("retry_history") or []
            )
            if isinstance(row, dict)
        ]
        feedback_retry_count = len(structured_attempts) or (
            len(raster_retry_history) + 1
            if raster_retry_history
            else 0
        )
        degradation_note = ""
        if attempts_exhausted and feedback_retry_count:
            degradation_note = (
                "spec quality insufficient after "
                f"{feedback_retry_count} "
                "feedback-carrying retries; reviewer threshold "
                "not relaxed"
            )
        if (
            not image_path.is_file()
            or illegible
            or submission_integrity_block
            or status
            not in {
                "model_approved_human_pending",
                "model_rejected_or_revision_required",
            }
        ):
            reason = (
                # Most specific explanation wins: an illegible figure was
                # rendered and retried, so "attempts exhausted" would hide
                # why the output was unusable.
                "generated_visual_illegible"
                if illegible
                else "submission_integrity_blocked"
                if submission_integrity_block
                else "generation_attempts_exhausted"
                if attempts_exhausted
                else (
                    "generated_visual_failed_technical_veto"
                    if technical_veto
                    else status or "generation_not_materialized"
                )
            )
            unresolved_entry: Dict[str, Any] = {
                **dict(request),
                "reason": reason,
                "generation_total_attempts": result.get(
                    "generation_total_attempts",
                    1,
                ),
                "generation_attempts_exhausted": (
                    attempts_exhausted
                ),
                "generation_result": result,
            }
            event_details: Dict[str, Any] = {
                "visual_plan_id": plan_id,
                "reason": reason,
                "generation_total_attempts": result.get(
                    "generation_total_attempts",
                    1,
                ),
            }
            if degradation_note:
                unresolved_entry["generation_degradation_note"] = (
                    degradation_note
                )
                event_details["generation_degradation_note"] = (
                    degradation_note
                )
            local_unresolved.append(unresolved_entry)
            self._event("conceptual_visual_unresolved", **event_details)
            return local_generated, local_unresolved
        # Reader-explanation mode allows a technically valid generated
        # schematic into the draft after model warnings, while preserving
        # every warning for the final user review.
        structured_spec = dict(
            result.get("structured_spec")
            or (result.get("structured_fallback") or {}).get("spec")
            or {}
        )
        if structured_spec:
            actual_figure_type = (
                "structured_explanatory_diagram"
            )
            structured_title = str(
                structured_spec.get("title") or ""
            ).strip()
            structured_takeaway = str(
                structured_spec.get("takeaway") or ""
            ).strip()
            caption = ". ".join(
                part
                for part in (
                    structured_title,
                    structured_takeaway,
                )
                if part
            ).rstrip(".")
            caption = (
                caption
                + ". AI-assisted explanatory diagram; not empirical "
                "evidence."
            )
        else:
            actual_figure_type = str(
                request.get("figure_kind")
                or "conceptual_schematic"
            )
            caption = (
                str(request.get("argumentative_purpose") or "")
                + " AI-generated explanatory schematic; not empirical "
                "evidence."
            ).strip()
        if integrity_flags:
            # Explicit risk disclosure for a salvaged figure the reviewer
            # judged scientifically unsound.  The warning is part of the
            # caption itself, not a side-channel field, so it survives every
            # downstream renderer that only consumes ``caption_en``.
            caption = (
                caption
                + " WARNING: automated review flagged this figure ("
                + ", ".join(integrity_flags).replace("_", " ")
                + "); illustrative only -- do not cite or rely on any "
                "value shown."
            )
        if (
            str(result.get("generation_status") or "")
            == "model_approved_human_pending"
            or salvaged
        ):
            # P2-1 wiring 2 (single aggregation point): this branch is the
            # one place where an accepted figure is finalized into
            # local_generated, so every approved-pending figure funnels
            # through here regardless of route.  Registration failure is
            # logged, never allowed to kill an already-paid-for figure.
            # Salvaged figures register too: a figure placed over a reviewer
            # objection is exactly the case where the human gate matters, and
            # it is the only interactive chance to drop one.
            try:
                request_decision(
                    run_dir=self.config.output_dir,
                    kind="visual_review",
                    subject_id=plan_id,
                    context={
                        "section_id": str(
                            request.get("section_id") or ""
                        ),
                        "figure_kind": actual_figure_type,
                        "local_path": str(image_path.resolve()),
                        "caption_en": caption,
                        "salvaged_over_reviewer_objection": salvaged,
                        "integrity_flags": list(integrity_flags),
                    },
                    options=["accept", "reject"],
                    auto_accept_after_seconds=(
                        self.config.visual_review_auto_accept_seconds
                    ),
                    default_option="accept",
                )
            except Exception as exc:
                logger.warning(
                    "human_decision_gate registration failed for "
                    "visual_review %s: %s: %s",
                    plan_id,
                    type(exc).__name__,
                    exc,
                )
        local_generated.append(
            {
                "figure_id": plan_id,
                "section_id": str(
                    request.get("section_id") or ""
                ),
                "purpose": str(
                    request.get("argumentative_purpose") or ""
                ),
                "figure_type": actual_figure_type,
                "requested_figure_kind": str(
                    request.get("figure_kind") or ""
                ),
                "local_path": str(image_path.resolve()),
                "placement_anchor": str(
                    request.get("placement_guidance")
                    or request.get("section_id")
                    or ""
                ),
                "panel_manifest": [
                    {
                        "panel_id": "a",
                        "visual_chunk_id": "",
                        "paper_id": "",
                        "doi": "",
                        "source_local_path": "",
                        "generated_status": "ai_generated",
                        "generation_model": (
                            result.get("generation_model_used")
                            or self.config.image_model
                        ),
                        "provenance_path": str(
                            result.get("provenance_path") or ""
                        ),
                    }
                ],
                "caption_en": caption,
                "source_caption": "",
                "source_attribution": {
                    "paper_id": "generated:article_owned_visual",
                    "doi": "",
                },
                "source_map": {},
                "figure_contract": dict(
                    request.get("figure_contract") or {}
                ),
                "caption_zh_optional": "",
                "source_route": "conceptual_generated",
                "data_provenance_level": str(
                    request.get("data_provenance_level")
                    or "schematic"
                ),
                "generated_or_source": "generated",
                "review_decision": _review_decision(
                    test_mode=self.config.test_mode
                ),
                "review_flags": list(
                    result.get("model_review", {}).get(
                        "misleading_elements",
                        [],
                    )
                    or []
                ),
                "salvaged_over_reviewer_objection": salvaged,
                "integrity_flags": list(integrity_flags),
                "generation_degradation_note": degradation_note,
                "generation_result": result,
                "render_status": "ready",
            }
        )
        self._bump_cost(generated_figures=1)
        self._event(
            "conceptual_visual_ready",
            figure_id=plan_id,
            generation_model=result.get(
                "generation_model_used",
                self.config.image_model,
            ),
            salvaged_over_reviewer_objection=salvaged,
            integrity_flags=list(integrity_flags),
            generation_degradation_note=degradation_note,
            review_warning_count=len(
                result.get("model_review", {}).get(
                    "misleading_elements",
                    [],
                )
                or []
            ),
        )
        return local_generated, local_unresolved

    def _own_accepted_figures(
        self,
        figures: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Copy accepted figures into the current run's owned asset directory.

        The long-term cache and previous runs are acquisition sources only.
        Every accepted figure receives a stable copy under
        ``output_dir/final_assets``; ``local_path`` becomes that owned
        canonical path while ``original_source_path`` preserves the
        acquisition provenance.  Copy failures stay fail-open and are
        recorded, never a pipeline block.
        """

        asset_dir = self.output_dir / FINAL_ASSET_SUBDIR
        asset_dir.mkdir(parents=True, exist_ok=True)
        report: Dict[str, Any] = {
            "asset_dir": str(asset_dir.resolve()),
            "owned": 0,
            "failures": [],
        }
        owned_figures: List[Dict[str, Any]] = []
        for index, source_figure in enumerate(figures, 1):
            figure = dict(source_figure)
            source_path = Path(str(figure.get("local_path") or ""))
            original_source_path = str(source_path)
            figure.setdefault(
                "original_source_path",
                original_source_path,
            )
            suffix = source_path.suffix or ".png"
            stem = _safe_asset_stem(
                figure.get("figure_id") or "figure"
            )
            destination = asset_dir / f"{index:03d}_{stem}{suffix}"
            owned_source_path = str(destination.resolve())
            if not source_path.is_file():
                report["failures"].append(
                    {
                        "figure_id": str(figure.get("figure_id") or ""),
                        "section_id": str(figure.get("section_id") or ""),
                        "reason": "source_file_missing",
                        "original_source_path": original_source_path,
                    }
                )
                self._event(
                    "final_asset_ownership_skipped_missing",
                    figure_id=figure.get("figure_id", ""),
                    source_path=original_source_path,
                )
                continue
            if source_path.resolve() == destination.resolve():
                figure["local_path"] = owned_source_path
                figure["owned_asset_path"] = owned_source_path
                figure["assets_owned_by_run"] = True
                report["owned"] += 1
                owned_figures.append(figure)
                continue
            try:
                shutil.copyfile(source_path, destination)
            except Exception as exc:
                report["failures"].append(
                    {
                        "figure_id": str(figure.get("figure_id") or ""),
                        "section_id": str(figure.get("section_id") or ""),
                        "reason": "copy_failed",
                        "original_source_path": original_source_path,
                        "error": (
                            f"{type(exc).__name__}:{exc}"
                        ),
                    }
                )
                self._event(
                    "final_asset_copy_failed",
                    figure_id=figure.get("figure_id", ""),
                    source_path=original_source_path,
                    error=str(exc),
                )
                continue
            figure["local_path"] = owned_source_path
            figure["owned_asset_path"] = owned_source_path
            figure["assets_owned_by_run"] = True
            report["owned"] += 1
            self._event(
                "final_asset_owned",
                figure_id=figure.get("figure_id", ""),
                original_source_path=original_source_path,
                owned_source_path=owned_source_path,
            )
            owned_figures.append(figure)
        return owned_figures, report

    def run(
        self,
        *,
        visual_plan_path: Path,
        blueprint: Dict[str, Any],
        review_work_dir: Path,
    ) -> Dict[str, Any]:
        del review_work_dir  # Reserved for later paragraph-anchor refinement.
        visual_plan, plan_ingestion = normalize_visual_factory_plan(
            _read_json(Path(visual_plan_path))
        )
        visual_plan = apply_structural_visual_policy(
            visual_plan
        )
        plan_ingestion["retained_placement_count"] = len(
            visual_plan.get("placements") or []
        )
        plan_ingestion["retained_generation_request_count"] = len(
            visual_plan.get("conceptual_figure_requests") or []
        )
        self._blueprint_sections = {
            str(row.get("section_id") or ""): row
            for row in blueprint.get("sections", []) or []
            if isinstance(row, dict) and row.get("section_id")
        }
        input_fingerprint = build_visual_factory_input_fingerprint(
            visual_plan=visual_plan,
            blueprint=blueprint,
            real_visual_audit=self.config.real_visual_audit,
            real_image_generation=self.config.real_image_generation,
            test_mode=self.config.test_mode,
            vision_model_tier=self.config.vision_model_tier,
            image_model=self.config.image_model,
            max_generated_images=self.config.max_generated_images,
            structural_visual_policy_enabled=True,
            cache_namespace=self.cache_namespace,
        )
        contract = build_article_visual_contract(
            blueprint=blueprint,
            visual_plan=visual_plan,
        )
        atomic_write_json(
            self.output_dir / "ARTICLE_VISUAL_CONTRACT.json",
            contract,
        )
        self._event(
            "visual_contract_created",
            slot_count=contract["slot_count"],
        )

        source, rejected = self._source_figures(
            visual_plan.get("placements", []) or []
        )
        figures = self._compose_groups(source)
        generated, generation_unresolved = self._generated_figures(
            visual_plan.get("conceptual_figure_requests", []) or [],
            blueprint=blueprint,
        )
        figures.extend(generated)
        review_queue = build_visual_review_queue(
            figures,
            test_mode=self.config.test_mode,
            timeout_seconds=self.config.human_timeout_seconds,
        )
        figures, human_rejected = apply_visual_review_queue(
            figures,
            review_queue,
        )
        figures, ownership_report = self._own_accepted_figures(
            figures
        )
        atomic_write_json(
            self.output_dir / "VISUAL_REVIEW_QUEUE.json",
            review_queue,
        )
        self._event(
            "visual_human_review_policy_applied",
            test_mode=self.config.test_mode,
            accepted_count=len(figures),
            rejected_count=len(human_rejected),
        )

        unfilled = [
            dict(row)
            for row in visual_plan.get("unfilled_visual_needs", []) or []
            if isinstance(row, dict)
        ]
        unfilled.extend(generation_unresolved)
        unfilled.extend(rejected)
        unfilled.extend(
            {
                "figure_id": str(row.get("figure_id") or ""),
                "section_id": str(row.get("section_id") or ""),
                "reason": "final_asset_ownership_failed",
                "ownership_failure_reason": str(row.get("reason") or ""),
                "original_source_path": str(
                    row.get("original_source_path") or ""
                ),
            }
            for row in ownership_report.get("failures", []) or []
        )
        unfilled.extend(
            {
                "figure_id": row.get("figure_id", ""),
                "section_id": row.get("section_id", ""),
                "reason": "human_rejected",
            }
            for row in human_rejected
        )
        self.cost["estimated_cost_cny"] = round(
            float(self.cost["estimated_cost_cny"]),
            6,
        )
        figure_count = len(figures)
        self.cost["cost_per_final_figure_cny"] = round(
            float(self.cost["estimated_cost_cny"])
            / max(1, figure_count),
            6,
        )
        self.cost["vlm_images_reviewed"] = int(
            self.cost["vision_calls"]
        )
        self.cost["cache_hit_rate"] = round(
            float(self.cost["cache_hits"])
            / max(
                1,
                int(self.cost["vision_calls"])
                + int(self.cost["cache_hits"]),
            ),
            4,
        )
        self.cost["approximate_data_figures"] = sum(
            1
            for row in figures
            if str(row.get("data_provenance_level") or "")
            == "approximate"
        )
        self.cost["unfilled_visual_opportunities"] = len(unfilled)
        final_package = {
            "schema_version": "research_harness.final_visual_package.v1",
            "run_id": self.config.run_id,
            "created_at": _now(),
            "input_fingerprint": input_fingerprint,
            "cache_namespace": self.cache_namespace,
            "mode": "reader_explanation",
            "visual_plan_ingestion": plan_ingestion,
            "article_visual_contract": contract,
            "figures": figures,
            "unfilled_visual_opportunities": unfilled,
            "visual_cost_report": self.cost,
            "human_review": {
                "test_mode": self.config.test_mode,
                "timeout_seconds": self.config.human_timeout_seconds,
                "timeout_decision": "timeout_accepted_for_draft",
                "queue_path": str(
                    self.output_dir / "VISUAL_REVIEW_QUEUE.json"
                ),
            },
            "validation": {},
        }
        validation = validate_final_visual_package_value(final_package)
        final_package["validation"] = validation
        final_audit = {
            "schema_version": (
                "research_harness.final_visual_audit.v1"
            ),
            "run_id": self.config.run_id,
            "created_at": _now(),
            "status": (
                "passed"
                if validation.get("status") == "passed"
                else "degraded_or_failed"
            ),
            "validation": validation,
            "renderable_figure_count": len(figures),
            "source_derived_count": sum(
                1
                for row in figures
                if row.get("source_route") == "source_derived"
            ),
            "generated_or_data_count": sum(
                1
                for row in figures
                if row.get("source_route")
                in {
                    "conceptual_generated",
                    "explanatory_data_visual",
                }
            ),
            "figures_with_review_warnings": [
                str(row.get("figure_id") or "")
                for row in figures
                if list(row.get("review_flags") or [])
            ],
            "accepted_review_state_distribution": {
                state: sum(
                    1
                    for row in figures
                    if str(row.get("review_decision") or "")
                    == state
                )
                for state in sorted(
                    {
                        str(row.get("review_decision") or "")
                        for row in figures
                    }
                )
                if state
            },
            "unfilled_visual_opportunity_count": len(unfilled),
            "text_preservation_policy": (
                "Unfilled visual opportunities never remove or downgrade "
                "validated review text."
            ),
            "provenance_routes": sorted(
                {
                    str(row.get("source_route") or "")
                    for row in figures
                    if row.get("source_route")
                }
            ),
            "visual_cost_report_path": str(
                self.output_dir / "VISUAL_COST_REPORT.json"
            ),
            "owned_asset_directory": str(
                self.output_dir / FINAL_ASSET_SUBDIR
            ),
            "owned_asset_count": int(
                ownership_report.get("owned") or 0
            ),
            "owned_asset_failure_count": len(
                ownership_report.get("failures") or []
            ),
            "owned_asset_failures": list(
                ownership_report.get("failures") or []
            ),
        }
        final_package["final_visual_audit_path"] = str(
            self.output_dir / "FINAL_VISUAL_AUDIT_REPORT.json"
        )
        self._event(
            "final_visual_package_validated",
            status=validation.get("status", ""),
            figure_count=validation.get("figure_count", 0),
            error_count=len(validation.get("errors", []) or []),
        )
        atomic_write_json(
            self.output_dir / "FINAL_VISUAL_PACKAGE.json",
            final_package,
        )
        atomic_write_json(
            self.output_dir / "FINAL_VISUAL_AUDIT_REPORT.json",
            final_audit,
        )
        atomic_write_json(
            self.output_dir / "VISUAL_COST_REPORT.json",
            self.cost,
        )
        self._persist_audit_cache()
        events_path = self.output_dir / "VISUAL_EVENTS.jsonl"
        events_path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for row in self.events
            ),
            encoding="utf-8",
        )
        return final_package


def status_is_verified(item: Dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "verified_existing"


def validate_final_visual_package_value(
    value: Dict[str, Any],
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    figures = value.get("figures", [])
    if not isinstance(figures, list):
        return {
            "status": "failed",
            "errors": ["figures_not_a_list"],
            "warnings": [],
        }
    for index, figure in enumerate(figures):
        if not isinstance(figure, dict):
            errors.append(f"figure[{index}]_not_object")
            continue
        path = Path(str(figure.get("local_path") or ""))
        if not path.is_file():
            errors.append(f"figure[{index}]_file_missing")
        if not str(figure.get("caption_en") or "").strip():
            errors.append(f"figure[{index}]_caption_missing")
        if not str(figure.get("placement_anchor") or "").strip():
            errors.append(f"figure[{index}]_placement_anchor_missing")
        if str(figure.get("review_decision") or "") not in (
            ACCEPTED_REVIEW_STATES
        ):
            errors.append(f"figure[{index}]_review_state_not_accepted")
        if str(figure.get("asset_copy_error") or ""):
            warnings.append(
                f"figure[{index}]_owned_asset_copy_failed"
            )
        route = str(figure.get("source_route") or "")
        if route == "source_derived":
            panels = figure.get("panel_manifest", []) or []
            if not panels or any(
                not str(panel.get("paper_id") or "")
                for panel in panels
                if isinstance(panel, dict)
            ):
                errors.append(
                    f"figure[{index}]_source_provenance_missing"
                )
        if route == "conceptual_generated":
            caption_lower = str(
                figure.get("caption_en") or ""
            ).lower()
            if not any(
                marker in caption_lower
                for marker in ("ai-generated", "ai-assisted")
            ):
                warnings.append(
                    f"figure[{index}]_generation_disclosure_unclear"
                )
    status = "passed" if not errors else "failed"
    if not figures:
        warnings.append("no_renderable_figures")
        status = "completed_without_figures" if not errors else status
    return {
        "status": status,
        "figure_count": len(figures),
        "errors": errors,
        "warnings": warnings,
    }


def validate_final_visual_package_file(path: Path) -> str:
    value = _read_json(Path(path))
    if not value:
        return "VALIDATION_FAILED: FINAL_VISUAL_PACKAGE.json is missing or invalid."
    report = validate_final_visual_package_value(value)
    if report["errors"]:
        return "VALIDATION_FAILED: " + "; ".join(report["errors"])
    # P1-3 (round 3): a package whose source plan still carries unfilled
    # visual needs is NOT fully passed.  Reporting VALIDATION_PASSED here let
    # runs claim completed materialization while needs went unanswered.
    # ``unfilled_visual_opportunities`` is the final-package contract.  A
    # missing field is unknown, not zero: treating it as zero once made an
    # incomplete historical package report VALIDATION_PASSED.
    if "unfilled_visual_opportunities" not in value:
        return (
            "VALIDATION_FAILED: validation_contract_missing: "
            "unfilled_visual_opportunities"
        )
    raw_unfilled = value.get("unfilled_visual_opportunities")
    if not isinstance(raw_unfilled, list):
        return (
            "VALIDATION_FAILED: validation_contract_missing: "
            "unfilled_visual_opportunities must be a list"
        )
    unfilled = len(raw_unfilled)
    if report["status"] == "completed_without_figures":
        suffix = (
            f"; {unfilled} unfilled visual need(s) remain"
            if unfilled > 0
            else ""
        )
        return (
            "VALIDATION_DEGRADED: final visual package is structurally valid "
            "but contains no renderable figures" + suffix + "."
        )
    if unfilled > 0:
        return (
            "VALIDATION_DEGRADED: final visual package contains "
            f"{report['figure_count']} renderable figure(s) but "
            f"{unfilled} unfilled visual need(s) remain unmet."
        )
    return (
        "VALIDATION_PASSED: final visual package contains "
        f"{report['figure_count']} renderable figure(s)."
    )


def run_visual_evidence_factory(
    *,
    visual_plan_path: Path,
    blueprint: Dict[str, Any],
    review_work_dir: Path,
    output_dir: Path,
    cost_budget_cny: float = 5.0,
    global_budget_remaining_cny: Optional[float] = None,
    real_visual_audit: bool = False,
    real_image_generation: bool = False,
    test_mode: bool = True,
    vision_model_tier: str = "vision_plus_model",
    image_model: str = "qwen-image-2.0-pro",
    max_generated_images: int = 4,
    workers: int = 1,
    run_id: str = "",
    shared_cache_dir: Optional[Path] = None,
    cache_namespace: str = "",
    visual_review_auto_accept_seconds: Optional[float] = None,
    execution_profile: str = "library_offline",
) -> Dict[str, Any]:
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=output_dir,
            run_id=run_id,
            cost_budget_cny=cost_budget_cny,
            global_budget_remaining_cny=global_budget_remaining_cny,
            real_visual_audit=real_visual_audit,
            real_image_generation=real_image_generation,
            test_mode=test_mode,
            vision_model_tier=vision_model_tier,
            image_model=image_model,
            max_generated_images=max_generated_images,
            workers=workers,
            shared_cache_dir=shared_cache_dir,
            cache_namespace=cache_namespace,
            visual_review_auto_accept_seconds=(
                visual_review_auto_accept_seconds
            ),
            execution_profile=str(execution_profile or "library_offline"),
        )
    )
    return factory.run(
        visual_plan_path=visual_plan_path,
        blueprint=blueprint,
        review_work_dir=review_work_dir,
    )
