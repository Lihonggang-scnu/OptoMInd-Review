"""Schema and validation helpers for the long-term visual cache.

This module is intentionally pure: it defines the durable visual unit
contract, portable path references, approval-state derivation, and the
fail-closed unit validator used before a snapshot is published.  It performs
no file I/O, no network calls, and no model calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .visual_source_contracts import (
    validate_visual_source_map,
)


CACHE_SCHEMA_VERSION = "optomind.visual_cache.v1"
STORE_SCHEMA_VERSION = "optomind.visual_cache_store.v1"
UNIT_SCHEMA_VERSION = "optomind.visual_unit.v1"
MANIFEST_SCHEMA_VERSION = "optomind.visual_cache_manifest.v1"
LATEST_SCHEMA_VERSION = "optomind.visual_cache_latest.v1"

UNITS_FILENAME = "units.json"
SQLITE_FILENAME = "visual_cache.sqlite"
MANIFEST_FILENAME = "manifest.json"
LATEST_FILENAME = "latest.json"
ASSETS_DIRNAME = "assets"

UNIT_KINDS = frozenset(
    {"single_figure", "subfigure", "parent_figure", "generated_visual"}
)
UNIT_ROLES = frozenset({"review_asset", "parent_context"})
APPROVAL_STATES = frozenset({"pending", "approved", "rejected"})
CROP_HYGIENE_STATUSES = frozenset(
    {"clean", "derived_clean", "needs_review", "rejected"}
)
ASSET_KINDS = frozenset(
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
GENERATED_VISUAL_KIND = "generated_visual"
GENERATED_SOURCE_KIND = "ai_generated_explanatory_visual"
GENERATED_DISCLOSURE = (
    "AI-generated explanatory visual; not empirical evidence."
)

APPROVED_MARKERS_DEFAULT = frozenset({"human_approved", "approved"})
REJECTED_MARKERS_DEFAULT = frozenset({"human_rejected", "rejected"})

PERMISSION_LEVELS = frozenset(
    {
        "discovery_only",
        "contextual_or_qualified_support",
        "factual_support",
    }
)
DEFAULT_PERMISSION = "discovery_only"

VALID_VISUAL_ARGUMENT_TYPES = frozenset(
    {
        "mechanism_anchor",
        "taxonomy_or_roadmap",
        "method_or_workflow",
        "quantitative_comparison",
        "trend_or_parameter_map",
        "representative_example",
        "anomaly_or_limitation",
        "synthesis_overview",
    }
)

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VisualCacheSchemaError(ValueError):
    """Raised when a unit violates the durable visual cache contract."""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _list_strings(value: Any, limit: int = 64) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def canonical_json_hash(value: Any) -> str:
    """Return a stable ``sha256:<hex>`` over canonical JSON bytes."""

    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_version(version: str) -> str:
    version = _text(version)
    if not _VERSION_RE.match(version):
        raise VisualCacheSchemaError(
            f"invalid cache version {version!r}: expected [A-Za-z0-9._-]+"
        )
    return version


def portable_path_ref(
    path: Path | str,
    *,
    root: Path | None = None,
    root_key: str = "source",
) -> dict[str, Any]:
    """Rebase a path into a portable root+relative reference.

    If the path is not under the supplied root, it is recorded as
    ``unbound`` with only a basename.  The cache never stores old-machine
    absolute paths as the authoritative locator; snapshot asset copies are.
    """

    path = Path(str(path))
    if root is not None:
        resolved_root = Path(root).resolve()
        if not path.is_absolute():
            rooted = resolved_root / path
            if rooted.exists():
                path = rooted
        try:
            relative = path.resolve().relative_to(resolved_root)
            return {
                "root": root_key,
                "relative": relative.as_posix(),
            }
        except ValueError:
            pass
    path = path.resolve()
    return {
        "root": "unbound",
        "relative": path.name,
        "note": "path_not_under_provided_root",
    }


def resolve_path_ref(
    ref: Any,
    roots: Mapping[str, Path] | None = None,
) -> Path | None:
    """Resolve a portable reference against caller-supplied roots."""

    roots = dict(roots or {})
    if isinstance(ref, str):
        for root in roots.values():
            candidate = Path(root) / Path(str(ref))
            if candidate.exists():
                return candidate
        return None
    if not isinstance(ref, Mapping):
        return None
    root_key = _text(ref.get("root"))
    relative = _text(ref.get("relative"))
    if not root_key or not relative:
        return None
    if root_key == "snapshot":
        return None  # snapshot refs are resolved by the store, not roots
    root = roots.get(root_key)
    if root is None:
        return None
    return Path(root) / Path(relative)


def derive_approval_state(
    record: Mapping[str, Any],
    *,
    approve_markers: set[str] | None = None,
    reject_markers: set[str] | None = None,
) -> dict[str, Any]:
    """Derive a durable approval state without inventing human review.

    Only explicit approval markers (default: ``human_approved``) promote a
    unit to ``approved``.  Test-mode and timeout acceptances stay ``pending``
    so they can never masquerade as durable approvals.
    """

    approve = set(approve_markers or APPROVED_MARKERS_DEFAULT)
    reject = set(reject_markers or REJECTED_MARKERS_DEFAULT)
    review_decision = _text(record.get("review_decision"))
    status = _text(record.get("status"))
    human_review_status = _text(record.get("human_review_status"))
    visual_argument_status = _text(record.get("visual_argument_status"))
    nested = _mapping(record.get("approval"))
    nested_state = _text(nested.get("state"))
    nested_source = _text(nested.get("source_marker"))

    markers = [
        review_decision,
        status,
        human_review_status,
        visual_argument_status,
        nested_state,
        nested_source,
    ]
    normalized = {
        marker
        for marker in markers
        if marker
    }
    if normalized & reject:
        source_marker = next(
            (marker for marker in markers if marker in reject),
            "",
        )
        return {
            "state": "rejected",
            "source_marker": source_marker,
            "approved_at": "",
            "approver": "",
            "note": "explicit rejection preserved",
        }
    approved_marker = next(
        (marker for marker in markers if marker in approve),
        "",
    )
    if approved_marker:
        return {
            "state": "approved",
            "source_marker": approved_marker,
            "approved_at": _text(nested.get("approved_at")),
            "approver": _text(nested.get("approver")),
            "note": "explicit approval marker",
        }
    return {
        "state": "pending",
        "source_marker": "",
        "approved_at": "",
        "approver": "",
        "note": (
            "no explicit human approval; test-mode and timeout acceptances "
            "remain pending"
        ),
    }


def derive_asset_kind(
    record: Mapping[str, Any],
    *,
    advisor: Any = None,
) -> str:
    """Derive a durable ``asset_kind`` from advisor/model/local signals.

    Explicit advisor output wins, then explicit candidate fields, then a
    conservative label/caption heuristic.  Tables are never silently folded
    into figures: a ``Table``/``Tbl`` label or table asset type resolves to
    ``table``.  Anything without usable evidence stays ``figure`` for old
    records, while an advisor-reported unknown is preserved as ``unknown``.
    """

    advisor_data = advisor
    if advisor_data is None:
        for key in ("qwen_crop_advisor", "crop_advisor", "advisor"):
            candidate_advisor = record.get(key)
            if isinstance(candidate_advisor, Mapping) and candidate_advisor:
                advisor_data = candidate_advisor
                break
    advisor_kind = (
        _text(advisor_data.get("asset_kind"))
        if isinstance(advisor_data, Mapping)
        else ""
    )
    if advisor_kind in ASSET_KINDS:
        figure = _mapping(record.get("figure_identity"))
        label_text = _text(
            figure.get("figure_label") or record.get("figure_label")
        ).casefold()
        type_text = _text(
            record.get("asset_type") or record.get("chunk_kind")
        ).casefold()
        if (
            any(token in label_text.split() for token in ("table", "tbl"))
            or "table" in type_text
        ):
            # A local Table label/type is ground truth: a model
            # misclassification must never turn Table 1 into source_figure.
            return "table"
        return advisor_kind

    figure = _mapping(record.get("figure_identity"))
    typing = _mapping(record.get("asset_typing"))
    source_map = _mapping(record.get("source_map"))
    explicit_candidates = (
        record.get("asset_kind"),
        figure.get("asset_kind"),
        typing.get("asset_kind"),
        source_map.get("asset_kind"),
        record.get("asset_type"),
        record.get("chunk_kind"),
    )
    for candidate in explicit_candidates:
        kind = _text(candidate).lower()
        if kind in ASSET_KINDS:
            return kind

    label = _text(figure.get("figure_label") or record.get("figure_label"))
    caption = _text(
        record.get("caption_original")
        or record.get("caption_clean")
        or record.get("caption")
        or record.get("caption_text")
    )
    asset_type = _text(record.get("asset_type") or record.get("chunk_kind"))
    searchable = " ".join((label, caption, asset_type)).lower()
    if re.search(r"\btable\b|\btbl\b", searchable) or "table" in asset_type:
        return "table"
    if any(
        marker in searchable
        for marker in ("equation", "formula")
    ):
        return "equation"
    if any(
        marker in searchable
        for marker in ("diagram", "schematic", "scheme")
    ):
        return "diagram"
    if any(
        marker in searchable
        for marker in ("photo", "photograph", "micrograph")
    ):
        return "photo"
    return "figure"


def derive_publication_eligibility(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Derive an explicit publication-eligibility policy for a durable unit.

    External discovery-only material and anything pending or under review is
    never publication-eligible.  Only explicit human approval combined with
    a sufficient permission level promotes a unit.  Generated article-owned
    visuals are eligible once approved and free of review flags.
    """

    permission = _mapping(unit.get("permission_state"))
    approval = _mapping(unit.get("approval"))
    review = _mapping(unit.get("review"))
    crop_hygiene = _mapping(unit.get("crop_hygiene"))
    source = _mapping(unit.get("source_identity"))
    use_permission = _text(permission.get("use_permission"))
    approval_state = _text(approval.get("state"))
    needs_human_review = bool(review.get("needs_human_review"))
    review_flags = {
        _text(item)
        for item in (
            review.get("review_flags")
            if isinstance(review.get("review_flags"), (list, tuple))
            else []
        )
    }
    crop_status = _text(crop_hygiene.get("status"))
    is_generated = (
        _text(source.get("source_kind")) == GENERATED_SOURCE_KIND
        or _text(unit.get("unit_kind")) == GENERATED_VISUAL_KIND
    )

    if use_permission == "discovery_only" and not is_generated:
        return {
            "publication_eligible": False,
            "reason": "external_or_discovery_only_not_publication_eligible",
            "requires_human_review": True,
        }
    if approval_state != "approved":
        return {
            "publication_eligible": False,
            "reason": "approval_state_not_approved",
            "requires_human_review": True,
        }
    if needs_human_review:
        return {
            "publication_eligible": False,
            "reason": "human_review_required",
            "requires_human_review": True,
        }
    if review_flags & {"caption_in_pixels", "page_prose"}:
        return {
            "publication_eligible": False,
            "reason": "image_contamination_review_required",
            "requires_human_review": True,
        }
    if crop_status in {"needs_review", "rejected"}:
        return {
            "publication_eligible": False,
            "reason": "crop_hygiene_review_required",
            "requires_human_review": True,
        }
    if is_generated:
        return {
            "publication_eligible": True,
            "reason": "article_owned_generated_visual_approved",
            "requires_human_review": False,
        }
    if use_permission in {"contextual_or_qualified_support", "factual_support"}:
        return {
            "publication_eligible": True,
            "reason": "approved_with_sufficient_use_permission",
            "requires_human_review": False,
        }
    return {
        "publication_eligible": False,
        "reason": "use_permission_not_sufficient",
        "requires_human_review": True,
    }


def _validate_image_ref(unit: Mapping[str, Any], errors: list[str]) -> None:
    paths = _mapping(unit.get("paths"))
    image_ref = _mapping(paths.get("image_ref"))
    root = _text(image_ref.get("root"))
    relative = _text(image_ref.get("relative"))
    if root != "snapshot":
        errors.append("paths.image_ref.root_must_be_snapshot")
    if not relative:
        errors.append("paths.image_ref.relative_missing")
    if (
        relative
        and not relative.startswith("assets/")
        or relative in {"assets", "assets/"}
    ):
        errors.append("paths.image_ref.must_live_under_assets")
    if not (unit.get("hashes") or {}).get("image_sha256"):
        errors.append("hashes.image_sha256_missing")


def validate_visual_unit(unit: Any) -> list[str]:
    """Return all contract violations; an empty list means publishable."""

    errors: list[str] = []
    if not isinstance(unit, Mapping):
        return ["unit_not_object"]
    required_top = (
        "unit_id",
        "schema_version",
        "unit_kind",
        "unit_role",
        "source_identity",
        "figure_identity",
        "caption",
        "semantic",
        "argumentative_roles",
        "provenance",
        "permission_state",
        "hashes",
        "vector_refs",
        "lineage",
        "use_history",
        "review",
        "approval",
        "crop_hygiene",
        "paths",
        "created_at",
    )
    for key in required_top:
        if key not in unit:
            errors.append(f"missing_field:{key}")
    if not _text(unit.get("unit_id")):
        errors.append("unit_id_missing")
    if _text(unit.get("schema_version")) != UNIT_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    unit_kind = _text(unit.get("unit_kind"))
    if unit_kind not in UNIT_KINDS:
        errors.append(f"invalid_unit_kind:{unit_kind or 'empty'}")
    unit_role = _text(unit.get("unit_role"))
    if unit_role not in UNIT_ROLES:
        errors.append(f"invalid_unit_role:{unit_role or 'empty'}")
    if unit_kind == "parent_figure" and unit_role != "parent_context":
        errors.append("parent_figure_must_be_parent_context")
    if unit_kind != "parent_figure" and unit_role != "review_asset":
        errors.append("review_unit_must_be_review_asset")
    is_generated = unit_kind == GENERATED_VISUAL_KIND

    source = _mapping(unit.get("source_identity"))
    if is_generated:
        if _text(source.get("source_kind")) != GENERATED_SOURCE_KIND:
            errors.append("generated_visual.source_kind_required")
        if _text(source.get("doi")):
            errors.append("generated_visual.must_not_have_source_doi")
        rights = _mapping(source.get("rights"))
        if GENERATED_DISCLOSURE not in _text(rights.get("disclosure")):
            errors.append("generated_visual.rights_disclosure_required")
    elif not _text(source.get("paper_id")) and not _text(source.get("doi")):
        errors.append("source_identity.paper_id_or_doi_required")
    figure = _mapping(unit.get("figure_identity"))
    if not _text(figure.get("asset_id")) and not _text(
        figure.get("figure_label")
    ):
        errors.append("figure_identity.asset_id_or_label_required")
    asset_kind = _text(figure.get("asset_kind"))
    if asset_kind and asset_kind not in ASSET_KINDS:
        errors.append(f"figure_identity.invalid_asset_kind:{asset_kind}")

    asset_typing = _mapping(unit.get("asset_typing"))
    typing_kind = _text(asset_typing.get("asset_kind"))
    if asset_typing:
        if typing_kind not in ASSET_KINDS:
            errors.append(
                f"asset_typing.invalid_asset_kind:{typing_kind or 'empty'}"
            )
        if bool(asset_typing.get("table")) != (typing_kind == "table"):
            errors.append("asset_typing.table_flag_mismatch")

    caption = _mapping(unit.get("caption"))
    if not _text(caption.get("clean")) and not _text(
        caption.get("original")
    ):
        errors.append("caption.empty")
    if is_generated:
        if GENERATED_DISCLOSURE not in _text(caption.get("disclosure")):
            errors.append("generated_visual.caption_disclosure_required")
        if GENERATED_DISCLOSURE not in _text(caption.get("clean")):
            errors.append("generated_visual.caption_must_display_disclosure")

    argument = _mapping(unit.get("argumentative_roles"))
    primary = _text(argument.get("primary"))
    if primary and primary not in VALID_VISUAL_ARGUMENT_TYPES:
        errors.append(f"argumentative_roles.invalid_primary:{primary}")
    if is_generated and primary == "quantitative_comparison":
        errors.append("generated_visual.quantitative_comparison_not_allowed")

    permission = _mapping(unit.get("permission_state"))
    use_permission = _text(permission.get("use_permission"))
    if use_permission not in PERMISSION_LEVELS:
        errors.append(
            f"permission_state.invalid_use_permission:{use_permission or 'empty'}"
        )
    publication_eligible = permission.get("publication_eligible")
    if (
        publication_eligible is True
        and use_permission == "discovery_only"
        and not is_generated
    ):
        errors.append(
            "permission_state.discovery_only_publication_eligible_forbidden"
        )
    approval_for_permission = _mapping(unit.get("approval"))
    if (
        publication_eligible is True
        and _text(approval_for_permission.get("state")) != "approved"
    ):
        errors.append("permission_state.pending_publication_eligible_forbidden")
    if is_generated:
        if _text(permission.get("evidence_ceiling")) != "explanatory_only":
            errors.append(
                "generated_visual.evidence_ceiling_must_be_explanatory_only"
            )
        if permission.get("empirical_evidence_allowed") is not False:
            errors.append("generated_visual.empirical_evidence_not_allowed")
        if permission.get("quantitative_evidence_allowed") is not False:
            errors.append(
                "generated_visual.quantitative_evidence_not_allowed"
            )

    vector_refs = unit.get("vector_refs")
    if not isinstance(vector_refs, Mapping) or not isinstance(
        vector_refs.get("entries"), list
    ):
        errors.append("vector_refs.entries_must_be_list")
    use_history = unit.get("use_history")
    if not isinstance(use_history, Mapping) or not isinstance(
        use_history.get("used_in_run_ids"), list
    ):
        errors.append("use_history.used_in_run_ids_must_be_list")

    lineage = _mapping(unit.get("lineage"))
    if not isinstance(lineage.get("enhancement_history"), list):
        errors.append("lineage.enhancement_history_must_be_list")
    parent_unit_id = _text(lineage.get("parent_unit_id"))
    if unit_kind == "subfigure" and not parent_unit_id:
        if not bool(lineage.get("parent_unavailable")):
            errors.append("subfigure_missing_parent_unit_id")
        elif not _text(figure.get("parent_asset_id")) and not _text(
            figure.get("parent_label")
        ):
            errors.append("subfigure_parent_unavailable_without_parent_identity")
    if unit_kind in {"single_figure", "parent_figure"} and parent_unit_id:
        errors.append("non_subfigure_must_not_have_parent_unit_id")
    if is_generated:
        if _text(lineage.get("generation_status")) != "ai_generated":
            errors.append(
                "generated_visual.generation_status_must_be_ai_generated"
            )
        generation = _mapping(lineage.get("generation"))
        if not (
            _text(generation.get("prompt"))
            or _text(generation.get("prompt_ref"))
        ):
            errors.append("generated_visual.generation_prompt_required")
        if not (
            _text(generation.get("model_version"))
            or _text(generation.get("model_name"))
        ):
            errors.append("generated_visual.generation_model_required")
        if GENERATED_DISCLOSURE not in _text(
            generation.get("disclosure")
        ):
            errors.append("generated_visual.generation_disclosure_required")
        if not _text(generation.get("created_at")):
            errors.append("generated_visual.generation_created_at_required")
        review_history = generation.get("review_history")
        if not isinstance(review_history, list) or not review_history:
            errors.append("generated_visual.review_history_required")
        elif not any(
            isinstance(entry, Mapping)
            and _text(entry.get("decision") or entry.get("status"))
            in {"approved", "human_approved"}
            for entry in review_history
        ):
            errors.append(
                "generated_visual.review_history_approved_entry_required"
            )
        if not isinstance(generation.get("attempt_history"), list):
            errors.append("generated_visual.attempt_history_must_be_list")

    approval = _mapping(unit.get("approval"))
    approval_state = _text(approval.get("state"))
    if approval_state not in APPROVAL_STATES:
        errors.append(f"approval.invalid_state:{approval_state or 'empty'}")
    if approval_state == "approved" and not _text(
        approval.get("source_marker")
    ):
        errors.append("approval.approved_without_source_marker")
    if approval_state == "pending" and _text(approval.get("approved_at")):
        errors.append("approval.pending_with_approved_at")
    approval_markers = set(APPROVED_MARKERS_DEFAULT)
    if (
        approval_state in {"pending", "rejected"}
        and _text(approval.get("source_marker")) in approval_markers
    ):
        errors.append("approval.approval_marker_on_non_approved")
    if is_generated and approval_state != "approved":
        errors.append("generated_visual.must_be_approved")

    crop_hygiene = _mapping(unit.get("crop_hygiene"))
    crop_status = _text(crop_hygiene.get("status"))
    if crop_status not in CROP_HYGIENE_STATUSES:
        errors.append(
            f"crop_hygiene.invalid_status:{crop_status or 'empty'}"
        )
    crop_source_kind = _text(crop_hygiene.get("source_kind"))
    if (
        crop_source_kind == "rendered_region_from_caption"
        and crop_status == "clean"
    ):
        errors.append("crop_hygiene.rendered_region_clean_without_derivation")
    hygiene_derivative = _mapping(crop_hygiene.get("derivative"))
    if crop_status == "derived_clean":
        if not hygiene_derivative:
            errors.append("crop_hygiene.derived_clean_without_derivative")
        else:
            if len(hygiene_derivative.get("crop_bbox_px") or []) != 4:
                errors.append(
                    "crop_hygiene.derivative_crop_bbox_missing"
                )
            if not _text(hygiene_derivative.get("parent_sha256")):
                errors.append(
                    "crop_hygiene.derivative_missing_parent_hash"
                )
            if not _text(hygiene_derivative.get("relpath")):
                errors.append("crop_hygiene.derivative_missing_relpath")
        crop_info = _mapping(lineage.get("crop"))
        if not _text(crop_info.get("parent_image_hash")):
            errors.append("lineage.crop.parent_image_hash_missing")
        if not _mapping(crop_info.get("parent_image_ref")).get("relative"):
            errors.append("lineage.crop.parent_image_ref_missing")
        if not _mapping(crop_info.get("derivative")):
            errors.append("lineage.crop.derivative_missing")
    elif crop_status in {"needs_review", "rejected"} and hygiene_derivative:
        errors.append("crop_hygiene.non_clean_with_derivative")

    paths = _mapping(unit.get("paths"))
    original_ref = _mapping(paths.get("original_image_ref"))
    if original_ref:
        if _text(original_ref.get("root")) != "snapshot":
            errors.append("paths.original_image_ref.root_must_be_snapshot")
        if not _text(original_ref.get("relative")).startswith("assets/"):
            errors.append(
                "paths.original_image_ref.must_live_under_assets"
            )

    _validate_image_ref(unit, errors)
    if "source_map" in unit:
        errors.extend(validate_visual_source_map(unit.get("source_map")))
    return errors
__all__ = [
    "ASSETS_DIRNAME",
    "ASSET_KINDS",
    "APPROVAL_STATES",
    "APPROVED_MARKERS_DEFAULT",
    "CACHE_SCHEMA_VERSION",
    "CROP_HYGIENE_STATUSES",
    "DEFAULT_PERMISSION",
    "GENERATED_DISCLOSURE",
    "GENERATED_SOURCE_KIND",
    "GENERATED_VISUAL_KIND",
    "LATEST_FILENAME",
    "LATEST_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "PERMISSION_LEVELS",
    "REJECTED_MARKERS_DEFAULT",
    "SQLITE_FILENAME",
    "STORE_SCHEMA_VERSION",
    "UNIT_KINDS",
    "UNIT_ROLES",
    "UNIT_SCHEMA_VERSION",
    "UNITS_FILENAME",
    "VALID_VISUAL_ARGUMENT_TYPES",
    "VisualCacheSchemaError",
    "canonical_json_hash",
    "derive_approval_state",
    "derive_asset_kind",
    "derive_publication_eligibility",
    "portable_path_ref",
    "resolve_path_ref",
    "validate_version",
    "validate_visual_unit",
]
