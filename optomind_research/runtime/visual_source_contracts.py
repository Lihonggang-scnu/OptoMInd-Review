"""Stable source maps and figure contracts for scientific visuals.

The helpers in this module are deliberately deterministic.  Models may fill
high-information semantic fields upstream, while stable identifiers,
provenance links, caption separation, and evidence ceilings are assembled
locally.  Missing optional detail is recorded rather than treated as a reason
to block otherwise useful prose or visuals.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence


VISUAL_SOURCE_MAP_SCHEMA_VERSION = "optomind.visual_source_map.v1"
FIGURE_CONTRACT_SCHEMA_VERSION = "optomind.figure_contract.v1"
FIGURE_ASSET_KINDS = frozenset(
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

_NODE_PREFIXES = {
    "figure": "F",
    "table": "T",
    "caption": "C",
    "text_chunk": "S",
    "body_callout": "M",
    "generated_spec": "G",
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_strings(value: Any, limit: int = 128) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    rows: list[str] = []
    for item in value:
        item_text = _text(item)
        if item_text and item_text not in rows:
            rows.append(item_text)
        if len(rows) >= limit:
            break
    return rows


def _stable_digest(value: Any, length: int = 18) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:length]


def stable_source_node_id(
    node_type: str,
    *,
    paper_id: str,
    external_id: str = "",
    page: Any = None,
    label: str = "",
    bbox: Any = None,
    text: str = "",
) -> str:
    """Return a stable, compact source-node identifier."""

    node_type = _text(node_type).lower() or "source"
    prefix = _NODE_PREFIXES.get(node_type, "X")
    digest = _stable_digest(
        {
            "type": node_type,
            "paper_id": _text(paper_id),
            "external_id": _text(external_id),
            "page": page,
            "label": _text(label),
            "bbox": bbox or [],
            "text": _text(text),
        }
    )
    return f"{prefix}:{digest}"


def _link(
    source_node_id: str,
    target_node_id: str,
    relation: str,
    inverse_relation: str,
) -> dict[str, Any]:
    payload = {
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "relation": relation,
        "inverse_relation": inverse_relation,
    }
    return {
        "link_id": "L:" + _stable_digest(payload),
        **payload,
        "bidirectional_lookup": True,
    }


def build_visual_source_map(
    *,
    unit_id: str,
    source_identity: Mapping[str, Any],
    figure_identity: Mapping[str, Any],
    caption: Mapping[str, Any],
    semantic: Mapping[str, Any],
    provenance: Mapping[str, Any],
    paths: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable graph connecting a visual to caption and source text."""

    source = _mapping(source_identity)
    figure = _mapping(figure_identity)
    caption_data = _mapping(caption)
    semantic_data = _mapping(semantic)
    provenance_data = _mapping(provenance)
    path_data = _mapping(paths)
    paper_id = _text(source.get("paper_id") or source.get("doi"))
    figure_label = _text(
        figure.get("figure_label") or figure.get("asset_id")
    )
    subfigure_label = _text(figure.get("subfigure_label"))
    page = (
        figure.get("page")
        if figure.get("page") is not None
        else provenance_data.get("page")
    )
    bbox = figure.get("bbox_original_px") or figure.get("bbox_px") or []
    figure_external_id = _text(figure.get("asset_id") or unit_id)
    asset_kind = _text(figure.get("asset_kind"))
    if asset_kind not in FIGURE_ASSET_KINDS:
        asset_kind = (
            "table"
            if any(
                token in figure_label.casefold().split()
                for token in ("table", "tbl")
            )
            else "figure"
        )
    elif asset_kind != "table" and any(
        token in figure_label.casefold().split()
        for token in ("table", "tbl")
    ):
        asset_kind = "table"
    figure_node_type = "table" if asset_kind == "table" else "figure"
    figure_node_id = stable_source_node_id(
        figure_node_type,
        paper_id=paper_id,
        external_id=figure_external_id,
        page=page,
        label=" ".join(
            value for value in (figure_label, subfigure_label) if value
        ),
        bbox=bbox,
    )
    nodes: list[dict[str, Any]] = [
        {
            "node_id": figure_node_id,
            "node_type": figure_node_type,
            "asset_kind": asset_kind,
            "external_id": figure_external_id,
            "paper_id": paper_id,
            "page": page,
            "label": figure_label,
            "subfigure_label": subfigure_label,
            "bbox": bbox,
            "content": "",
            "confidence": "high" if figure_external_id else "medium",
            "resource_ref": _mapping(path_data.get("image_ref")),
        }
    ]
    links: list[dict[str, Any]] = []

    caption_text = _text(
        caption_data.get("original")
        or caption_data.get("clean")
        or caption_data.get("subfigure_focus")
    )
    caption_node_ids: list[str] = []
    if caption_text:
        caption_node_id = stable_source_node_id(
            "caption",
            paper_id=paper_id,
            external_id=figure_external_id,
            page=page,
            label=figure_label,
            text=caption_text,
        )
        caption_node_ids.append(caption_node_id)
        nodes.append(
            {
                "node_id": caption_node_id,
                "node_type": "caption",
                "external_id": "",
                "paper_id": paper_id,
                "page": page,
                "label": figure_label,
                "subfigure_label": subfigure_label,
                "bbox": provenance_data.get("caption_bbox") or [],
                "content": caption_text,
                "clean_content": _text(caption_data.get("clean")),
                "confidence": _text(caption_data.get("confidence")) or "medium",
                "resource_ref": {},
            }
        )
        links.append(
            _link(
                caption_node_id,
                figure_node_id,
                "caption_of",
                "has_caption",
            )
        )

    text_node_ids: list[str] = []
    for chunk_id in _list_strings(
        semantic_data.get("linked_text_chunk_ids")
    ):
        node_id = stable_source_node_id(
            "text_chunk",
            paper_id=paper_id,
            external_id=chunk_id,
            page=page,
        )
        text_node_ids.append(node_id)
        nodes.append(
            {
                "node_id": node_id,
                "node_type": "text_chunk",
                "external_id": chunk_id,
                "paper_id": paper_id,
                "page": None,
                "label": "",
                "subfigure_label": "",
                "bbox": [],
                "content": "",
                "confidence": "high",
                "resource_ref": {},
            }
        )
        links.append(
            _link(
                figure_node_id,
                node_id,
                "explained_by_text",
                "explains_figure",
            )
        )

    body_callout_node_ids: list[str] = []
    for index, callout in enumerate(
        _list_strings(semantic_data.get("body_callout_texts")),
        1,
    ):
        node_id = stable_source_node_id(
            "body_callout",
            paper_id=paper_id,
            external_id=f"{figure_external_id}:mention:{index}",
            page=page,
            label=figure_label,
            text=callout,
        )
        body_callout_node_ids.append(node_id)
        nodes.append(
            {
                "node_id": node_id,
                "node_type": "body_callout",
                "external_id": "",
                "paper_id": paper_id,
                "page": None,
                "label": figure_label,
                "subfigure_label": subfigure_label,
                "bbox": [],
                "content": callout,
                "confidence": "medium",
                "resource_ref": {},
            }
        )
        links.append(
            _link(
                node_id,
                figure_node_id,
                "mentions",
                "mentioned_by",
            )
        )

    return {
        "schema_version": VISUAL_SOURCE_MAP_SCHEMA_VERSION,
        "unit_id": _text(unit_id),
        "paper_id": paper_id,
        "asset_kind": asset_kind,
        "root_visual_node_id": figure_node_id,
        "caption_node_ids": caption_node_ids,
        "linked_text_node_ids": text_node_ids,
        "body_callout_node_ids": body_callout_node_ids,
        "nodes": nodes,
        "links": links,
        "lookup_policy": {
            "stable_ids": True,
            "bidirectional": True,
            "caption_separate_from_pixels": True,
            "missing_optional_nodes_fail_open": True,
        },
    }


def validate_visual_source_map(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["source_map_not_object"]
    if _text(value.get("schema_version")) != VISUAL_SOURCE_MAP_SCHEMA_VERSION:
        errors.append("source_map_schema_version_mismatch")
    nodes = value.get("nodes")
    links = value.get("links")
    if not isinstance(nodes, list) or not nodes:
        errors.append("source_map_nodes_missing")
        nodes = []
    if not isinstance(links, list):
        errors.append("source_map_links_not_list")
        links = []
    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            errors.append(f"source_map_node[{index}]_not_object")
            continue
        node_id = _text(node.get("node_id"))
        if not node_id:
            errors.append(f"source_map_node[{index}]_id_missing")
        elif node_id in node_ids:
            errors.append(f"source_map_duplicate_node:{node_id}")
        node_ids.add(node_id)
    root_id = _text(value.get("root_visual_node_id"))
    if root_id not in node_ids:
        errors.append("source_map_root_missing")
    for index, link in enumerate(links):
        if not isinstance(link, Mapping):
            errors.append(f"source_map_link[{index}]_not_object")
            continue
        source_id = _text(link.get("source_node_id"))
        target_id = _text(link.get("target_node_id"))
        if source_id not in node_ids or target_id not in node_ids:
            errors.append(f"source_map_link[{index}]_dangling")
        if not _text(link.get("relation")):
            errors.append(f"source_map_link[{index}]_relation_missing")
    return errors


def _claim_layers(claim_bindings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    primary: list[str] = []
    supporting: list[str] = []
    for binding in claim_bindings:
        claim_id = _text(binding.get("claim_id"))
        if not claim_id:
            continue
        if _text(binding.get("binding_type")) == "direct":
            primary.append(claim_id)
        else:
            supporting.append(claim_id)
    return {
        "primary_claim_ids": list(dict.fromkeys(primary)),
        "supporting_claim_ids": list(dict.fromkeys(supporting)),
        "control_claim_ids": [],
        "boundary_claim_ids": [],
    }


def build_figure_contract(
    *,
    contract_id: str,
    section_id: str,
    figure_kind: str,
    asset_kind: str = "",
    argumentative_purpose: str,
    claim_bindings: Sequence[Mapping[str, Any]] = (),
    panel_manifest: Sequence[Mapping[str, Any]] = (),
    source_map: Mapping[str, Any] | None = None,
    source_caption: str = "",
    editorial_caption: str = "",
    attribution: Mapping[str, Any] | None = None,
    permission: Mapping[str, Any] | None = None,
    evidence_ceiling: str = "",
    data_provenance_level: str = "source_figure",
    transformation: Mapping[str, Any] | None = None,
    review_state: str = "",
    required_disclosure: str = "",
) -> dict[str, Any]:
    """Build a local fill-the-form contract for one selected figure."""

    source_map_data = _mapping(source_map)
    attribution_data = _mapping(attribution)
    permission_data = _mapping(permission)
    normalized_asset_kind = _text(asset_kind)
    if normalized_asset_kind not in FIGURE_ASSET_KINDS:
        normalized_asset_kind = (
            "table"
            if "table" in _text(figure_kind).casefold()
            else "figure"
        )
    panels: list[dict[str, Any]] = []
    for index, panel in enumerate(panel_manifest, 1):
        panel_data = _mapping(panel)
        panels.append(
            {
                "panel_id": _text(panel_data.get("panel_id")) or str(index),
                "question": "",
                "data_or_source": _text(
                    panel_data.get("visual_chunk_id")
                    or panel_data.get("source_local_path")
                    or panel_data.get("local_image_path")
                ),
                "visual_form": _text(figure_kind),
                "statistics": "",
                "reader_takeaway": _text(argumentative_purpose),
                "paper_id": _text(panel_data.get("paper_id")),
                "doi": _text(panel_data.get("doi")),
                "subfigure_label": _text(panel_data.get("subfigure_label")),
            }
        )
    if not panels:
        panels.append(
            {
                "panel_id": "a",
                "question": "",
                "data_or_source": "",
                "visual_form": _text(figure_kind),
                "statistics": "",
                "reader_takeaway": _text(argumentative_purpose),
                "paper_id": _text(attribution_data.get("paper_id")),
                "doi": _text(attribution_data.get("doi")),
                "subfigure_label": _text(
                    attribution_data.get("subfigure_label")
                ),
            }
        )
    return {
        "schema_version": FIGURE_CONTRACT_SCHEMA_VERSION,
        "contract_id": _text(contract_id),
        "section_id": _text(section_id),
        "figure_kind": _text(figure_kind),
        "asset_kind": normalized_asset_kind,
        "is_table": normalized_asset_kind == "table",
        "conclusion": _text(argumentative_purpose),
        "evidence_layers": _claim_layers(claim_bindings),
        "evidence_ceiling": _text(evidence_ceiling)
        or _text(permission_data.get("use_permission"))
        or ("explanatory_only" if required_disclosure else "source_figure"),
        "panel_map": panels,
        "caption_contract": {
            "source_caption": _text(source_caption),
            "editorial_caption": _text(editorial_caption),
            "attribution": attribution_data,
            "required_disclosure": _text(required_disclosure),
            "caption_separate_from_image": True,
            "source_caption_separate_from_editorial_caption": True,
        },
        "data_contract": {
            "provenance_level": _text(data_provenance_level),
            "input_refs": [],
            "independent_unit": "",
            "variables_and_units": [],
            "transformations": [],
            "missing_value_policy": "",
            "exclusion_policy": "",
        },
        "statistics_contract": {
            "required": _text(data_provenance_level) in {"exact", "approximate"},
            "n_definition": "",
            "summary_and_error": "",
            "test_or_model": "",
            "multiple_comparison_policy": "",
            "effect_size_and_interval": "",
            "exploratory": None,
        },
        "source_map_ref": {
            "unit_id": _text(source_map_data.get("unit_id")),
            "root_visual_node_id": _text(
                source_map_data.get("root_visual_node_id")
            ),
            "caption_node_ids": _list_strings(
                source_map_data.get("caption_node_ids")
            ),
            "linked_text_node_ids": _list_strings(
                source_map_data.get("linked_text_node_ids")
            ),
        },
        "permission": permission_data,
        "transformation": _mapping(transformation),
        "fidelity_lock": {
            "paper_id": _text(attribution_data.get("paper_id")),
            "doi": _text(attribution_data.get("doi")),
            "figure_label": _text(attribution_data.get("figure_label")),
            "subfigure_label": _text(
                attribution_data.get("subfigure_label")
            ),
            "visual_chunk_ids": list(
                dict.fromkeys(
                    _text(panel.get("visual_chunk_id"))
                    for panel in panel_manifest
                    if _text(panel.get("visual_chunk_id"))
                )
            ),
        },
        "review": {
            "state": _text(review_state),
            "fail_open_for_optional_fields": True,
            "hard_blocks": [
                "missing_source_identity",
                "missing_or_changed_image",
                "fabricated_empirical_content",
                "permission_restricted",
            ],
        },
    }


def validate_figure_contract(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["figure_contract_not_object"]
    if _text(value.get("schema_version")) != FIGURE_CONTRACT_SCHEMA_VERSION:
        errors.append("figure_contract_schema_version_mismatch")
    for field in ("contract_id", "section_id", "figure_kind", "conclusion"):
        if not _text(value.get(field)):
            errors.append(f"figure_contract_{field}_missing")
    asset_kind = _text(value.get("asset_kind"))
    if asset_kind and asset_kind not in FIGURE_ASSET_KINDS:
        errors.append(f"figure_contract_invalid_asset_kind:{asset_kind}")
    if value.get("is_table") is True and asset_kind != "table":
        errors.append("figure_contract_table_flag_mismatch")
    if value.get("is_table") is False and asset_kind == "table":
        errors.append("figure_contract_table_flag_mismatch")
    caption_contract = _mapping(value.get("caption_contract"))
    if caption_contract.get("caption_separate_from_image") is not True:
        errors.append("figure_contract_caption_not_separate")
    panel_map = value.get("panel_map")
    if not isinstance(panel_map, list) or not panel_map:
        errors.append("figure_contract_panel_map_missing")
    return errors


__all__ = [
    "FIGURE_ASSET_KINDS",
    "FIGURE_CONTRACT_SCHEMA_VERSION",
    "VISUAL_SOURCE_MAP_SCHEMA_VERSION",
    "build_figure_contract",
    "build_visual_source_map",
    "stable_source_node_id",
    "validate_figure_contract",
    "validate_visual_source_map",
]
