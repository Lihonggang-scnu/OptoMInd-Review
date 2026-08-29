"""Additive visual procurement bridge for ``visual_material_gap`` tasks.

This module is the missing bridge between an existing supplementary
retrieval result and the long-term visual cache.  It deliberately reuses the
production modules that already own the work:

* the existing literature retrieval/materialization callbacks from
  ``supplementary_retrieval_pipeline`` (delegated, never re-implemented);
* ``candidates_from_staging_kb`` / ``ingest_visual_candidates`` from
  ``visual_cache_ingest`` for discovery and durable unit construction;
* ``VisualCacheStore`` from ``visual_cache_store`` for immutable,
  self-contained snapshot publication;
* ``VisualArgumentClassifier`` (via the bundled production reviewer helper)
  for bounded, non-approving model review.

No new extractor is introduced: visual extraction already happens after
OA/S2 fulltext acquisition and writes ``visual_chunks`` /
``visual_assets`` / ``visual_candidate_queue`` into the runtime KB.

Fail-open rules:

* Missing visual procurement never blocks manuscript text; a run with no
  valid new figure returns ``adequate=False`` no-progress metadata instead
  of raising.
* Reviewer/classification failures are isolated per candidate and reported;
  unreviewed candidates still enter the cache with pending approval.
* Model-reviewed assets may enter the long-term cache but stay pending
  unless an explicit existing approval marker (default ``human_approved``)
  is present on the candidate.
* Only contract/programmer errors raise: invalid config, non-visual task or
  wrong route, malformed reviewer result, or snapshot publication failure.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .supplementary_retrieval_contract import SupplementaryRetrievalTask
from .supplementary_retrieval_service import (
    ROUTE_VISUAL,
    MaterializationOutcome,
    RetrievalOutcome,
)
from .visual_cache_ingest import (
    candidates_from_staging_kb,
    ingest_visual_candidates,
)
from .visual_cache_schemas import (
    APPROVED_MARKERS_DEFAULT,
    REJECTED_MARKERS_DEFAULT,
    validate_version,
)
from .visual_cache_store import (
    VisualCachePublicationError,
    VisualCacheStore,
)
from .visual_asset_planner_adapter import (
    run_article_visual_asset_planner,
)


VISUAL_PROCUREMENT_SCHEMA_VERSION = "optomind.visual_procurement.v1"
VISUAL_PROCUREMENT_CONFIG_SCHEMA_VERSION = (
    "optomind.visual_procurement.config.v1"
)
VISUAL_PROCUREMENT_TO_PLANNING_SCHEMA_VERSION = (
    "optomind.visual_procurement_to_planning.v1"
)
MATERIALIZED_ROUTE = "visual_long_term_cache_increment"
DEFAULT_REVIEW_CAP = 8
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class VisualProcurementContractError(ValueError):
    """Raised for contract/programmer errors, never for missing visuals."""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VisualProcurementConfig:
    """Additive procurement contract: bounded review, explicit versions."""

    schema_version: str = VISUAL_PROCUREMENT_CONFIG_SCHEMA_VERSION
    review_cap: int = DEFAULT_REVIEW_CAP
    snapshot_version: str = ""
    parent_version: str = ""
    delegate_literature_materialization: bool = True
    copy_parent_assets: bool = True
    approve_markers: frozenset[str] = frozenset(APPROVED_MARKERS_DEFAULT)
    reject_markers: frozenset[str] = frozenset(REJECTED_MARKERS_DEFAULT)
    source_root: str = ""

    def validate(self) -> list[str]:
        """Return contract violations (empty means valid)."""

        errors: list[str] = []
        if not isinstance(self.review_cap, int) or self.review_cap < 0:
            errors.append("review_cap_must_be_non_negative_integer")
        for label, version in (
            ("snapshot_version", self.snapshot_version),
            ("parent_version", self.parent_version),
        ):
            if not version:
                continue
            try:
                validate_version(version)
            except Exception as exc:
                errors.append(f"invalid_{label}:{exc}")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "review_cap": self.review_cap,
            "snapshot_version": self.snapshot_version,
            "parent_version": self.parent_version,
            "delegate_literature_materialization": (
                self.delegate_literature_materialization
            ),
            "copy_parent_assets": self.copy_parent_assets,
            "approve_markers": sorted(set(self.approve_markers)),
            "reject_markers": sorted(set(self.reject_markers)),
            "source_root": self.source_root,
        }


@dataclass(slots=True)
class VisualReviewBatch:
    """Result of one injected batch reviewer call.

    ``records`` must be one reviewed candidate per input candidate, in the
    same order.  Per-candidate failures keep the original record and are
    reported in ``errors`` so they are isolated and never raise.
    """

    records: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return _text(
        candidate.get("chunk_id")
        or candidate.get("asset_id")
        or candidate.get("candidate_visual_id")
        or candidate.get("visual_id")
        or candidate.get("figure_id")
    ) or "<unknown>"


def _candidate_identity_keys(candidate: Mapping[str, Any]) -> set[str]:
    return {
        _text(candidate.get(key))
        for key in (
            "candidate_visual_id",
            "chunk_id",
            "asset_id",
            "visual_id",
            "figure_id",
            "unit_id",
        )
        if _text(candidate.get(key))
    }


def _bounded_candidates(
    candidates: Sequence[Mapping[str, Any]],
    review_cap: int | None,
) -> list[dict[str, Any]]:
    rows = [dict(candidate) for candidate in candidates]
    if review_cap is None or review_cap < 0:
        return rows
    return rows[:review_cap]


REVIEW_SOURCE_DIVERSITY = 2
_CAPTION_UNAVAILABLE_MARKER = "caption unavailable"


def _review_query_terms(
    task: Any | None,
    context: Mapping[str, Any] | None,
) -> set[str]:
    """Deterministic query terms from the task and its context."""

    terms: set[str] = set()
    values: list[str] = []
    if task is not None:
        values.extend(getattr(task, "retrieval_queries", ()) or ())
        metadata = getattr(task, "metadata", None)
        if isinstance(metadata, Mapping):
            values.append(_text(metadata.get("user_question")))
    if isinstance(context, Mapping):
        values.append(_text(context.get("user_question")))
        section_task = context.get("section_task")
        if isinstance(section_task, Mapping):
            values.append(_text(section_task.get("task")))
    for value in values:
        terms.update(
            re.findall(r"[a-z0-9]{3,}", str(value or "").casefold())
        )
    return terms


def _has_real_caption(candidate: Mapping[str, Any]) -> bool:
    """True when the candidate carries a real caption, never a placeholder."""

    for key in (
        "caption_original",
        "caption_clean",
        "caption",
        "caption_text",
    ):
        value = _text(candidate.get(key))
        if value and _CAPTION_UNAVAILABLE_MARKER not in value.casefold():
            return True
    return False


def _candidate_review_score(
    candidate: Mapping[str, Any],
    query_terms: set[str],
) -> float:
    """Deterministic relevance/quality score for the paid-review ranking."""

    score = 0.0
    text = " ".join(
        [
            str(candidate.get("title") or ""),
            str(candidate.get("caption") or ""),
            str(candidate.get("search_text") or ""),
            " ".join(str(value) for value in (candidate.get("labels") or [])),
            str(candidate.get("visual_argument_claim") or ""),
        ]
    ).casefold()
    text_terms = set(re.findall(r"[a-z0-9]{3,}", text))
    if query_terms:
        score += len(query_terms & text_terms) / max(1, len(query_terms))
    if _has_real_caption(candidate):
        score += 0.5
    if str(candidate.get("visual_argument_type") or ""):
        score += 0.3
    if str(candidate.get("visual_argument_confidence") or "").lower() == "high":
        score += 0.2
    if str(candidate.get("review_utility") or "").lower() == "high":
        score += 0.2
    if candidate.get("body_callout_texts") or candidate.get(
        "linked_text_chunk_ids"
    ):
        score += 0.2
    permission = candidate.get("permission")
    permission_status = (
        str(permission.get("status") or "")
        if isinstance(permission, Mapping)
        else str(candidate.get("permission_status") or "")
    )
    if permission_status.lower() in {"allowed", "open_access"}:
        score += 0.2
    return round(score, 6)


def rank_review_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    task: Any | None = None,
    context: Mapping[str, Any] | None = None,
    queries: Sequence[str] = (),
    review_cap: int | None = None,
    max_per_source: int = REVIEW_SOURCE_DIVERSITY,
) -> list[dict[str, Any]]:
    """Deterministically rank candidates for the bounded paid-review subset.

    Ranking uses query/task relevance and useful metadata (real caption,
    argument type, confidence, review utility, permission) with stable
    tie-breaking, then applies source-paper diversity.  The placeholder
    caption never earns caption-quality credit.  This is a review *selection*
    ranker only; every candidate still reaches dedupe/ingest.
    """

    query_terms = _review_query_terms(task, context)
    for query in queries:
        query_terms.update(
            re.findall(r"[a-z0-9]{3,}", str(query or "").casefold())
        )
    rows = [dict(candidate) for candidate in candidates]
    scored = sorted(
        (
            (
                _candidate_review_score(candidate, query_terms),
                _candidate_id(candidate),
                candidate,
            )
            for candidate in rows
        ),
        key=lambda item: (-item[0], item[1]),
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    per_source: dict[str, int] = {}
    deferred: list[tuple[float, str, dict[str, Any]]] = []
    for _score, candidate_id, candidate in scored:
        if review_cap is not None and len(selected) >= review_cap:
            break
        source = str(
            candidate.get("paper_id")
            or candidate.get("doi")
            or candidate_id
        )
        if per_source.get(source, 0) >= max(1, int(max_per_source)):
            deferred.append((_score, candidate_id, candidate))
            continue
        selected.append(candidate)
        selected_ids.add(candidate_id)
        per_source[source] = per_source.get(source, 0) + 1
    # Diversity is best-effort: backfill the remaining review budget from
    # deferred same-source candidates so the cap is still used when reached.
    if review_cap is not None:
        for _score, candidate_id, candidate in deferred:
            if len(selected) >= review_cap:
                break
            if candidate_id in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(candidate_id)
    return selected


def _resolve_candidate_image(
    candidate: Mapping[str, Any],
    source_root: Path | None,
    extra_roots: Mapping[str, Path],
) -> Path | None:
    raw = _text(
        candidate.get("local_image_path")
        or candidate.get("image_path")
        or candidate.get("local_path")
        or candidate.get("path")
    )
    if not raw:
        return None
    direct = Path(raw)
    if direct.is_file():
        return direct
    roots: list[Path] = [source_root] if source_root is not None else []
    roots.extend(extra_roots.values())
    for root in roots:
        candidate_path = Path(root) / raw
        if candidate_path.is_file():
            return candidate_path
    return None


def _resolve_source_root(
    config: VisualProcurementConfig,
    retrieval_metadata: Mapping[str, Any],
) -> Path | None:
    if config.source_root:
        return Path(config.source_root)
    for key in ("source_root", "runtime_source_root", "fulltext_root", "oa_root"):
        value = retrieval_metadata.get(key)
        if value:
            return Path(str(value))
    return None


def _build_path_roots(
    config: VisualProcurementConfig,
    retrieval_metadata: Mapping[str, Any],
    runtime_kb: Path | None,
) -> dict[str, Path]:
    """Collect every safe root for resolving relative candidate paths.

    Resolution order reflects specificity: explicit config root, retrieval
    metadata roots, the runtime KB directory, then the repository root and
    the current working directory as safe fallbacks.  Duplicate resolved
    paths are collapsed (first key wins).
    """

    roots: dict[str, Path] = {}
    if config.source_root:
        roots["config_source"] = Path(config.source_root)
    for key in (
        "source_root",
        "runtime_source_root",
        "fulltext_root",
        "oa_root",
        "work_dir",
        "work_root",
    ):
        value = retrieval_metadata.get(key)
        if value:
            roots[f"metadata_{key}"] = Path(str(value))
    if runtime_kb is not None and runtime_kb.parent.is_dir():
        roots["kb"] = runtime_kb.parent
    roots["repo"] = PROJECT_ROOT
    try:
        roots["cwd"] = Path.cwd()
    except OSError:
        pass
    unique: dict[str, Path] = {}
    seen: set[str] = set()
    for key, path in roots.items():
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path.absolute())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique[key] = path
    return unique


def _merge_classification(
    candidate: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a classifier result without granting human approval."""

    updated = dict(candidate)
    for key in ("visual_argument_type", "visual_argument_claim", "supported_aspect"):
        value = _text(result.get(key))
        if value:
            updated[key] = value
    confidence = result.get("confidence")
    if confidence is not None:
        updated["visual_argument_confidence"] = str(confidence)
    needs_human_review = bool(result.get("needs_human_review", True))
    updated["visual_argument_needs_human_review"] = needs_human_review
    updated["visual_argument_schema_version"] = (
        _text(result.get("schema_version"))
        or "visual_argument_classification.v1"
    )
    updated["secondary_visual_argument_types"] = [
        str(item)
        for item in (result.get("secondary_visual_argument_types") or [])
        if str(item).strip()
    ]
    updated["argument_basis"] = [
        str(item)
        for item in (result.get("argument_basis") or [])
        if str(item).strip()
    ]
    existing_flags = [
        str(item) for item in (candidate.get("review_flags") or [])
    ]
    risk_flags = [
        str(item) for item in (result.get("risk_flags") or [])
    ]
    updated["review_flags"] = list(
        dict.fromkeys([*existing_flags, *risk_flags])
    )[:16]
    classified_ok = (
        _text(result.get("classification_status")) == "ok"
        and not needs_human_review
    )
    updated["visual_argument_status"] = (
        "ok" if classified_ok else "pending_multimodal_review"
    )
    # Never invent approval: review_decision/status/human_review_status are
    # left untouched so approval stays pending unless an explicit existing
    # marker is present on the candidate.
    return updated


def review_with_visual_argument_classifier(
    candidates: Sequence[Mapping[str, Any]],
    *,
    classifier: Any | None = None,
    review_cap: int | None = None,
) -> VisualReviewBatch:
    """Production reviewer: bounded ``VisualArgumentClassifier`` application.

    Classification failures are isolated per candidate and reported in
    ``errors``; failed candidates keep their original record with a pending
    review state.  No approval is granted here.
    """

    from optomind_research.visual_argument_classifier import (
        VisualArgumentClassifier,
    )

    classifier = classifier or VisualArgumentClassifier(
        model_tier="vision_premium_model",
    )
    bounded = _bounded_candidates(candidates, review_cap)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    for candidate in bounded:
        candidate_id = _candidate_id(candidate)
        updated = dict(candidate)
        try:
            result, diagnostics = classifier.classify_chunk(updated)
        except Exception as exc:
            errors.append(
                {
                    "candidate_id": candidate_id,
                    "reason": (
                        f"classifier_exception:{type(exc).__name__}:{exc}"
                    ),
                }
            )
            updated["visual_argument_status"] = "pending_multimodal_review"
            records.append(updated)
            continue
        if _text(result.get("classification_status")) == "failed":
            errors.append(
                {
                    "candidate_id": candidate_id,
                    "reason": _text(result.get("failure_reason"))
                    or "classification_failed",
                }
            )
            updated["visual_argument_status"] = "pending_multimodal_review"
            records.append(updated)
            continue
        records.append(_merge_classification(updated, result))
        llm_usage = _mapping(diagnostics.get("_llm_usage"))
        if llm_usage:
            usage.append(
                {
                    **dict(llm_usage),
                    "candidate_id": candidate_id,
                    "agent": "VisualArgumentClassifier",
                }
            )
    return VisualReviewBatch(records=records, errors=errors, usage=usage)


def _guard_visual_route(
    task: SupplementaryRetrievalTask,
    execution_meta: Mapping[str, Any] | None = None,
    *,
    route: str = ROUTE_VISUAL,
) -> None:
    if task.gap_type != "visual_material_gap":
        raise VisualProcurementContractError(
            "visual procurement requires a visual_material_gap task"
        )
    if not task.is_visual():
        raise VisualProcurementContractError(
            "visual procurement requires a visual_route task"
        )
    meta_route = _text((execution_meta or {}).get("route"))
    if meta_route and meta_route != route:
        raise VisualProcurementContractError(
            f"visual route mismatch: expected {route}, got {meta_route}"
        )


def make_visual_retrieve_callback(
    literature_retrieve: Callable[..., Any] | None,
    *,
    route: str = ROUTE_VISUAL,
) -> Callable[..., RetrievalOutcome]:
    """Wrap the existing literature retrieval callback for the visual route.

    Delegation preserves the S2/OA/fulltext retrieval and the runtime KB it
    populates; the wrapper only enforces ``visual_material_gap``/visual route
    identity and normalizes the outcome route.
    """

    if literature_retrieve is None:
        raise VisualProcurementContractError(
            "literature_retrieve callback is required"
        )

    def visual_retrieve(
        task: SupplementaryRetrievalTask,
        queries: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> RetrievalOutcome:
        _guard_visual_route(task, execution_meta, route=route)
        outcome = literature_retrieve(task, queries, context, execution_meta)
        if not isinstance(outcome, RetrievalOutcome):
            raise TypeError(
                "literature retrieval callback returned "
                f"{type(outcome).__name__}, expected RetrievalOutcome"
            )
        return RetrievalOutcome(
            candidates=[
                dict(candidate)
                for candidate in outcome.candidates
                if isinstance(candidate, Mapping)
            ],
            adequate=bool(outcome.adequate),
            query_runs=[
                dict(run)
                for run in outcome.query_runs
                if isinstance(run, Mapping)
            ],
            metadata=dict(outcome.metadata),
            route=route,
        )

    return visual_retrieve


def _run_literature_delegate(
    *,
    literature_materialize: Callable[..., Any] | None,
    task: SupplementaryRetrievalTask,
    retrieval: RetrievalOutcome,
    context: Mapping[str, Any],
    execution_meta: Mapping[str, Any],
    config: VisualProcurementConfig,
) -> dict[str, Any]:
    if (
        not config.delegate_literature_materialization
        or literature_materialize is None
    ):
        return {"delegated": False}
    try:
        outcome = literature_materialize(
            task,
            retrieval,
            context,
            execution_meta,
        )
    except Exception as exc:
        return {
            "delegated": True,
            "adequate": False,
            "error": f"{type(exc).__name__}:{exc}",
        }
    if not isinstance(outcome, MaterializationOutcome):
        return {
            "delegated": True,
            "adequate": False,
            "error": "expected MaterializationOutcome",
        }
    metadata = dict(outcome.metadata)
    return {
        "delegated": True,
        "adequate": bool(outcome.adequate),
        "materialized_route": outcome.materialized_route,
        "reason": _text(metadata.get("reason")),
        "work_dir": _text(metadata.get("work_dir")),
        "total_references": int(outcome.total_references),
        "qwen_usage": [
            dict(row)
            for row in (metadata.get("qwen_usage") or [])
            if isinstance(row, Mapping)
        ],
        "embedding_usage": dict(metadata.get("embedding_usage") or {}),
        "metadata": metadata,
    }


def _copy_parent_assets(
    parent_assets_dir: Path,
    destination_assets_dir: Path,
) -> None:
    destination_assets_dir.mkdir(parents=True, exist_ok=True)
    if not parent_assets_dir.is_dir():
        return
    for source in sorted(parent_assets_dir.iterdir()):
        if not source.is_file():
            continue
        target = destination_assets_dir / source.name
        if target.exists() and _sha256_file(target) != _sha256_file(source):
            raise VisualProcurementContractError(
                f"parent asset name collision with different content: "
                f"{source.name}"
            )
        if not target.exists():
            shutil.copy2(source, target)


def _dedupe_against_parent(
    candidates: Sequence[Mapping[str, Any]],
    parent_units: Sequence[Mapping[str, Any]],
    *,
    source_root: Path | None,
    extra_roots: Mapping[str, Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Dedupe by durable unit id / asset id / image identity."""

    parent_unit_ids: set[str] = {
        _text(unit.get("unit_id")) for unit in parent_units
    }
    parent_asset_ids: set[str] = {
        _text((unit.get("figure_identity") or {}).get("asset_id"))
        for unit in parent_units
    }
    parent_shas: set[str] = {
        _text((unit.get("hashes") or {}).get("image_sha256"))
        for unit in parent_units
    }
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        ids = _candidate_identity_keys(candidate)
        image = _resolve_candidate_image(
            candidate,
            source_root,
            extra_roots,
        )
        image_sha = _sha256_file(image) if image is not None else ""
        matches: list[str] = sorted(ids & parent_unit_ids) or sorted(
            ids & parent_asset_ids
        )
        if image_sha and image_sha in parent_shas and "image_sha256" not in matches:
            matches.append("image_sha256")
        if ids & parent_unit_ids or ids & parent_asset_ids or (
            image_sha and image_sha in parent_shas
        ):
            duplicates.append(
                {
                    "candidate_id": candidate_id,
                    "matches": matches,
                    "image_sha256": image_sha,
                }
            )
            continue
        kept.append(dict(candidate))
    return kept, duplicates


def _dedupe_new_units_against_parent(
    new_units: Sequence[Mapping[str, Any]],
    parent_units: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    parent_unit_ids = {_text(unit.get("unit_id")) for unit in parent_units}
    parent_shas = {
        _text((unit.get("hashes") or {}).get("image_sha256"))
        for unit in parent_units
    }
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for unit in new_units:
        unit_id = _text(unit.get("unit_id"))
        image_sha = _text((unit.get("hashes") or {}).get("image_sha256"))
        if unit_id in parent_unit_ids or (
            image_sha and image_sha in parent_shas
        ):
            dropped.append(unit_id)
            continue
        kept.append(dict(unit))
    return kept, dropped


def resolve_next_snapshot_version(
    parent_version: str | None,
    *,
    configured: str = "",
) -> str:
    """Deterministic next version, or a caller-provided validated version."""

    if configured:
        return validate_version(configured)
    if not parent_version:
        return "snapshot-0001"
    match = re.fullmatch(r"^(.*?)(\d+)$", str(parent_version))
    if not match:
        return "snapshot-0001"
    prefix, digits = match.group(1), match.group(2)
    return f"{prefix}{int(digits) + 1:0{len(digits)}d}"


def _unit_summary(unit: Mapping[str, Any]) -> dict[str, Any]:
    figure = _mapping(unit.get("figure_identity"))
    source = _mapping(unit.get("source_identity"))
    paths = _mapping(unit.get("paths"))
    approval = _mapping(unit.get("approval"))
    permission = _mapping(unit.get("permission_state"))
    return {
        "unit_id": _text(unit.get("unit_id")),
        "visual_chunk_id": _text(figure.get("asset_id"))
        or _text(unit.get("unit_id")),
        "paper_id": _text(source.get("paper_id"))
        or _text(source.get("doi")),
        "asset_kind": _text(figure.get("asset_kind"))
        or _text(_mapping(unit.get("asset_typing")).get("asset_kind"))
        or "figure",
        "publication_eligible": bool(
            permission.get("publication_eligible")
        ),
        "image_ref": _text(
            _mapping(paths.get("image_ref")).get("relative")
        ),
        "approval_state": _text(approval.get("state")),
    }


def _no_progress_outcome(
    *,
    execution_meta: Mapping[str, Any],
    reason: str,
    candidate_counts: Mapping[str, int],
    text_materialization: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    parent_version: str,
    parent_unit_count: int = 0,
    parent_snapshot_dir: str = "",
    review_cap: int | None = None,
    review_usage: Sequence[Mapping[str, Any]] = (),
    dedupe: Sequence[Mapping[str, Any]] | None = None,
    ingest: Mapping[str, Any] | None = None,
) -> MaterializationOutcome:
    review_usage_rows = [dict(row) for row in review_usage]
    metadata: dict[str, Any] = {
        "schema_version": VISUAL_PROCUREMENT_SCHEMA_VERSION,
        "execution_meta": dict(execution_meta),
        "reason": reason,
        "adequate": False,
        "candidate_counts": dict(candidate_counts),
        "review": {
            "cap": review_cap,
            "errors": [dict(e) for e in errors],
            "usage": review_usage_rows,
            "skipped": int(candidate_counts.get("review_skipped") or 0),
        },
        "parent": {
            "version": parent_version,
            "unit_count": parent_unit_count,
            "snapshot_dir": parent_snapshot_dir,
        },
        "snapshot": {},
        "text_materialization": dict(text_materialization),
        "ingest": dict(ingest) if ingest is not None else {},
        "usage": {
            "text_materialization": {
                "qwen_usage": [
                    dict(row)
                    for row in (
                        text_materialization.get("qwen_usage") or []
                    )
                    if isinstance(row, Mapping)
                ],
                "embedding_usage": dict(
                    text_materialization.get("embedding_usage") or {}
                ),
            },
            "visual_review": review_usage_rows,
        },
        "errors": [dict(e) for e in errors],
        "warnings": list(warnings),
    }
    if dedupe is not None:
        metadata["candidate_counts"]["duplicates_against_parent"] = len(
            dedupe
        )
        metadata["dedupe"] = {"duplicates_against_parent": [dict(d) for d in dedupe]}
    return MaterializationOutcome(
        sources=[],
        adequate=False,
        total_references=0,
        background_only_references=0,
        materialized_route=MATERIALIZED_ROUTE,
        metadata=metadata,
    )


def run_visual_procurement(
    *,
    task: SupplementaryRetrievalTask,
    retrieval: RetrievalOutcome,
    context: Mapping[str, Any],
    execution_meta: Mapping[str, Any],
    cache_store: VisualCacheStore,
    reviewer: Callable[[Sequence[Mapping[str, Any]]], VisualReviewBatch]
    | None = None,
    literature_materialize: Callable[..., Any] | None = None,
    config: VisualProcurementConfig | None = None,
) -> MaterializationOutcome:
    """Execute one visual procurement run and return a detailed outcome."""

    cfg = config or VisualProcurementConfig()
    config_errors = cfg.validate()
    if config_errors:
        raise VisualProcurementContractError(
            "invalid visual procurement config: "
            + "; ".join(config_errors)
        )
    _guard_visual_route(task, execution_meta, route=ROUTE_VISUAL)
    if retrieval.route and retrieval.route != ROUTE_VISUAL:
        raise VisualProcurementContractError(
            "visual materialization requires a visual-route retrieval outcome"
        )
    if not isinstance(cache_store, VisualCacheStore):
        raise VisualProcurementContractError(
            "cache_store must be a VisualCacheStore"
        )

    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    metadata = retrieval.metadata
    work_dir_raw = _text(metadata.get("work_dir"))
    work_dir = Path(work_dir_raw) if work_dir_raw else None
    runtime_kb = Path(_text(metadata.get("runtime_kb_sqlite")))
    source_root = _resolve_source_root(cfg, metadata)
    extra_roots = _build_path_roots(cfg, metadata, runtime_kb)

    text_materialization = _run_literature_delegate(
        literature_materialize=literature_materialize,
        task=task,
        retrieval=retrieval,
        context=context,
        execution_meta=execution_meta,
        config=cfg,
    )
    if _text(text_materialization.get("error")):
        errors.append(
            {
                "candidate_id": "<task>",
                "reason": "text_materialization_failed:"
                + text_materialization["error"],
            }
        )

    parent_version = cfg.parent_version or (
        cache_store.latest_version() or ""
    )
    parent_units: list[dict[str, Any]] = []
    parent_snapshot_dir: Path | None = None
    if parent_version:
        parent_snapshot_dir = cache_store.snapshot_path(parent_version)
        parent_units = [
            dict(unit)
            for unit in (cache_store.load_snapshot(parent_version) or {}).get(
                "units"
            )
            or []
            if isinstance(unit, Mapping)
        ]

    candidates = (
        candidates_from_staging_kb(runtime_kb)
        if runtime_kb.is_file()
        else []
    )
    if not candidates:
        if not runtime_kb.is_file():
            warnings.append("runtime_kb_sqlite_missing")
        return _no_progress_outcome(
            execution_meta=execution_meta,
            reason="no_visual_candidates",
            candidate_counts={
                "discovered": 0,
                "reviewed": 0,
                "review_skipped": 0,
                "review_errors": 0,
                "duplicates_against_parent": 0,
                "ingested": 0,
                "ingest_errors": 0,
                "published_new_units": 0,
            },
            text_materialization=text_materialization,
            errors=errors,
            warnings=warnings,
            parent_version=parent_version,
            parent_unit_count=len(parent_units),
            parent_snapshot_dir=str(parent_snapshot_dir or ""),
            review_cap=cfg.review_cap,
        )

    review_subset = rank_review_candidates(
        candidates,
        task=task,
        context=context,
        queries=task.retrieval_queries,
        review_cap=cfg.review_cap,
    )
    selected_ids = {_candidate_id(candidate) for candidate in review_subset}
    unreviewed_tail = [
        dict(candidate)
        for candidate in candidates
        if _candidate_id(candidate) not in selected_ids
    ]
    reviewed_records, review_errors, review_usage = _review_candidates(
        review_subset,
        reviewer,
    )
    errors.extend(review_errors)
    review_attempted = len(review_subset) if reviewer is not None else 0
    review_skipped = len(candidates) - review_attempted
    records_for_ingest = [*reviewed_records, *unreviewed_tail]

    to_ingest, duplicates = _dedupe_against_parent(
        records_for_ingest,
        parent_units,
        source_root=source_root,
        extra_roots=extra_roots,
    )
    if not to_ingest:
        return _no_progress_outcome(
            execution_meta=execution_meta,
            reason="duplicate_only_no_new_units",
            candidate_counts={
                "discovered": len(candidates),
                "reviewed": review_attempted,
                "review_skipped": review_skipped,
                "review_errors": len(review_errors),
                "duplicates_against_parent": len(duplicates),
                "ingested": 0,
                "ingest_errors": 0,
                "published_new_units": 0,
            },
            text_materialization=text_materialization,
            errors=errors,
            warnings=warnings,
            parent_version=parent_version,
            parent_unit_count=len(parent_units),
            parent_snapshot_dir=str(parent_snapshot_dir or ""),
            review_cap=cfg.review_cap,
            review_usage=review_usage,
            dedupe=duplicates,
        )

    staging_root = (
        work_dir / "visual_procurement_staging"
        if work_dir is not None
        else cache_store.root / ".visual-procurement-staging"
    )
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_assets = staging_root / "assets"
    staging_assets.mkdir(parents=True, exist_ok=True)
    if parent_snapshot_dir is not None and cfg.copy_parent_assets:
        _copy_parent_assets(
            parent_snapshot_dir / "assets",
            staging_assets,
        )

    new_units, ingest_report = ingest_visual_candidates(
        to_ingest,
        source_root=source_root,
        extra_roots=extra_roots,
        copy_assets_to=staging_root,
        approve_markers=set(cfg.approve_markers),
        reject_markers=set(cfg.reject_markers),
    )
    for ingest_error in ingest_report.get("errors") or []:
        if isinstance(ingest_error, Mapping):
            errors.append(
                {
                    "candidate_id": _text(
                        ingest_error.get("candidate_id")
                    )
                    or "<unknown>",
                    "reason": "ingest:" + _text(ingest_error.get("reason")),
                }
            )
    warnings.extend(
        str(warning) for warning in ingest_report.get("warnings") or []
    )

    new_unique, unit_dedup = _dedupe_new_units_against_parent(
        new_units,
        parent_units,
    )
    if not new_unique:
        ingest_error_count = len(ingest_report.get("errors") or [])
        if not new_units:
            reason = (
                "ingest_all_candidates_failed"
                if ingest_error_count
                else "ingest_no_units"
            )
        else:
            reason = "duplicate_only_no_new_units"
        return _no_progress_outcome(
            execution_meta=execution_meta,
            reason=reason,
            candidate_counts={
                "discovered": len(candidates),
                "reviewed": review_attempted,
                "review_skipped": review_skipped,
                "review_errors": len(review_errors),
                "duplicates_against_parent": len(duplicates),
                "ingested": len(new_units),
                "ingest_errors": ingest_error_count,
                "published_new_units": 0,
            },
            text_materialization=text_materialization,
            errors=errors,
            warnings=warnings,
            parent_version=parent_version,
            parent_unit_count=len(parent_units),
            parent_snapshot_dir=str(parent_snapshot_dir or ""),
            review_cap=cfg.review_cap,
            review_usage=review_usage,
            dedupe=duplicates,
            ingest=ingest_report,
        )

    version = resolve_next_snapshot_version(
        parent_version,
        configured=cfg.snapshot_version,
    )
    combined_units = [*parent_units, *new_unique]
    publish_report = cache_store.publish_snapshot(
        version=version,
        units=combined_units,
        assets_dir=staging_root,
    )
    snapshot_dir = cache_store.snapshot_path(version)
    sources = [_unit_summary(unit) for unit in new_unique]
    asset_kind_counts: dict[str, int] = {}
    publication_eligible_count = 0
    for unit in new_unique:
        kind = _text(
            _mapping(unit.get("figure_identity")).get("asset_kind")
        ) or _text(_mapping(unit.get("asset_typing")).get("asset_kind"))
        kind = kind or "figure"
        asset_kind_counts[kind] = asset_kind_counts.get(kind, 0) + 1
        if _mapping(unit.get("permission_state")).get(
            "publication_eligible"
        ):
            publication_eligible_count += 1
    metadata: dict[str, Any] = {
        "schema_version": VISUAL_PROCUREMENT_SCHEMA_VERSION,
        "execution_meta": dict(execution_meta),
        "reason": "committed",
        "adequate": True,
        "candidate_counts": {
            "discovered": len(candidates),
            "reviewed": review_attempted,
            "review_skipped": review_skipped,
            "review_errors": len(review_errors),
            "duplicates_against_parent": len(duplicates),
            "ingested": len(new_units),
            "ingest_errors": len(ingest_report.get("errors") or []),
            "published_new_units": len(new_unique),
        },
        "review": {
            "cap": cfg.review_cap,
            "errors": [dict(error) for error in review_errors],
            "usage": [dict(row) for row in review_usage],
            "skipped": review_skipped,
        },
        "ingest": {
            "candidates_seen": int(
                ingest_report.get("candidates_seen") or 0
            ),
            "units_created": int(ingest_report.get("units_created") or 0),
            "duplicates_skipped": int(
                ingest_report.get("duplicates_skipped") or 0
            ),
            "errors": [
                dict(row)
                for row in (ingest_report.get("errors") or [])
                if isinstance(row, Mapping)
            ],
            "warnings": list(ingest_report.get("warnings") or []),
            "status": _text(ingest_report.get("status")),
        },
        "parent": {
            "version": parent_version,
            "snapshot_dir": str(parent_snapshot_dir or ""),
            "unit_count": len(parent_units),
        },
        "snapshot": {
            "version": version,
            "path": str(snapshot_dir),
            "unit_count": len(combined_units),
            "new_unit_count": len(new_unique),
            "publish_report": dict(publish_report),
        },
        "asset_kind_counts": asset_kind_counts,
        "publication_eligible_count": publication_eligible_count,
        "version_selection": {
            "mode": (
                "caller_provided"
                if cfg.snapshot_version
                else "deterministic_next"
            ),
            "configured": cfg.snapshot_version,
            "parent_version": parent_version,
        },
        "dedupe": {
            "duplicates_against_parent": [
                dict(row) for row in duplicates
            ],
            "new_unit_duplicates_against_parent": list(unit_dedup),
        },
        "text_materialization": dict(text_materialization),
        "usage": {
            "text_materialization": {
                "qwen_usage": [
                    dict(row)
                    for row in (
                        text_materialization.get("qwen_usage") or []
                    )
                    if isinstance(row, Mapping)
                ],
                "embedding_usage": dict(
                    text_materialization.get("embedding_usage") or {}
                ),
            },
            "visual_review": [dict(row) for row in review_usage],
        },
        "errors": [dict(error) for error in errors],
        "warnings": list(dict.fromkeys(warnings)),
    }
    return MaterializationOutcome(
        sources=sources,
        adequate=True,
        total_references=len(new_unique),
        background_only_references=0,
        materialized_route=MATERIALIZED_ROUTE,
        metadata=metadata,
    )


def _review_candidates(
    candidates: Sequence[Mapping[str, Any]],
    reviewer: Callable[
        [Sequence[Mapping[str, Any]]], VisualReviewBatch
    ]
    | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the injected batch reviewer with per-candidate isolation."""

    bounded = [dict(candidate) for candidate in candidates]
    if reviewer is None or not bounded:
        return bounded, [], []
    try:
        batch = reviewer(bounded)
    except Exception as exc:
        errors = [
            {
                "candidate_id": _candidate_id(candidate),
                "reason": (
                    f"review_batch_failed:{type(exc).__name__}:{exc}"
                ),
            }
            for candidate in bounded
        ]
        return bounded, errors, []
    if not isinstance(batch, VisualReviewBatch):
        raise VisualProcurementContractError(
            "reviewer must return VisualReviewBatch"
        )
    records = [dict(record) for record in batch.records]
    if len(records) != len(bounded):
        raise VisualProcurementContractError(
            f"reviewer returned {len(records)} records for "
            f"{len(bounded)} candidates"
        )
    return (
        records,
        [dict(error) for error in batch.errors],
        [dict(row) for row in batch.usage],
    )


def make_visual_materialize_callback(
    *,
    cache_store: VisualCacheStore | None = None,
    cache_root: str | Path | None = None,
    reviewer: Callable[
        [Sequence[Mapping[str, Any]]], VisualReviewBatch
    ]
    | None = None,
    literature_materialize: Callable[..., Any] | None = None,
    config: VisualProcurementConfig | None = None,
) -> Callable[..., MaterializationOutcome]:
    """Build a ``visual_materialize`` callback for pipeline injection."""

    cfg = config or VisualProcurementConfig()
    config_errors = cfg.validate()
    if config_errors:
        raise VisualProcurementContractError(
            "invalid visual procurement config: "
            + "; ".join(config_errors)
        )
    if cache_store is None:
        if cache_root is None:
            raise VisualProcurementContractError(
                "cache_store or cache_root is required"
            )
        cache_store = VisualCacheStore(cache_root)

    def materialize(
        task: SupplementaryRetrievalTask,
        retrieval: RetrievalOutcome,
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> MaterializationOutcome:
        _guard_visual_route(task, execution_meta, route=ROUTE_VISUAL)
        if retrieval.route and retrieval.route != ROUTE_VISUAL:
            raise VisualProcurementContractError(
                "visual materialization requires a visual-route "
                "retrieval outcome"
            )
        return run_visual_procurement(
            task=task,
            retrieval=retrieval,
            context=context,
            execution_meta=execution_meta,
            cache_store=cache_store,
            reviewer=reviewer,
            literature_materialize=literature_materialize,
            config=cfg,
        )

    return materialize


def run_visual_procurement_to_planning(
    *,
    pipeline: Any,
    task: SupplementaryRetrievalTask,
    context: Mapping[str, Any],
    execution_meta: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
    query_records: Sequence[Mapping[str, Any]] = (),
    planner_config: Any | None = None,
    output_dir: str | Path | None = None,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """One additive run: retrieve -> review -> snapshot -> plan artifacts.

    The already-configured ``pipeline`` owns the visual callbacks (including
    the bounded reviewer and cache root); this workflow only invokes them and
    reuses the existing article visual planner to write the normal plan
    artifacts.  When procurement yields no new unit, planning fails open from
    the latest available snapshot (or from an empty cache when none exists).
    """

    retrieval = pipeline.visual_retrieve(
        task,
        list(query_records),
        context,
        execution_meta,
    )
    if not isinstance(retrieval, RetrievalOutcome):
        raise VisualProcurementContractError(
            "visual retrieve callback must return RetrievalOutcome"
        )
    materialization = pipeline.visual_materialize(
        task,
        retrieval,
        context,
        execution_meta,
    )
    if not isinstance(materialization, MaterializationOutcome):
        raise VisualProcurementContractError(
            "visual materialize callback must return MaterializationOutcome"
        )

    meta = dict(materialization.metadata or {})
    snapshot_info = dict(meta.get("snapshot") or {})
    snapshot_version = _text(snapshot_info.get("version"))
    snapshot_path = _text(snapshot_info.get("path"))
    source = "published"
    if not snapshot_path and cache_root is not None:
        store = VisualCacheStore(cache_root)
        latest = store.latest_version()
        if latest:
            snapshot_version = latest
            snapshot_path = str(store.snapshot_path(latest))
            source = "latest"
        else:
            source = "none"
    visual_cache_paths = [Path(snapshot_path)] if snapshot_path else []

    plan = run_article_visual_asset_planner(
        sections=list(sections),
        visual_cache_paths=visual_cache_paths,
        output_dir=Path(output_dir) if output_dir is not None else None,
        config=planner_config,
    )
    planning_validation = plan.get("validation") or {}
    warnings = list(meta.get("warnings") or [])
    warnings.extend(planning_validation.get("warnings") or [])
    return {
        "schema_version": VISUAL_PROCUREMENT_TO_PLANNING_SCHEMA_VERSION,
        "stages": {
            "retrieval": {
                "adequate": bool(retrieval.adequate),
                "route": retrieval.route,
                "query_run_count": len(retrieval.query_runs),
            },
            "materialization": {
                "adequate": bool(materialization.adequate),
                "reason": _text(meta.get("reason")),
                "candidate_counts": dict(
                    meta.get("candidate_counts") or {}
                ),
                "snapshot_version": snapshot_version,
                "usage": dict(meta.get("usage") or {}),
            },
            "snapshot_resolution": {
                "source": source,
                "version": snapshot_version,
                "path": snapshot_path,
            },
            "planning": {
                "validation_status": _text(
                    planning_validation.get("status")
                ),
                "placement_count": len(plan.get("placements") or []),
                "request_count": len(
                    plan.get("conceptual_figure_requests") or []
                ),
                "unfilled_need_count": len(
                    plan.get("unfilled_visual_needs") or []
                ),
            },
        },
        "plan_output_dir": (
            str(output_dir) if output_dir is not None else ""
        ),
        "errors": [dict(row) for row in (meta.get("errors") or [])],
        "warnings": list(dict.fromkeys(warnings)),
        "ok": bool(
            _text(planning_validation.get("status")) != "failed"
        ),
    }


__all__ = [
    "DEFAULT_REVIEW_CAP",
    "MATERIALIZED_ROUTE",
    "REVIEW_SOURCE_DIVERSITY",
    "VISUAL_PROCUREMENT_CONFIG_SCHEMA_VERSION",
    "VISUAL_PROCUREMENT_SCHEMA_VERSION",
    "VISUAL_PROCUREMENT_TO_PLANNING_SCHEMA_VERSION",
    "VisualProcurementConfig",
    "VisualProcurementContractError",
    "VisualReviewBatch",
    "make_visual_materialize_callback",
    "make_visual_retrieve_callback",
    "rank_review_candidates",
    "resolve_next_snapshot_version",
    "review_with_visual_argument_classifier",
    "run_visual_procurement",
    "run_visual_procurement_to_planning",
]
