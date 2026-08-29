"""Article-level visual editor tools for existing and requested figures."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.tool import FunctionTool

logger = logging.getLogger(__name__)
# Emitted once per structurally-broken visual library path so that a
# "zero visuals" outcome is never silent again (backend-fix 2.2).
_REPORTED_BAD_KB_PATHS: set[str] = set()
# Backend-fix ticket 2.4: near-total dangling local_image_path ratios
# are a pipeline defect, not bad luck; scream once per load cycle.
_VISUAL_MISSING_RATIO_ALARM = 0.9
_VISUAL_MISSING_MIN_ROWS = 10

from optomind_research.visual_argument_alignment import (
    VALID_VISUAL_ARGUMENT_TYPES,
    VisualArgumentAligner,
)
from optomind_research.visual_argument_classifier import (
    VisualArgumentClassifier,
)

from .artifact_store import atomic_write_json
from .cost_ledger import estimate_call_cost_cny
from .tool_provider import ToolProvider
from .visual_asset_planner_adapter import load_visual_cache_records

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

_HYGIENE_ELIGIBLE_STATUSES = frozenset({"clean", "derived_clean"})

MAX_CONCEPTUAL_FIGURE_REQUESTS = 4
VISUAL_EDITOR_PAYLOAD_CHAR_LIMIT = 23_500
GENERATION_PORTFOLIO_BUDGET_REASON = (
    "generation_portfolio_budget_or_low_incremental_value"
)
_GENERATION_OVERVIEW_KINDS = frozenset(
    {
        "mechanism_schematic",
        "concept_map",
        "taxonomy_diagram",
        "comparison_diagram",
    }
)
_GENERATION_WORKFLOW_KINDS = frozenset({"workflow_schematic"})
_REQUEST_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Words that describe almost any machine-learning/optics paper are useful for
# section ranking but are too weak to establish topic identity.  The provider
# combines this list with the topic identity contract so a generic candidate
# such as a neural-rendering paper cannot enter an optical diffractive-network
# shortlist merely because it shares "neural" or "network".
_TOPIC_GENERIC_ANCHORS = frozenset(
    {
        "network",
        "networks",
        "neural",
        "imaging",
        "image",
        "images",
        "optical",
        "system",
        "systems",
        "method",
        "methods",
        "model",
        "models",
        "application",
        "applications",
        "learning",
        "architecture",
        "architectures",
        "training",
        "analysis",
        "review",
        "research",
        "performance",
        "design",
        "data",
        "processing",
        "deep",
        "based",
        "using",
        "layer",
        "layers",
        "study",
        "studies",
        "approach",
        "device",
        "devices",
        "technology",
        "framework",
        "structure",
        "structures",
        "comparison",
        "comparisons",
        "future",
        "direction",
        "directions",
        "challenge",
        "challenges",
        "problem",
        "problems",
        "paper",
        "papers",
        "work",
        "works",
        "result",
        "results",
    }
)


def _apply_generation_portfolio_budget(
    requests: List[Dict[str, Any]],
    *,
    max_requests: int = MAX_CONCEPTUAL_FIGURE_REQUESTS,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministically trim a conceptual generation portfolio.

    The cap is a safety upper bound, not a target.  Source placements never
    count against it.  At most one generated request per section is retained.
    Priority is preserved first; among equal priority the portfolio prefers
    one mechanism/comparison overview and one workflow/decision figure, then
    fills with one each of distinct figure_kind/section before repeating.
    Dropped requests become explicit unfilled needs.
    """

    if not requests:
        return [], []

    def sort_key(
        item: tuple[int, Dict[str, Any]],
    ) -> tuple[int, int, int]:
        index, request = item
        priority = str(request.get("priority") or "medium").strip().lower()
        kind = str(request.get("figure_kind") or "")
        if kind in _GENERATION_OVERVIEW_KINDS:
            portfolio = 0
        elif kind in _GENERATION_WORKFLOW_KINDS:
            portfolio = 1
        else:
            portfolio = 2
        return (
            _REQUEST_PRIORITY_RANK.get(priority, 1),
            portfolio,
            index,
        )

    indexed = [(index, dict(request)) for index, request in enumerate(requests)]
    # At most one generated request per section.
    by_section: Dict[str, tuple[int, Dict[str, Any]]] = {}
    for item in indexed:
        section_id = str(item[1].get("section_id") or "")
        if not section_id:
            continue
        current = by_section.get(section_id)
        if current is None or sort_key(item) < sort_key(current):
            by_section[section_id] = item
    duplicate_dropped = sorted(
        (
            item
            for item in indexed
            if item[0]
            not in {kept[0] for kept in by_section.values()}
        ),
        key=lambda item: item[0],
    )
    unique = sorted(by_section.values(), key=sort_key)
    if len(unique) <= max_requests:
        retained = [
            request for _, request in sorted(unique, key=lambda item: item[0])
        ]
        dropped = duplicate_dropped
        return retained, [dict(item) for _, item in dropped]

    selected: List[tuple[int, Dict[str, Any]]] = []
    selected_indexes: set[int] = set()

    def first_with_kind(
        kinds: frozenset[str],
    ) -> Optional[tuple[int, Dict[str, Any]]]:
        for item in unique:
            if item[0] in selected_indexes:
                continue
            if item[1].get("figure_kind") in kinds:
                return item
        return None

    overview = first_with_kind(_GENERATION_OVERVIEW_KINDS)
    if overview is not None:
        selected.append(overview)
        selected_indexes.add(overview[0])
    workflow = first_with_kind(_GENERATION_WORKFLOW_KINDS)
    if workflow is not None:
        selected.append(workflow)
        selected_indexes.add(workflow[0])

    remaining = [
        item for item in unique if item[0] not in selected_indexes
    ]
    used_kinds = {item[1]["figure_kind"] for item in selected}
    used_sections = {item[1]["section_id"] for item in selected}
    while len(selected) < max_requests and remaining:
        pick: Optional[tuple[int, Dict[str, Any]]] = None
        for item in sorted(remaining, key=sort_key):
            if (
                item[1]["figure_kind"] not in used_kinds
                or item[1]["section_id"] not in used_sections
            ):
                pick = item
                break
        if pick is None:
            pick = min(remaining, key=sort_key)
        selected.append(pick)
        selected_indexes.add(pick[0])
        used_kinds.add(pick[1]["figure_kind"])
        used_sections.add(pick[1]["section_id"])
        remaining = [
            item for item in remaining if item[0] not in selected_indexes
        ]

    retained = [
        request for _, request in sorted(selected, key=lambda item: item[0])
    ]
    dropped = sorted(
        [*remaining, *duplicate_dropped],
        key=lambda item: item[0],
    )
    return retained, [dict(item) for _, item in dropped]


@dataclass
class VisualEditorContext:
    blueprint: Dict[str, Any]
    review_work_dir: Path
    work_dir: Path
    kb_sqlite_paths: List[Path] = field(default_factory=list)
    classifier_model_tier: str = "vision_plus_model"
    max_pending_classifications: int = 4
    classifier_cost_budget_cny: float = 0.8
    compact_article_mode: bool = True
    input_fingerprint: str = ""


def validate_visual_editorial_plan_file(
    plan_path: Path,
    expected_input_fingerprint: str = "",
    expected_visual_section_ids: Optional[set[str]] = None,
) -> str:
    """Validate a saved visual plan without spending another model call."""
    path = Path(plan_path)
    if not path.is_file():
        return "VALIDATION_FAILED: VISUAL_EDITORIAL_PLAN.json is missing."
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"VALIDATION_FAILED: invalid visual plan JSON: {exc}"
    if not isinstance(value, dict):
        return "VALIDATION_FAILED: visual plan root is not an object."
    errors: List[str] = []
    if (
        expected_input_fingerprint
        and str(value.get("input_fingerprint") or "")
        != expected_input_fingerprint
    ):
        errors.append("visual plan input fingerprint is stale")
    placements = value.get("placements", [])
    requests = value.get("conceptual_figure_requests", [])
    unfilled = value.get("unfilled_visual_needs", [])
    if not all(
        isinstance(items, list)
        for items in (placements, requests, unfilled)
    ):
        return "VALIDATION_FAILED: visual plan lists are malformed."
    for index, placement in enumerate(placements):
        if not isinstance(placement, dict):
            errors.append(f"placement[{index}] is not an object")
            continue
        image_path = Path(str(placement.get("local_image_path") or ""))
        if (
            str(placement.get("status") or "")
            not in {
                "verified_existing",
                "traceable_source_pending_review",
            }
            or not image_path.is_file()
            or not placement.get("visual_chunk_id")
            or not placement.get("paper_id")
        ):
            errors.append(f"placement[{index}] is not traceable")
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            errors.append(f"request[{index}] is not an object")
            continue
        if (
            request.get("status") != "pending_generation_and_review"
            or request.get("required_disclosure")
            not in {
                "AI-generated conceptual illustration",
                "AI-generated explanatory visual",
            }
        ):
            errors.append(f"request[{index}] lacks disclosure/review state")
    accounted_sections = {
        str(item.get("section_id") or "")
        for items in (placements, requests, unfilled)
        for item in items
        if isinstance(item, dict) and item.get("section_id")
    }
    missing_sections = sorted(
        set(expected_visual_section_ids or set()) - accounted_sections
    )
    if missing_sections:
        errors.append(
            "visual needs were silently omitted for sections: "
            + ", ".join(missing_sections)
        )
    if errors:
        return "VALIDATION_FAILED: " + "; ".join(errors)
    return (
        "VALIDATION_PASSED: visual editorial plan contains "
        f"{len(placements)} traceable existing assets and "
        f"{len(requests)} disclosed conceptual requests."
    )


class VisualEditorToolProvider(ToolProvider):
    TOOL_NAMES = [
        "load_visual_editor_context",
        "inspect_article_visual_candidates",
        "inspect_section_visual_candidates",
        "classify_pending_visual_candidates",
        "read_section_text_for_visuals",
        "submit_visual_editorial_plan",
        "validate_visual_editorial_plan",
    ]

    def __init__(self, ctx: VisualEditorContext) -> None:
        self.ctx = ctx
        self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path = self.ctx.work_dir / "VISUAL_EDITORIAL_PLAN.json"
        self.rejected_plan_path = (
            self.ctx.work_dir / "VISUAL_EDITORIAL_PLAN_REJECTED.json"
        )
        self.recovery_path = (
            self.ctx.work_dir / "VISUAL_EDITOR_RECOVERY.json"
        )
        self._aligner = VisualArgumentAligner()
        self._visuals = self._load_visuals()
        # Round-2 defect-A follow-up: the initial load must alarm too --
        # previously only _reload_visuals did, so a first-load dangling
        # library (the be780761 shape) passed completely silently.
        self._alarm_on_dangling_paths(self._visuals)
        self._index = self._aligner.build_visual_retrieval_index(
            self._visuals,
            allowed_statuses=("ok", "pending_multimodal_review"),
        )
        self._sections = {
            str(section.get("section_id")): section
            for section in self.ctx.blueprint.get("sections", [])
            if isinstance(section, dict) and section.get("section_id")
        }
        (
            self._topic_anchor_tokens,
            self._topic_anchor_phrases,
        ) = self._build_topic_relevance_profile()
        self._topic_relevance_rejected_ids: set[str] = set()
        self._inspected: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._pending_classifications_used = 0
        self._classifier_cost_cny = 0.0
        self._classifier_input_tokens = 0
        self._classifier_output_tokens = 0
        self._classifier_calls = 0
        self._classifier_cost_path = (
            self.ctx.work_dir / "NESTED_VISION_COST.json"
        )

    def finalize_safe_partial_plan(self) -> Dict[str, Any]:
        """Recover a valid conservative plan after a model budget stop.

        The model may submit several valid, traceable placements plus one bad
        ID.  Discarding the entire article plan wastes both good work and API
        budget.  This recovery never guesses a replacement: it keeps only the
        items that already passed deterministic provenance checks and records
        every dropped item for human review.
        """

        if self.plan_path.is_file():
            validation = validate_visual_editorial_plan_file(
                self.plan_path,
                self.ctx.input_fingerprint,
                self._expected_visual_section_ids(),
            )
            if validation.startswith("VALIDATION_PASSED"):
                return {
                    "recovered": True,
                    "validation": validation,
                    "reused_existing": True,
                }
        if not self.rejected_plan_path.is_file():
            return {"recovered": False, "reason": "no_rejected_plan"}
        try:
            rejected = json.loads(
                self.rejected_plan_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            return {
                "recovered": False,
                "reason": f"rejected_plan_unreadable: {exc}",
            }
        partial = rejected.get("valid_partial_plan", {})
        if not isinstance(partial, dict):
            return {"recovered": False, "reason": "partial_plan_missing"}
        if not any(
            partial.get(key)
            for key in (
                "placements",
                "conceptual_figure_requests",
                "unfilled_visual_needs",
            )
        ):
            return {"recovered": False, "reason": "partial_plan_empty"}
        partial["recovery"] = {
            "mode": "deterministic_drop_invalid_items",
            "dropped_items": rejected.get("errors", []),
            "human_review_recommended": bool(rejected.get("errors")),
        }
        atomic_write_json(self.plan_path, partial)
        validation = validate_visual_editorial_plan_file(
            self.plan_path,
            self.ctx.input_fingerprint,
            self._expected_visual_section_ids(),
        )
        recovered = validation.startswith("VALIDATION_PASSED")
        report = {
            "schema_version": "research_harness.visual_recovery.v1",
            "recovered": recovered,
            "validation": validation,
            "dropped_items": rejected.get("errors", []),
            "plan_path": str(self.plan_path),
        }
        atomic_write_json(self.recovery_path, report)
        if not recovered:
            self.plan_path.unlink(missing_ok=True)
        return report

    def _expected_visual_section_ids(self) -> set[str]:
        """Return sections whose blueprint declares an argumentative visual need."""

        return {
            section_id
            for section_id, section in self._sections.items()
            if (
                section.get("visual_argument_slots")
                or section.get("expected_visual_arguments")
            )
        }

    @staticmethod
    def _crop_hygiene_eligible(visual: Dict[str, Any]) -> bool:
        """Only clean/derived_clean snapshot images enter the editor pool.

        Legacy records without a ``crop_hygiene`` payload remain eligible so
        historical ``visual_chunks`` caches keep working unchanged.  A
        snapshot-format record without hygiene metadata fails closed: only
        ``clean``/``derived_clean`` is ever shortlisted.  A ``derived_clean``
        candidate must point at its derivative image; the rendered
        caption-region original is never exposed.
        """

        hygiene = visual.get("crop_hygiene")
        if str(visual.get("cache_format") or "") == "snapshot":
            if not isinstance(hygiene, dict) or not hygiene:
                return False
        if not isinstance(hygiene, dict) or not hygiene:
            return True
        status = str(hygiene.get("status") or "").lower()
        if status not in _HYGIENE_ELIGIBLE_STATUSES:
            return False
        if status == "derived_clean":
            derivative = hygiene.get("derivative")
            if not isinstance(derivative, dict) or not str(
                derivative.get("relpath") or ""
            ):
                return False
            local_path = Path(str(visual.get("local_image_path") or ""))
            if not local_path.is_file():
                return False
            original_path = Path(str(visual.get("original_image_path") or ""))
            if (
                original_path.is_file()
                and local_path.resolve() == original_path.resolve()
            ):
                return False
        return True

    def _load_visuals(self) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for path in self.ctx.kb_sqlite_paths:
            if not path or not Path(path).exists():
                continue
            try:
                records = load_visual_cache_records(Path(path))
            except FileNotFoundError as exc:
                # Missing index file: legitimately empty for this run.
                logger.warning("visual cache index missing: %s (%s)", path, exc)
                continue
            except ValueError as exc:
                # Ticket 2.2: a sqlite file without units/visual_chunks is a
                # structural problem, not empty data.  It used to be swallowed
                # here, which is exactly why the wrong-KB wiring in ticket 2.1
                # stayed invisible.  Log loudly and keep going; the editor
                # itself must not crash on bad history data.
                key = str(path)
                if key not in _REPORTED_BAD_KB_PATHS:
                    _REPORTED_BAD_KB_PATHS.add(key)
                    logger.error(
                        "visual cache path is not a visual library "
                        "(no units/visual_chunks table): %s (%s)",
                        path,
                        exc,
                    )
                continue
            except Exception as exc:
                key = str(path)
                if key not in _REPORTED_BAD_KB_PATHS:
                    _REPORTED_BAD_KB_PATHS.add(key)
                    logger.error(
                        "failed to load visual cache %s: %s: %s",
                        path,
                        type(exc).__name__,
                        exc,
                    )
                continue
            for visual in records:
                visual = dict(visual)
                visual["_source_kb_sqlite"] = str(Path(path))
                if not self._crop_hygiene_eligible(visual):
                    continue
                visual_id = str(
                    visual.get("chunk_id")
                    or visual.get("visual_chunk_id")
                    or ""
                )
                if visual_id:
                    merged[visual_id] = visual
        return list(merged.values())

    def _reload_visuals(self) -> None:
        self._visuals = self._load_visuals()
        self._alarm_on_dangling_paths(self._visuals)
        self._index = self._aligner.build_visual_retrieval_index(
            self._visuals,
            allowed_statuses=("ok", "pending_multimodal_review"),
        )

    @staticmethod
    def _alarm_on_dangling_paths(
        visuals: List[Dict[str, Any]],
    ) -> None:
        """Log an error when most loaded visuals point at missing files.

        Ticket 2.4: the be780761 run fed the editor 260 candidates whose
        local_image_path values had all been orphaned by the parallel
        commit bug.  That must never pass silently again -- the editor
        keeps running (an empty plate is still actionable telemetry), but
        the condition is logged at ERROR with counts and a sample id.
        """

        referenced = [
            str(visual.get("local_image_path") or "")
            for visual in visuals
        ]
        referenced = [path for path in referenced if path.strip()]
        if len(referenced) < _VISUAL_MISSING_MIN_ROWS:
            return
        missing = sum(
            1 for path in referenced if not Path(path).is_file()
        )
        ratio = missing / len(referenced)
        if ratio >= _VISUAL_MISSING_RATIO_ALARM:
            sample_id = str(
                visuals[0].get("chunk_id")
                or visuals[0].get("visual_chunk_id")
                or ""
            )
            logger.error(
                "visual library integrity failure: %d/%d visual rows "
                "reference missing image files (ratio %.2f, sample chunk "
                "%s); the editor will see no usable imagery",
                missing,
                len(referenced),
                ratio,
                sample_id,
            )

    @staticmethod
    def _compact_visual_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Return only the fields an editor needs, keeping ReAct context small."""
        caption = str(
            candidate.get("caption_preview")
            or candidate.get("caption")
            or candidate.get("search_text")
            or ""
        ).strip()
        return {
            "chunk_id": str(candidate.get("chunk_id") or ""),
            "paper_id": str(candidate.get("paper_id") or ""),
            "doi": str(candidate.get("doi") or ""),
            "title": str(candidate.get("title") or "")[:240],
            "chunk_kind": str(candidate.get("chunk_kind") or ""),
            "local_image_path": str(candidate.get("local_image_path") or ""),
            "caption_preview": caption[:700],
            "visual_argument_type": str(
                candidate.get("visual_argument_type") or ""
            ),
            "visual_argument_status": str(
                candidate.get("visual_argument_status") or ""
            ),
            "visual_argument_needs_human_review": bool(
                candidate.get("visual_argument_needs_human_review", False)
            ),
            "visual_argument_claim": str(
                candidate.get("visual_argument_claim") or ""
            )[:500],
            "score": candidate.get("score", 0.0),
            "reason": str(candidate.get("reason") or "")[:300],
            "topic_relevance": str(
                candidate.get("topic_relevance") or "matched"
            ),
            "topic_anchor_hits": list(
                candidate.get("topic_anchor_hits") or []
            )[:8],
            "retrieval_pool_size": int(
                candidate.get("retrieval_pool_size") or 4
            ),
            "path_verified": True,
        }

    def _build_topic_relevance_profile(self) -> tuple[set[str], set[str]]:
        """Build distinctive topic anchors from the identity contract.

        The core/supporting anchors are preferred because they are produced
        upstream from the user's query.  A small blueprint-text fallback keeps
        focused/offline callers that do not provide a topic contract safe.
        Generic academic words are intentionally removed from the gate, while
        exact anchor phrases remain an additional strong match.
        """

        identity = self.ctx.blueprint.get("topic_identity") or {}
        if not isinstance(identity, dict):
            identity = {}

        def as_tokens(value: Any) -> set[str]:
            if isinstance(value, (list, tuple, set)):
                raw = " ".join(str(item or "") for item in value)
            else:
                raw = str(value or "")
            return self._aligner._tokenize_for_retrieval(raw)

        core = as_tokens(identity.get("core_anchor_tokens"))
        supporting = as_tokens(identity.get("supporting_anchor_tokens"))
        phrases = {
            re.sub(r"\s+", " ", str(item or "")).strip().casefold()
            for item in (identity.get("anchor_phrases") or [])
            if str(item or "").strip()
        }
        anchors = {
            token
            for token in (core | supporting)
            if token not in _TOPIC_GENERIC_ANCHORS
        }
        if not anchors and not phrases:
            fallback_parts = [
                self.ctx.blueprint.get("review_thesis", ""),
                self.ctx.blueprint.get("full_review_argument", ""),
            ]
            fallback_parts.extend(
                str(section.get(key) or "")
                for section in self._sections.values()
                for key in ("title", "argument_role", "chapter_argument")
            )
            fallback = self._aligner._tokenize_for_retrieval(
                " ".join(fallback_parts)
            )
            anchors = {
                token
                for token in fallback
                if token not in _TOPIC_GENERIC_ANCHORS
            }
        # An identity contract with only generic core anchors should still be
        # safer than the old open lexical gate; use the core as a last resort.
        if not anchors and core:
            anchors = set(core)
        return anchors, phrases

    def _topic_relevance(
        self,
        candidate: Dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Return whether a candidate is anchored to this article's topic."""

        candidate_tokens = {
            str(token).casefold()
            for token in (candidate.get("tokens") or [])
            if str(token).strip()
        }
        if not candidate_tokens:
            candidate_text = " ".join(
                str(candidate.get(key) or "")
                for key in (
                    "title",
                    "caption",
                    "caption_preview",
                    "search_text",
                    "parent_label",
                    "visual_argument_claim",
                )
            )
            candidate_tokens = self._aligner._tokenize_for_retrieval(
                candidate_text
            )
        token_hits = sorted(self._topic_anchor_tokens & candidate_tokens)
        candidate_text = " ".join(
            str(candidate.get(key) or "")
            for key in (
                "title",
                "caption",
                "caption_preview",
                "search_text",
                "parent_label",
            )
        ).casefold()
        phrase_hits = sorted(
            phrase
            for phrase in self._topic_anchor_phrases
            if phrase and phrase in candidate_text
        )
        hits = list(dict.fromkeys([*token_hits, *phrase_hits]))
        if not self._topic_anchor_tokens and not self._topic_anchor_phrases:
            return True, []
        return bool(hits), hits

    def _record_topic_relevance(
        self,
        candidate: Dict[str, Any],
    ) -> bool:
        relevant, hits = self._topic_relevance(candidate)
        candidate["topic_anchor_hits"] = hits[:8]
        candidate["topic_relevance"] = "matched" if relevant else "off_topic"
        if relevant:
            return True
        identifier = str(
            candidate.get("chunk_id")
            or candidate.get("visual_chunk_id")
            or candidate.get("local_image_path")
            or ""
        )
        if identifier:
            self._topic_relevance_rejected_ids.add(identifier)
        return False

    def _verified_candidates_for_section(
        self,
        section_id: str,
        *,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Retrieve traceable source assets and remember canonical rows.

        Pending current-topic figures are deliberately visible here.  They are
        cheap to rank from captions and nearby text, while the downstream
        visual evidence factory audits only the assets that the editor selects.
        """
        section = self._sections.get(str(section_id))
        if section is None:
            return []
        requested = min(max(1, int(top_k)), 12)
        # Caption/text retrieval is effectively free. Expand only when the
        # first four records are sparse or dominated by one paper, then expose
        # a small diverse shortlist to the LLM. This implements 4→8→12
        # progressive recall without paying to show every image to a VLM.
        candidates: List[Dict[str, Any]] = []
        expansion_used = 4
        for pool_size in (4, 8, 12):
            expansion_used = pool_size
            candidates = self._aligner.recommend_visuals_for_section(
                section,
                self._index,
                top_k=max(requested, pool_size),
            )
            paper_ids = {
                str(row.get("paper_id") or "")
                for row in candidates
                if row.get("paper_id")
            }
            best_score = max(
                (
                    float(row.get("score") or 0.0)
                    for row in candidates
                ),
                default=0.0,
            )
            if (
                len(candidates) >= requested
                and (
                    len(paper_ids) >= min(2, requested)
                    or requested == 1
                )
                and best_score >= 0.03
            ):
                break
        accepted_full: List[Dict[str, Any]] = []
        seen_paths: set[str] = set()
        for raw in candidates:
            candidate = dict(raw)
            path = Path(str(candidate.get("local_image_path") or ""))
            kind = str(candidate.get("chunk_kind") or "").lower()
            if not candidate.get("local_image_path") or not path.is_file():
                continue
            # Composite parents are containers, not review-ready visual chunks.
            # Historical IDs may end in "-parent" even when the canonical kind
            # is a genuine single figure, so the semantic kind is authoritative.
            if "composite" in kind and "subfigure" not in kind:
                continue
            if not self._record_topic_relevance(candidate):
                continue
            normalized_path = str(path.resolve()).lower()
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            candidate["path_verified"] = True
            candidate["retrieval_pool_size"] = expansion_used
            accepted_full.append(candidate)
        # Prefer distinct papers, then fill remaining positions by relevance.
        selected: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        used_papers: set[str] = set()
        for candidate in accepted_full:
            paper_id = str(candidate.get("paper_id") or "")
            if paper_id and paper_id in used_papers:
                deferred.append(candidate)
                continue
            selected.append(candidate)
            if paper_id:
                used_papers.add(paper_id)
            if len(selected) >= requested:
                break
        if len(selected) < requested:
            for candidate in deferred:
                selected.append(candidate)
                if len(selected) >= requested:
                    break
        accepted_full = selected
        remembered = self._inspected.setdefault(str(section_id), {})
        remembered.update(
            {
                str(item["chunk_id"]): item
                for item in accepted_full
                if item.get("chunk_id")
            }
        )
        return [
            self._compact_visual_candidate(item)
            for item in accepted_full
        ]

    def _write_classifier_cost(self) -> None:
        atomic_write_json(
            self._classifier_cost_path,
            {
                "schema_version": "research_harness.nested_vision_cost.v1",
                "model_tier": self.ctx.classifier_model_tier,
                "calls": self._classifier_calls,
                "input_tokens": self._classifier_input_tokens,
                "output_tokens": self._classifier_output_tokens,
                "estimated_cost_cny": round(self._classifier_cost_cny, 6),
                "hard_budget_cny": self.ctx.classifier_cost_budget_cny,
                "classification_limit": self.ctx.max_pending_classifications,
            },
        )

    def _pending_candidates_for_section(
        self,
        section: Dict[str, Any],
        limit: int,
    ) -> List[Dict[str, Any]]:
        query_text = " ".join(
            str(value or "")
            for value in (
                section.get("title"),
                section.get("argument_role"),
                section.get("chapter_argument"),
            )
        )
        query_tokens = self._aligner._tokenize_for_retrieval(query_text)
        ranked: List[tuple[float, Dict[str, Any]]] = []
        for visual in self._visuals:
            if visual.get("visual_argument_status") != "pending_multimodal_review":
                continue
            image_path = Path(str(visual.get("local_image_path") or ""))
            if not image_path.is_file():
                continue
            candidate_text = " ".join(
                str(visual.get(key) or "")
                for key in ("title", "caption", "search_text", "parent_label")
            )
            candidate_tokens = self._aligner._tokenize_for_retrieval(candidate_text)
            candidate_for_relevance = {
                **visual,
                "tokens": candidate_tokens,
            }
            if not self._record_topic_relevance(candidate_for_relevance):
                continue
            overlap = len(query_tokens & candidate_tokens)
            if query_tokens and overlap == 0:
                continue
            union = max(1, len(query_tokens | candidate_tokens))
            score = overlap / union
            if visual.get("caption"):
                score += 0.03
            ranked.append((score, visual))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(item[1]) for item in ranked[: max(1, limit)]]

    @staticmethod
    def _update_visual_classification(
        candidate: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        sqlite_path = Path(str(candidate.get("_source_kb_sqlite") or ""))
        chunk_id = str(candidate.get("chunk_id") or "")
        if not sqlite_path.is_file() or not chunk_id:
            return
        conn = sqlite3.connect(str(sqlite_path))
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(visual_chunks)").fetchall()
            }
            required = {
                "visual_argument_type",
                "visual_argument_status",
                "visual_argument_confidence",
                "visual_argument_claim",
                "visual_argument_needs_human_review",
                "visual_argument_schema_version",
            }
            if not required.issubset(columns):
                raise RuntimeError("visual_chunks classification schema is incomplete")
            status = (
                "ok"
                if result.get("classification_status") == "ok"
                and result.get("visual_argument_type")
                in VALID_VISUAL_ARGUMENT_TYPES
                else "failed"
            )
            with conn:
                conn.execute(
                    """UPDATE visual_chunks
                       SET visual_argument_type=?,
                           visual_argument_status=?,
                           visual_argument_confidence=?,
                           visual_argument_claim=?,
                           visual_argument_needs_human_review=?,
                           visual_argument_schema_version=?
                       WHERE chunk_id=?""",
                    (
                        str(result.get("visual_argument_type") or ""),
                        status,
                        str(result.get("confidence") or 0.0),
                        str(result.get("visual_argument_claim") or ""),
                        1 if result.get("needs_human_review") else 0,
                        str(result.get("schema_version") or ""),
                        chunk_id,
                    ),
                )
                try:
                    conn.execute(
                        """UPDATE visual_candidate_queue
                           SET status=?, exclusion_reason=?
                           WHERE candidate_visual_id=?""",
                        (
                            "classified_ok" if status == "ok" else "classification_failed",
                            "" if status == "ok" else str(result.get("failure_reason") or ""),
                            chunk_id,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    def get_allowed_tool_names(self) -> List[str]:
        if self.ctx.compact_article_mode:
            return [
                "load_visual_editor_context",
                "inspect_article_visual_candidates",
                "submit_visual_editorial_plan",
                "validate_visual_editorial_plan",
            ]
        return list(self.TOOL_NAMES)

    def get_tools(self, work_dir: Path) -> list:
        provider = self

        def load_visual_editor_context() -> str:
            """Load section roles, figure needs, and corpus visual statistics."""

            sections = []
            for section_id, section in provider._sections.items():
                draft_path = (
                    provider.ctx.review_work_dir
                    / "sections"
                    / section_id
                    / "SECTION_DRAFT_EN.md"
                )
                sections.append(
                    {
                        "section_id": section_id,
                        "title": section.get("title", ""),
                        "argument_role": section.get("argument_role", ""),
                        "chapter_argument": section.get(
                            "chapter_argument",
                            section.get("argument_role", ""),
                        ),
                        "visual_argument_slots": section.get(
                            "visual_argument_slots",
                            section.get("expected_visual_arguments", []),
                        ),
                        "draft_available": draft_path.exists(),
                        "draft_word_count": (
                            len(
                                draft_path.read_text(
                                    encoding="utf-8",
                                    errors="replace",
                                ).split()
                            )
                            if draft_path.exists()
                            else 0
                        ),
                    }
                )
            distribution = provider._aligner.summarize_distribution(
                provider._visuals
            )
            return json.dumps(
                {
                    "status": "ok",
                    "review_thesis": provider.ctx.blueprint.get(
                        "full_review_argument",
                        provider.ctx.blueprint.get("review_thesis", ""),
                    ),
                    "sections": sections,
                    "visual_corpus": distribution,
                    "topic_relevance": {
                        "anchor_token_count": len(
                            provider._topic_anchor_tokens
                        ),
                        "anchor_phrase_count": len(
                            provider._topic_anchor_phrases
                        ),
                        "off_topic_candidates_filtered": len(
                            provider._topic_relevance_rejected_ids
                        ),
                        "policy": (
                            "A candidate must match a distinctive topic "
                            "anchor or an exact topic phrase before it is "
                            "shown to the visual editor."
                        ),
                    },
                    "policy": {
                        "existing_quantitative_figures": (
                            "May be selected only with a verified local path."
                        ),
                        "generated_conceptual_figures": (
                            "May be requested with explicit AI-generation disclosure."
                        ),
                        "generated_quantitative_figures": "Forbidden.",
                        "text_without_a_figure": (
                            "Remains valid; absence of a figure never deletes prose."
                        ),
                    },
                },
                ensure_ascii=True,
            )

        def inspect_section_visual_candidates(
            section_id: str,
            top_k: int = 8,
        ) -> str:
            """Return ranked, canonical visual candidates for one section."""

            if str(section_id) not in provider._sections:
                return json.dumps(
                    {"status": "error", "error": "unknown_section_id"},
                    ensure_ascii=True,
                )
            accepted = provider._verified_candidates_for_section(
                str(section_id),
                top_k=top_k,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "section_id": section_id,
                    "candidate_count": len(accepted),
                    "candidates": accepted,
                },
                ensure_ascii=True,
            )

        def inspect_article_visual_candidates(
            top_k_per_section: int = 6,
            draft_excerpt_characters: int = 220,
        ) -> str:
            """Inspect all sections, compact draft excerpts, and verified visuals at once.

            Prefer this article-level tool over repeatedly calling the two
            section-level read/inspect tools. It reduces model calls and cost
            while preserving the evidence needed for editorial decisions.
            """

            # Start with up to six compact records per section. The provider
            # may retrieve 8 or 12 internally, but the model sees only a
            # diverse shortlist and the complete article payload remains under
            # AgentScope's cached tool-result envelope.  The envelope is set
            # explicitly by the visual editor contract
            # (context_tool_result_limit = 6000 tokens ~= 24 kB); the
            # thresholds below are sized against that, not against the
            # 1800-token ResearchWorker default.
            #
            # The default must equal the clamp ceiling: the model normally
            # calls this tool with no arguments, so a lower default would
            # silently hand back the old two-per-section shortlist and the
            # widened envelope would buy nothing.  The shrink cascade below,
            # not a small default, is what keeps the payload cache-safe.
            top_k = min(max(1, int(top_k_per_section)), 6)
            excerpt_limit = min(
                max(160, int(draft_excerpt_characters)),
                320,
            )
            sections: List[Dict[str, Any]] = []
            for section_id, section in provider._sections.items():
                draft_path = (
                    provider.ctx.review_work_dir
                    / "sections"
                    / section_id
                    / "SECTION_DRAFT_EN.md"
                )
                draft = (
                    draft_path.read_text(encoding="utf-8", errors="replace")
                    if draft_path.is_file()
                    else ""
                )
                candidates = provider._verified_candidates_for_section(
                    section_id,
                    top_k=top_k,
                )
                compact_candidates = [
                    {
                        "visual_chunk_id": str(
                            candidate.get("chunk_id")
                            or candidate.get("visual_chunk_id")
                            or ""
                        ),
                        "paper_id": str(candidate.get("paper_id") or ""),
                        "doi": str(candidate.get("doi") or ""),
                        "paper_title": str(
                            candidate.get("title") or ""
                        )[:100],
                        "caption": str(
                            candidate.get("caption_preview")
                            or candidate.get("caption")
                            or ""
                        )[:160],
                            "visual_argument_type": str(
                                candidate.get("visual_argument_type") or ""
                            ),
                    "visual_argument_status": str(
                        candidate.get("visual_argument_status") or ""
                    ),
                            "score": candidate.get("score", 0.0),
                            "topic_relevance": str(
                                candidate.get("topic_relevance") or "matched"
                            ),
                            "topic_anchor_hits": list(
                                candidate.get("topic_anchor_hits") or []
                            )[:6],
                        }
                    for candidate in candidates
                ]
                sections.append(
                    {
                        "section_id": section_id,
                        "title": str(section.get("title") or ""),
                        "argument_role": str(
                            section.get("argument_role")
                            or section.get("chapter_argument")
                            or ""
                        )[:260],
                        "draft_excerpt": draft[:excerpt_limit],
                        "draft_truncated": len(draft) > excerpt_limit,
                        "candidate_count": len(candidates),
                    "candidates": compact_candidates,
                }
                )
            payload = {
                "status": "ok",
                "section_count": len(sections),
                "all_sections_included": True,
                "sections": sections,
                "instruction": (
                    "Select only non-redundant candidates whose exact "
                    "visual_chunk_id appears in this complete payload. Local "
                    "paths remain in the deterministic tool registry and are "
                    "filled into the accepted plan automatically."
                ),
            }
            encoded = json.dumps(payload, ensure_ascii=True)
            # Fail closed rather than letting the AgentScope cache silently
            # truncate a purportedly complete article map.  The visual
            # contract exposes 6000 tokens (roughly 24 kB of ASCII JSON), so
            # compact optional prose before compacting the candidate records.
            # Candidate count is never reduced here: dropping the fourth or
            # later candidate makes an article-level request look complete
            # while starving the editor's shortlist for late sections.
            if len(encoded) > 21_000:
                for item in sections:
                    item["draft_excerpt"] = item["draft_excerpt"][:120]
                    item["argument_role"] = item["argument_role"][:180]
                    for candidate in item["candidates"]:
                        candidate["paper_title"] = candidate[
                            "paper_title"
                        ][:60]
                        candidate["caption"] = candidate["caption"][:100]
                encoded = json.dumps(payload, ensure_ascii=True)
            if len(encoded) > 22_500:
                for item in sections:
                    item["draft_excerpt"] = item["draft_excerpt"][:80]
                    item["argument_role"] = item["argument_role"][:140]
                    for candidate in item["candidates"]:
                        candidate["paper_title"] = candidate[
                            "paper_title"
                        ][:40]
                        candidate["caption"] = candidate["caption"][:70]
                        candidate["topic_anchor_hits"] = list(
                            candidate.get("topic_anchor_hits") or []
                        )[:3]
                encoded = json.dumps(payload, ensure_ascii=True)
            if len(encoded) > VISUAL_EDITOR_PAYLOAD_CHAR_LIMIT:
                for item in sections:
                    item["draft_excerpt"] = item["draft_excerpt"][:48]
                    item["argument_role"] = item["argument_role"][:100]
                    for candidate in item["candidates"]:
                        candidate["paper_title"] = candidate[
                            "paper_title"
                        ][:28]
                        candidate["caption"] = candidate["caption"][:48]
                        candidate.pop("topic_anchor_hits", None)
                        candidate.pop("visual_argument_status", None)
                        candidate.pop("topic_relevance", None)
                encoded = json.dumps(payload, ensure_ascii=True)
            payload["payload_characters"] = len(encoded)
            payload["cache_safe"] = len(encoded) <= VISUAL_EDITOR_PAYLOAD_CHAR_LIMIT
            return json.dumps(payload, ensure_ascii=True)

        def classify_pending_visual_candidates(
            section_id: str,
            max_items: int = 1,
        ) -> str:
            """Classify a few relevant pending OA visuals only when needed."""

            section = provider._sections.get(str(section_id))
            if section is None:
                return json.dumps(
                    {"status": "error", "error": "unknown_section_id"},
                    ensure_ascii=True,
                )
            remaining_items = (
                provider.ctx.max_pending_classifications
                - provider._pending_classifications_used
            )
            remaining_cost = (
                provider.ctx.classifier_cost_budget_cny
                - provider._classifier_cost_cny
            )
            if remaining_items <= 0 or remaining_cost <= 0.02:
                return json.dumps(
                    {
                        "status": "budget_exhausted",
                        "remaining_classifications": max(0, remaining_items),
                        "remaining_cost_cny": round(max(0.0, remaining_cost), 6),
                    },
                    ensure_ascii=True,
                )
            requested = min(max(1, int(max_items)), 2, remaining_items)
            candidates = provider._pending_candidates_for_section(
                section, requested
            )
            if not candidates:
                return json.dumps(
                    {"status": "no_relevant_pending_candidates", "section_id": section_id},
                    ensure_ascii=True,
                )
            classifier = VisualArgumentClassifier(
                model_tier=provider.ctx.classifier_model_tier,
                output_dir=provider.ctx.work_dir / "classifier",
            )
            outcomes = []
            for candidate in candidates:
                if provider._classifier_cost_cny >= provider.ctx.classifier_cost_budget_cny:
                    break
                result, diagnostics = classifier.classify_chunk(candidate)
                usage = dict(diagnostics.get("_llm_usage") or {})
                input_tokens = int(usage.get("estimated_input_tokens", 0) or 0)
                output_tokens = int(usage.get("estimated_output_tokens", 0) or 0)
                model_name = str(usage.get("model_name") or provider.ctx.classifier_model_tier)
                call_cost = estimate_call_cost_cny(
                    model_name,
                    input_tokens,
                    output_tokens,
                )
                provider._classifier_input_tokens += input_tokens
                provider._classifier_output_tokens += output_tokens
                provider._classifier_calls += 1
                provider._classifier_cost_cny += call_cost
                provider._pending_classifications_used += 1
                provider._update_visual_classification(candidate, result)
                outcomes.append(
                    {
                        "chunk_id": candidate.get("chunk_id", ""),
                        "classification_status": result.get("classification_status", ""),
                        "visual_argument_type": result.get("visual_argument_type", ""),
                        "confidence": result.get("confidence", 0.0),
                        "needs_human_review": result.get("needs_human_review", True),
                    }
                )
            provider._write_classifier_cost()
            provider._reload_visuals()
            return json.dumps(
                {
                    "status": "ok" if outcomes else "no_classification_completed",
                    "section_id": section_id,
                    "outcomes": outcomes,
                    "remaining_classifications": max(
                        0,
                        provider.ctx.max_pending_classifications
                        - provider._pending_classifications_used,
                    ),
                    "estimated_nested_cost_cny": round(
                        provider._classifier_cost_cny, 6
                    ),
                    "next_step": (
                        "Call inspect_section_visual_candidates again to see "
                        "newly accepted candidates."
                    ),
                },
                ensure_ascii=True,
            )

        def read_section_text_for_visuals(
            section_id: str,
            max_characters: int = 12000,
        ) -> str:
            """Read bounded prose so figure choices follow the actual draft."""

            if section_id not in provider._sections:
                return json.dumps(
                    {"status": "error", "error": "unknown_section_id"},
                    ensure_ascii=True,
                )
            path = (
                provider.ctx.review_work_dir
                / "sections"
                / section_id
                / "SECTION_DRAFT_EN.md"
            )
            if not path.exists():
                return json.dumps(
                    {"status": "error", "error": "section_draft_missing"},
                    ensure_ascii=True,
                )
            limit = min(max(1000, int(max_characters)), 20000)
            text = path.read_text(encoding="utf-8", errors="replace")
            return json.dumps(
                {
                    "status": "ok",
                    "section_id": section_id,
                    "text": text[:limit],
                    "truncated": len(text) > limit,
                },
                ensure_ascii=True,
            )

        def submit_visual_editorial_plan(plan_json: str) -> str:
            """Submit the final article-level visual plan as strict JSON.

            Required shape:
            {"placements":[{"section_id":"S01","visual_chunk_id":"exact inspected
            ID","argumentative_purpose":"English explanation of what this figure
            proves or clarifies","placement_guidance":"where it supports the
            prose"}],"conceptual_figure_requests":[{"section_id":"S02",
            "figure_kind":"concept_map|mechanism_schematic|workflow_schematic|
            taxonomy_diagram","argumentative_purpose":"English purpose",
            "generation_brief":"At least forty characters describing a
            non-quantitative conceptual illustration"}],
            "unfilled_visual_needs":[]}
            """

            try:
                value = json.loads(plan_json)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_json: {exc}"},
                    ensure_ascii=True,
                )
            if not isinstance(value, dict):
                return json.dumps(
                    {"status": "error", "error": "plan must be an object"},
                    ensure_ascii=True,
                )
            placements = value.get("placements", [])
            requests = value.get("conceptual_figure_requests", [])
            unfilled = value.get("unfilled_visual_needs", [])
            if not all(
                isinstance(items, list)
                for items in (placements, requests, unfilled)
            ):
                return json.dumps(
                    {"status": "error", "error": "plan lists are malformed"},
                    ensure_ascii=True,
                )

            errors = []
            normalized_placements = []
            used_visuals = set()
            for index, item in enumerate(placements):
                error_count_before = len(errors)
                if not isinstance(item, dict):
                    errors.append(f"placement[{index}] is not an object")
                    continue
                section_id = str(item.get("section_id") or "")
                visual_id = str(item.get("visual_chunk_id") or "")
                canonical = provider._inspected.get(section_id, {}).get(
                    visual_id
                )
                if canonical is None:
                    errors.append(
                        f"placement[{index}] was not returned by section inspection"
                    )
                    continue
                if visual_id in used_visuals:
                    errors.append(
                        f"placement[{index}] reuses a visual without justification"
                    )
                    continue
                used_visuals.add(visual_id)
                purpose = str(item.get("argumentative_purpose") or "").strip()
                if len(purpose) < 20 or _CJK.search(purpose):
                    errors.append(
                        f"placement[{index}] lacks an English argumentative purpose"
                    )
                if len(errors) == error_count_before:
                    normalized_placements.append(
                        {
                            "section_id": section_id,
                            "visual_chunk_id": visual_id,
                            "paper_id": canonical.get("paper_id", ""),
                            "doi": canonical.get("doi", ""),
                            "local_image_path": canonical.get(
                                "local_image_path", ""
                            ),
                            "visual_argument_type": canonical.get(
                                "visual_argument_type", ""
                            ),
                            "caption_preview": canonical.get(
                                "caption_preview", ""
                            ),
                            "argumentative_purpose": purpose,
                            "placement_guidance": str(
                                item.get("placement_guidance") or ""
                            ).strip(),
                            "composite_group_id": str(
                                item.get("composite_group_id") or ""
                            ).strip(),
                            "panel_role": str(
                                item.get("panel_role") or ""
                            ).strip(),
                            "priority": str(
                                item.get("priority") or "medium"
                            ).strip(),
                            "figure_kind": str(
                                item.get("figure_kind")
                                or "source_figure"
                            ).strip(),
                            "status": (
                                "verified_existing"
                                if str(
                                    canonical.get(
                                        "visual_argument_status"
                                    )
                                    or ""
                                )
                                == "ok"
                                else "traceable_source_pending_review"
                            ),
                        }
                    )

            normalized_requests = []
            for index, item in enumerate(requests):
                error_count_before = len(errors)
                if not isinstance(item, dict):
                    errors.append(f"request[{index}] is not an object")
                    continue
                section_id = str(item.get("section_id") or "")
                if section_id not in provider._sections:
                    errors.append(f"request[{index}] has unknown section")
                kind = str(item.get("figure_kind") or "")
                if kind not in {
                    "concept_map",
                    "mechanism_schematic",
                    "workflow_schematic",
                    "taxonomy_diagram",
                    "data_infographic",
                    "trend_schematic",
                    "comparison_diagram",
                }:
                    errors.append(
                        f"request[{index}] is not an allowed conceptual kind"
                    )
                purpose = str(item.get("argumentative_purpose") or "")
                prompt = str(item.get("generation_brief") or "")
                if len(purpose) < 20 or len(prompt) < 40:
                    errors.append(
                        f"request[{index}] lacks purpose or generation brief"
                    )
                provenance = str(
                    item.get("data_provenance_level") or "schematic"
                ).lower()
                if provenance not in {
                    "exact",
                    "approximate",
                    "schematic",
                }:
                    errors.append(
                        f"request[{index}] has invalid data provenance level"
                    )
                input_data = item.get("input_data", {})
                if input_data not in ({}, None) and not isinstance(
                    input_data,
                    (dict, list),
                ):
                    errors.append(
                        f"request[{index}] input_data must be an object or list"
                    )
                if len(errors) == error_count_before:
                    normalized_requests.append(
                        {
                            "section_id": section_id,
                            "figure_kind": kind,
                            "argumentative_purpose": purpose,
                            "generation_brief": prompt,
                            "placement_guidance": str(
                                item.get("placement_guidance") or ""
                            ).strip(),
                            "data_provenance_level": provenance,
                            "input_data": input_data or {},
                            "approximate_data_allowed": bool(
                                item.get(
                                    "approximate_data_allowed",
                                    provenance in {
                                        "approximate",
                                        "schematic",
                                    },
                                )
                            ),
                            "priority": str(
                                item.get("priority") or "medium"
                            ).strip(),
                            "required_disclosure": (
                                "AI-generated explanatory visual"
                            ),
                            "status": "pending_generation_and_review",
                        }
                    )
            (
                normalized_requests,
                budget_dropped_requests,
            ) = _apply_generation_portfolio_budget(normalized_requests)
            normalized_unfilled = [
                dict(item)
                for item in unfilled
                if isinstance(item, dict)
                and str(item.get("section_id") or "") in provider._sections
            ]
            normalized_unfilled.extend(
                {
                    "section_id": request.get("section_id", ""),
                    "argumentative_purpose": request.get(
                        "argumentative_purpose", ""
                    ),
                    "reason": GENERATION_PORTFOLIO_BUDGET_REASON,
                    "status": "unfilled_requires_editorial_decision",
                    "priority": request.get("priority", "medium"),
                    "figure_kind": request.get("figure_kind", ""),
                }
                for request in budget_dropped_requests
            )
            accounted_sections = {
                str(item.get("section_id") or "")
                for items in (
                    normalized_placements,
                    normalized_requests,
                    normalized_unfilled,
                )
                for item in items
                if isinstance(item, dict) and item.get("section_id")
            }
            # A missing figure must never disappear from the audit trail merely
            # because the model omitted it.  This deterministic fallback does
            # not invent an image or force one into the article; it records the
            # unmet blueprint contract so later acquisition, generation, or
            # human review can address it.
            for section_id in sorted(
                provider._expected_visual_section_ids() - accounted_sections
            ):
                section = provider._sections[section_id]
                slots = (
                    section.get("visual_argument_slots")
                    or section.get("expected_visual_arguments")
                    or []
                )
                first_slot = slots[0] if slots else {}
                if isinstance(first_slot, dict):
                    purpose = str(
                        first_slot.get("purpose")
                        or first_slot.get("argumentative_purpose")
                        or ""
                    )
                else:
                    purpose = str(first_slot)
                normalized_unfilled.append(
                    {
                        "section_id": section_id,
                        "argumentative_purpose": purpose,
                        "reason": (
                            "no_verified_existing_or_approved_conceptual_"
                            "visual_selected"
                        ),
                        "status": "unfilled_requires_editorial_decision",
                    }
                )
            normalized = {
                "schema_version": "research_harness.visual_editorial_plan.v1",
                "input_fingerprint": provider.ctx.input_fingerprint,
                "placements": normalized_placements,
                "conceptual_figure_requests": normalized_requests,
                "unfilled_visual_needs": normalized_unfilled,
                "policy": {
                    "reader_explanation_mode": True,
                    "approximate_or_schematic_data_requires_disclosure": True,
                    "missing_visual_does_not_invalidate_text": True,
                },
                "generation_policy": {
                    "max_conceptual_figure_requests": (
                        MAX_CONCEPTUAL_FIGURE_REQUESTS
                    ),
                    "one_generated_request_per_section": True,
                    "source_placements_not_counted": True,
                    "cap_is_upper_bound_not_target": True,
                },
            }
            if errors:
                atomic_write_json(
                    provider.rejected_plan_path,
                    {
                        "schema_version": (
                            "research_harness.visual_editorial_rejection.v1"
                        ),
                        "errors": errors[:20],
                        "valid_partial_plan": normalized,
                    },
                )
                return json.dumps(
                    {
                        "status": "error",
                        "errors": errors[:20],
                        "safe_partial_saved": True,
                        "recovery_policy": (
                            "Correct only the invalid items and resubmit. If "
                            "the task budget ends, the system may retain the "
                            "already verified subset without inventing replacements."
                        ),
                    },
                    ensure_ascii=True,
                )

            atomic_write_json(provider.plan_path, normalized)
            return json.dumps(
                {
                    "status": "ok",
                    "artifact": provider.plan_path.name,
                    "placement_count": len(normalized_placements),
                    "conceptual_request_count": len(normalized_requests),
                },
                ensure_ascii=True,
            )

        def validate_visual_editorial_plan() -> str:
            """Validate paths, provenance, disclosure, and non-fabrication policy."""
            return validate_visual_editorial_plan_file(
                provider.plan_path,
                provider.ctx.input_fingerprint,
                provider._expected_visual_section_ids(),
            )

        return [
            FunctionTool(load_visual_editor_context),
            FunctionTool(inspect_article_visual_candidates),
            FunctionTool(inspect_section_visual_candidates),
            FunctionTool(classify_pending_visual_candidates),
            FunctionTool(read_section_text_for_visuals),
            FunctionTool(submit_visual_editorial_plan),
            FunctionTool(validate_visual_editorial_plan),
        ]
