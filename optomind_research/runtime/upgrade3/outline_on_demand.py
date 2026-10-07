"""Small on-demand material access seam for autonomous outline strengthening.

The access pass only selects material locations.  It cannot provide revision
answers, issue lists, or a replacement plan.  A local resolver then attaches
the requested original records (including tool and mediated records) to one
Max owner request.  The owner may ask for one additional read; a final owner
response is still validated by the existing outline-strengthening contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import outline_strengthening as strengthening
from . import progressive_review_plan as planning


ACCESS_STAGE = "outline_material_access"
_MATERIAL_CHANNELS = ("source_materials", "candidate_materials", "tool_materials")
_CATALOG_FORBIDDEN = frozenset({
    "updated_plan", "chapter_updates", "body", "manuscript", "issues",
    "review_targets", "expected_answers", "prior_candidate", "edited_body",
})
_CONTENT_KEYS = frozenset({
    "study_summary_A", "review_planning_B", "deep_read_material",
    "study_summary_A_variants", "review_planning_B_variants",
    "deep_read_materials", "supplement_material", "supplement_materials",
    "supplement_gap_material", "supplement_gap_materials", "local_passages",
    "local_passages_variants",
    "tool_supplement_materials", "usable_content",
})


class OnDemandMaterialError(ValueError):
    """Raised when an access plan cannot be resolved without losing evidence."""


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _unit_ids(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    return (
        [str(value) for value in payload.get("modifiable_unit_ids") or [] if str(value).strip()],
        [str(value) for value in payload.get("read_only_unit_ids") or [] if str(value).strip()],
    )


def _material_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "source_handle", "paper_id", "original_paper_id", "original_source_handle",
        "title", "study_id", "doi", "identity", "source_identity",
    )
    return {key: _copy(row[key]) for key in keys if key in row}


# These fields identify a study across the source, candidate, and tool
# channels.  A single study can legitimately have a full source card plus
# several tool or navigation records with different useful content.  The
# resolver must distinguish those aliases from records whose identity fields
# actually disagree.
_STABLE_IDENTITY_FIELDS = (
    "source_handle", "paper_id", "original_paper_id", "original_source_handle",
    "doi", "study_id",
)


def _identity_value(identity: Mapping[str, Any], key: str) -> str:
    value = identity.get(key)
    if value is None:
        return ""
    return str(value).strip().casefold()


def _identities_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two catalog identities can be aliases of one study.

    Missing fields are allowed because tool and mediated records often carry
    only a subset of the source identity.  Any shared stable field must agree;
    this keeps a same-handle/different-DOI collision an explicit error.
    """

    shared = False
    for key in _STABLE_IDENTITY_FIELDS:
        left_value = _identity_value(left, key)
        right_value = _identity_value(right, key)
        if left_value and right_value:
            shared = True
            if left_value != right_value:
                return False
    return shared


def _identity_group_compatible(identities: Sequence[Mapping[str, Any]]) -> bool:
    """Reject any disagreement among non-empty stable identity fields."""

    for key in _STABLE_IDENTITY_FIELDS:
        values = {
            _identity_value(identity, key)
            for identity in identities
            if _identity_value(identity, key)
        }
        if len(values) > 1:
            return False
    return True


def _canonical_access_id(access_ids: Sequence[str], lookup: Mapping[str, Any]) -> str:
    """Choose a stable representative without dropping any alias record."""

    channel_rank = {
        "source_materials": 0,
        "candidate_materials": 1,
        "candidate_navigation": 2,
        "tool_materials": 3,
    }

    def rank(access_id: str) -> tuple[int, int, int, str]:
        info = lookup.get(access_id) if isinstance(lookup, Mapping) else None
        entry = info.get("entry") if isinstance(info, Mapping) else {}
        record = info.get("record") if isinstance(info, Mapping) else {}
        content_fields = sum(1 for key in _CONTENT_KEYS if isinstance(record, Mapping) and key in record)
        record_size = len(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
        channel = channel_rank.get(str(entry.get("channel") or ""), 9)
        return (-content_fields, -record_size, channel, str(access_id))

    return min((str(value) for value in access_ids), key=rank)


def _paths(row: Mapping[str, Any]) -> list[str]:
    result = []
    for key, value in row.items():
        if key in _CONTENT_KEYS or "condition" in str(key).casefold() or "limit" in str(key).casefold():
            result.append(str(key))
        if isinstance(value, Mapping):
            for child in value:
                if key in _CONTENT_KEYS:
                    result.append(f"{key}.{child}")
    return list(dict.fromkeys(result))


# Per material root, eight times the former 600-character JSON prefix.  This
# targets bounded displayed content, with one atomic overflow if the view
# would otherwise be empty. Full records remain available to the resolver.
_LOCATOR_CONTENT_BUDGET = 4800
_LOCATOR_PRIORITY = (
    "problem_or_question", "research_question", "question", "research_scope", "scope",
    "key_findings", "key_results", "finding", "findings", "claim", "results",
    "planning_summary", "contribution_and_limits", "conditions", "boundary_conditions",
    "limits", "limitations", "scope_interpretation_cautions", "paper_kind",
    "work_summary", "facet_contributions", "broader_review_uses", "topic_handles",
    "usable_content", "approach",
)
_LOCATOR_CLAIMS = frozenset({
    "key_findings", "key_results", "finding", "findings", "claim", "result", "results",
    "contribution",
})
_LOCATOR_QUALIFIERS = frozenset({
    "condition", "conditions", "boundary_conditions", "limits", "limitations",
})


def _locator_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _semantic_locator(value: Any, path: str) -> dict[str, Any]:
    """Select source-authored whole fields/records; never cut or paraphrase text.

    Unknown fields remain eligible after semantic fields, so this is not a
    domain-specific evidence filter. Finding/condition objects are atomic;
    sibling qualifiers and claims are also selected together or omitted together.
    """
    original = _locator_json(value)
    if len(original) <= _LOCATOR_CONTENT_BUDGET:
        return {"path": path, "source_excerpt": original, "excerpt": False}

    omitted = []
    if isinstance(value, Mapping):
        selected: dict[str, Any] = {}
        priority = {key: index for index, key in enumerate(_LOCATOR_PRIORITY)}
        keys = sorted(value, key=lambda key: (priority.get(key, len(priority)), str(key)))
        paired = (set(value) & _LOCATOR_CLAIMS) | (set(value) & _LOCATOR_QUALIFIERS)
        if not (set(value) & _LOCATOR_CLAIMS and set(value) & _LOCATOR_QUALIFIERS):
            paired = set()
        handled = set()
        first_addition = {}
        for key in keys:
            if key in handled:
                continue
            group = [other for other in keys if other in paired] if key in paired else [key]
            handled.update(group)
            addition = {other: value[other] for other in group}
            if not first_addition:
                first_addition = addition
            if len(_locator_json({**selected, **addition})) <= _LOCATOR_CONTENT_BUDGET:
                selected.update(addition)
                continue
            # Each list record retains all its fields, including its conditions.
            # Do not split a list coupled to qualifiers stored beside that list.
            if len(group) == 1 and isinstance(value[key], list):
                records = []
                for record in value[key]:
                    candidate = {**selected, key: [*records, record]}
                    if len(_locator_json(candidate)) <= _LOCATOR_CONTENT_BUDGET:
                        records.append(record)
                if records:
                    selected[key] = records
            omitted.extend(f"{path}.{other}" for other in group)
        if not selected:
            selected = first_addition
            omitted = [item for item in omitted if item not in {f"{path}.{key}" for key in selected}]
        excerpt = _locator_json(selected)
    elif isinstance(value, list):
        records = []
        for record in value:
            if len(_locator_json([*records, record])) <= _LOCATOR_CONTENT_BUDGET:
                records.append(record)
        if not records and value:
            records = [value[0]]
        excerpt = _locator_json(records)
        if len(records) != len(value):
            omitted.append(path)
    else:
        # A long passage is an atomic source field, not a safe prefix to quote.
        excerpt = original
    return {
        "path": path,
        "source_excerpt": excerpt,
        "excerpt": bool(omitted),
        "atomic_content_budget_overflow": len(excerpt) > _LOCATOR_CONTENT_BUDGET,
        "omitted_material_paths": omitted,
        "omitted_materials_requestable": True,
    }


def _locator_summary(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose bounded semantic source locators, never generated summaries."""
    return [
        _semantic_locator(row[key], key)
        for key in sorted(_CONTENT_KEYS)
        if key in row
    ]


def build_material_catalog(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Index every supplied material channel without rewriting any record."""

    strengthening._verify_envelope(payload)
    entries: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}

    def add(access_id: str, channel: str, index_path: str, row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        entry = {
            "access_id": access_id,
            "channel": channel,
            "index_path": index_path,
            "identity": _material_identity(row),
            "available_material_paths": _paths(row),
            "source_supplied_locators": _locator_summary(row),
            "record_sha256": _hash(row),
        }
        entries.append(entry)
        lookup[access_id] = {"entry": entry, "record": _copy(row)}

    for channel in _MATERIAL_CHANNELS:
        rows = payload.get(channel) or []
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, Mapping)):
            for index, row in enumerate(rows):
                add(f"{channel}[{index}]", channel, f"{channel}[{index}]", row)
                if isinstance(row, Mapping):
                    for nested_key in ("sources", "source_materials", "candidate_materials", "tool_supplement_materials"):
                        nested_rows = row.get(nested_key) or []
                        if isinstance(nested_rows, Sequence) and not isinstance(nested_rows, (str, bytes, Mapping)):
                            for nested_index, nested_row in enumerate(nested_rows):
                                add(
                                    f"{channel}[{index}].{nested_key}[{nested_index}]",
                                    channel,
                                    f"{channel}[{index}].{nested_key}[{nested_index}]",
                                    nested_row,
                                )

    navigation = payload.get("candidate_navigation")
    if isinstance(navigation, Mapping):
        rows = navigation.get("candidate_materials") or []
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, Mapping)):
            for index, row in enumerate(rows):
                add(
                    f"candidate_navigation.candidate_materials[{index}]",
                    "candidate_navigation",
                    f"candidate_navigation.candidate_materials[{index}]",
                    row,
                )
                if isinstance(row, Mapping):
                    for nested_key in ("sources", "source_materials", "candidate_materials"):
                        nested_rows = row.get(nested_key) or []
                        if isinstance(nested_rows, Sequence) and not isinstance(nested_rows, (str, bytes, Mapping)):
                            for nested_index, nested_row in enumerate(nested_rows):
                                add(
                                    f"candidate_navigation.candidate_materials[{index}].{nested_key}[{nested_index}]",
                                    "candidate_navigation",
                                    f"candidate_navigation.candidate_materials[{index}].{nested_key}[{nested_index}]",
                                    nested_row,
                                )

    coverage = {channel: 0 for channel in (*_MATERIAL_CHANNELS, "candidate_navigation")}
    for entry in entries:
        coverage[entry["channel"]] = coverage.get(entry["channel"], 0) + 1
    locator_policy = {
        "version": "semantic_whole_fields_v1",
        "content_char_budget_per_material_root": _LOCATOR_CONTENT_BUDGET,
        "selection": "whole_fields_or_records",
        "empty_view_fallback": "one_whole_atomic_field_or_record_may_exceed_budget",
        "omitted_materials_requestable": True,
    }
    catalog = {
        "schema_version": "optomind.outline_on_demand_catalog.v1",
        "locator_policy": locator_policy,
        "coverage": coverage,
        "entries": entries,
        "catalog_sha256": _hash({"entries": entries, "locator_policy": locator_policy}),
    }
    # Keep the local lookup private to the resolver; it is never sent as an
    # answer or used to replace the source payload.
    catalog["_lookup"] = lookup
    return catalog


def public_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _copy(value)
        for key, value in catalog.items()
        if key != "_lookup"
    }


_IDENTITY_REFERENCE_KEYS = frozenset({
    "source_handle", "paper_id", "original_paper_id", "original_source_handle",
    "doi", "study_id", "source_handles", "paper_ids", "material_handles",
    "evidence_handles", "material_ids", "source_ids", "access_id",
})


def _payload_identity_terms(payload: Mapping[str, Any], *, editable_only: bool = False) -> set[str]:
    """Collect explicit material identities without scanning arbitrary prose.

    Priority locators are scoped to the currently editable units.  Read-only
    roles remain available in the light index and can still be expanded by an
    explicit search/request, but they do not inflate the initial semantic view.
    """

    terms: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            recursive_context = key in {
                "chapter_plan", "selection_context", "shared_outline", "full_chapter_context",
                "readonly_neighbor_unit_roles", "supporting_studies", "case_objects",
                "paragraph_briefs",
            }
            for child_key, child_value in value.items():
                child_name = str(child_key)
                if child_name in _IDENTITY_REFERENCE_KEYS:
                    visit(child_value, child_name)
                elif recursive_context or child_name in {
                    "chapter_plan", "selection_context", "shared_outline", "full_chapter_context",
                    "readonly_neighbor_unit_roles", "supporting_studies", "case_objects", "paragraph_briefs",
                }:
                    visit(child_value, child_name)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item, key)
        elif key in _IDENTITY_REFERENCE_KEYS and value not in (None, ""):
            text = str(value).strip().casefold()
            if text:
                terms.add(text)

    if editable_only:
        editable = {str(value) for value in payload.get("modifiable_unit_ids") or []}
        plan = payload.get("chapter_plan") if isinstance(payload.get("chapter_plan"), Mapping) else {}
        units = [
            unit for unit in plan.get("units") or []
            if isinstance(unit, Mapping) and str(unit.get("unit_id") or unit.get("id") or "") in editable
        ]
        visit({"chapter_plan": {"units": units}})
        context = payload.get("selection_context")
        if isinstance(context, Mapping) and set(str(value) for value in context.get("selected_unit_ids") or []) & editable:
            visit({"selection_context": {
                "selected_unit_ids": [str(value) for value in context.get("selected_unit_ids") or [] if str(value) in editable],
                "improvement_focus": context.get("improvement_focus") or [],
            }})
    else:
        visit(payload)
    return terms


def _priority_access_ids(catalog: Mapping[str, Any], payload: Mapping[str, Any]) -> set[str]:
    """Find material rows explicitly linked to the current outline/focus."""

    terms = _payload_identity_terms(payload, editable_only=True)
    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
    priority: set[str] = set()
    if not isinstance(lookup, Mapping):
        return priority
    for access_id, info in lookup.items():
        entry = info.get("entry") if isinstance(info, Mapping) else {}
        identity = entry.get("identity") if isinstance(entry, Mapping) else {}
        values = {str(access_id).casefold()}
        if isinstance(identity, Mapping):
            values.update(
                str(value).strip().casefold()
                for value in identity.values()
                if value not in (None, "")
            )
        if values & terms:
            priority.add(str(access_id))
    return priority


def _priority_source_locators(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose structured findings with their paired conditions and limits.

    This intentionally does not serialize every A/B/deep-read root.  It keeps
    source-authored finding/result/negative/condition/limit objects whole, so a
    paired qualifier cannot be cut into an arbitrary preview.
    """

    selected_keys = (
        "key_findings", "key_results", "finding", "findings", "claim", "claims",
        "results", "negative_findings", "negative_results", "negative_finding",
        "conditions", "condition", "boundary_conditions", "limits", "limitations",
        "planning_summary", "scope_interpretation_cautions", "contribution_and_limits",
        "review_summary", "review_planning_summary", "usable_content",
    )

    def extract(value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            output = {}
            for child_key, child_value in value.items():
                name = str(child_key)
                lowered = name.casefold()
                if name in selected_keys or any(token in lowered for token in ("finding", "result", "condition", "limit", "negative", "planning_summary", "usable")):
                    output[name] = _copy(child_value)
                elif isinstance(child_value, (Mapping, list)):
                    nested = extract(child_value, name)
                    if nested not in ({}, [], None):
                        output[name] = nested
            return output
        if isinstance(value, list):
            rows = []
            for item in value:
                nested = extract(item, key)
                if nested not in ({}, [], None):
                    rows.append(nested)
            return rows
        return _copy(value) if key in selected_keys else None

    locators = []
    for root_key in sorted(_CONTENT_KEYS):
        if root_key not in record:
            continue
        extracted = extract(record[root_key], root_key)
        if extracted in ({}, [], None):
            continue
        locators.append({
            "path": root_key,
            "source_excerpt": _locator_json(extracted),
            "excerpt": False,
            "priority": "structured_findings_with_paired_qualifiers",
            "omitted_materials_requestable": True,
        })
    return locators


def navigation_catalog(
    catalog: Mapping[str, Any], payload: Mapping[str, Any], *, page_size: int = 24,
) -> dict[str, Any]:
    """Project a full identity/navigation index with progressive semantic pages.

    The complete records remain in ``catalog['_lookup']``.  The returned view
    deliberately contains no long semantic locators for unrelated records;
    priority records and one pageable discovery window retain whole-field
    locators, while every record remains requestable by stable identity.
    """

    page_size = max(1, min(64, int(page_size)))
    entries = [entry for entry in catalog.get("entries") or [] if isinstance(entry, Mapping)]
    priority_ids = _priority_access_ids(catalog, payload)
    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
    units = [*(payload.get("chapter_plan") or {}).get("units", []),
             *(payload.get("readonly_neighbor_unit_roles") or [])]
    unit_terms = [(str(unit.get("unit_id") or unit.get("id") or ""),
                   _payload_identity_terms({"chapter_plan": {"units": [unit]}}))
                  for unit in units if isinstance(unit, Mapping)]
    light_entries: list[dict[str, Any]] = []
    priority_entries: list[dict[str, Any]] = []
    for entry in entries:
        all_paths = [str(value) for value in entry.get("available_material_paths") or []]
        material_types = sorted({
            re.split(r"[.\[]", value, maxsplit=1)[0]
            for value in all_paths if value
        })
        current = {
            key: _copy(entry[key])
            for key in (
                "access_id", "channel", "index_path", "identity",
            )
            if key in entry
        }
        current["available_material_types"] = material_types
        access_id = str(entry.get("access_id") or "")
        current["full_record_available"] = True
        current["priority_reference"] = access_id in priority_ids
        identity_values = {str(value).strip().casefold() for value in current.get("identity", {}).values() if value}
        current["task_unit_ids"] = sorted({unit_id for unit_id, terms in unit_terms
                                          if unit_id and identity_values & terms})
        light_entries.append(current)
        if access_id in priority_ids and isinstance(lookup, Mapping):
            info = lookup.get(access_id) or {}
            record = info.get("record") if isinstance(info, Mapping) else None
            if isinstance(record, Mapping):
                priority_entries.append({
                    **current,
                    "source_supplied_locators": _priority_source_locators(record),
                    "full_semantic_locator": True,
                })
    # The access planner receives structured semantic locators only for
    # records linked to the editable units.  The default discovery page is
    # deliberately light; a page/search request is the explicit operation
    # that expands a complete record.  This keeps an arbitrary first page
    # from becoming an accidental full-material filter.
    non_priority = [entry for entry in light_entries if not entry.get("priority_reference")]
    page_count = (len(light_entries) + page_size - 1) // page_size if light_entries else 0
    page_rows: list[dict[str, Any]] = []
    projected = {
        "schema_version": "optomind.outline_on_demand_navigation.v1",
        "mode": "navigation_first_progressive_disclosure",
        "coverage": _copy(catalog.get("coverage") or {}),
        "entry_count": len(light_entries),
        "priority_entry_count": len(priority_entries),
        "entries": light_entries,
        "priority_semantic_entries": priority_entries,
        "semantic_page": {
            "page": 0,
            "page_size": page_size,
            "page_count": page_count,
            "returned_count": 0,
            "entries": page_rows,
            "next_page_request": {"request_type": "catalog_page", "page": 0, "page_size": page_size} if page_count else None,
        },
        "discovery_contract": {
            "all_entries_are_requestable": True,
            "unselected_records_are_unread_not_missing": True,
            "whole_records_only": True,
            "conditions_and_limits_are_atomic": True,
            "search_request": {"request_type": "catalog_search", "query": "...", "offset": 0, "limit": page_size},
        },
        "catalog_sha256": catalog.get("catalog_sha256"),
        "navigation_catalog_sha256": _hash({
            "entries": light_entries,
            "priority_semantic_entries": priority_entries,
            "semantic_page": {"page": 0, "page_size": page_size, "page_count": page_count},
        }),
    }
    return projected


def access_messages(payload: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build an issue-free, material-location-only Plus request."""

    strengthening._verify_envelope(payload)
    modifiable, readonly = _unit_ids(payload)
    system = (
        "你是自主细纲的材料访问规划器，不是修订负责人。只返回 JSON："
        "status 为 access_plan、partial 或 no_change，并可带 material_requests。"
        "material_requests 只能请求当前目录中的 access_id、source_handle 或材料路径，"
        "也可使用 request_type=catalog_page/page 或 request_type=catalog_search/query 探索未读目录；"
        "每项可带 unit_ids 和 reason；不得返回 updated_plan、chapter_updates、issues、正文、答案或科学修订建议。"
        "本轮可修改任务严格限于 modifiable_unit_ids；优先为这些任务取材。read_only_unit_ids 和邻居职责只能用于边界参照，"
        "若共享证据同时服务可编辑任务可以共同标注；若材料只被只读邻居标注，仍保留请求并让负责人看到范围诊断，不把只读任务变成编辑任务。"
        "目录覆盖 source_materials、candidate_materials、candidate_navigation、tool_materials，"
        "包含原研究身份和可用条件路径；综述转述材料不因没有自身 A/B 或全文而失去资格。"
        "当前目录是轻量身份与路径导航，包含优先任务的完整语义定位和一个分页发现窗口；"
        "未附完整语义并不表示材料不存在。可用 catalog_page 或 catalog_search 请求更多入口，"
        "也可直接按稳定 access_id/source_handle 请求完整原始记录。条件、限制、阴性结果和综述转述记录不可截断。"
        "未出现在本轮已取记录中表示尚未读取，不表示不存在；不要把本规划结果当成材料过滤硬限制。"
    )
    selection_context = payload.get("selection_context")
    if isinstance(selection_context, Mapping):
        system += (
            "上一层自主选择器给出了以下投入范围说明；它只是机器生成的编辑线索，不是科学事实、人工问题清单或答案。"
            "请独立依据当前细纲和材料目录决定取材，允许拒绝该线索："
            + json.dumps(_copy(selection_context), ensure_ascii=False, separators=(",", ":"))
        )
    user_value = {
        "research_question": payload.get("research_question"),
        "chapter_id": payload.get("chapter_id"),
        "chapter_plan": payload.get("chapter_plan"),
        "modifiable_unit_ids": modifiable,
        "read_only_unit_ids": readonly,
        "readonly_neighbor_unit_roles": _model_visible_readonly_roles(payload),
        "full_chapter_context": payload.get("full_chapter_context") or {},
        "actual_local_body": payload.get("actual_local_body") or "",
        "material_catalog": navigation_catalog(catalog, payload),
        "return_contract": {
            "status_values": ["access_plan", "partial", "no_change"],
            "material_requests_only": True,
        "supported_request_types": ["material", "catalog_page", "catalog_search", "readonly_unit"],
            "no_scientific_revision": True,
            "editable_scope_only": True,
            "readonly_evidence_may_be_preserved_with_diagnostic": True,
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_value, ensure_ascii=False, separators=(",", ":"))},
    ]


def _request_rows(parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = parsed.get("material_requests")
    if rows is None:
        rows = parsed.get("requests")
    if rows is None and isinstance(parsed.get("needs_materials"), list):
        rows = parsed.get("needs_materials")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise OnDemandMaterialError("material_requests_must_be_list")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise OnDemandMaterialError(f"material_request_not_object:{index}")
        result.append(dict(row))
    return result


def validate_access_response(parsed: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(parsed, Mapping):
        raise OnDemandMaterialError("access_response_not_object")
    for key in parsed:
        if str(key).casefold() in _CATALOG_FORBIDDEN:
            raise OnDemandMaterialError(f"access_response_contains_revision_control:{key}")
    status = str(parsed.get("status") or "").casefold()
    if status not in {"access_plan", "partial", "no_change"}:
        raise OnDemandMaterialError("access_status_invalid")
    requests = _request_rows(parsed)
    if status == "no_change" and requests:
        raise OnDemandMaterialError("no_change_with_material_requests")
    if status != "no_change" and not requests:
        raise OnDemandMaterialError("access_requests_missing")
    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else None
    if not isinstance(lookup, Mapping):
        raise OnDemandMaterialError("catalog_lookup_missing")
    for index, request in enumerate(requests):
        request_type = str(request.get("request_type") or "material").casefold()
        if request_type not in {"material", "record", "catalog_page", "catalog_search", "readonly_unit"}:
            raise OnDemandMaterialError(f"material_request_type_invalid:{index}")
        if request_type == "readonly_unit" and not str(request.get("unit_id") or "").strip():
            raise OnDemandMaterialError(f"readonly_unit_parameters_invalid:{index}")
        if request_type == "catalog_page":
            try:
                page = int(request.get("page", 0))
                page_size = int(request.get("page_size", 24))
            except (TypeError, ValueError) as exc:
                raise OnDemandMaterialError(f"catalog_page_parameters_invalid:{index}") from exc
            if page < 0 or not 1 <= page_size <= 64:
                raise OnDemandMaterialError(f"catalog_page_parameters_invalid:{index}")
        if request_type == "catalog_search":
            query = str(request.get("query") or "").strip()
            try:
                limit = int(request.get("limit", 24))
                offset = int(request.get("offset", 0))
            except (TypeError, ValueError) as exc:
                raise OnDemandMaterialError(f"catalog_search_parameters_invalid:{index}") from exc
            if not query or not 0 <= offset or not 1 <= limit <= 64:
                raise OnDemandMaterialError(f"catalog_search_parameters_invalid:{index}")
    return {"status": status, "material_requests": requests}


def _catalog_navigation_matches(request: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[str]:
    """Resolve explicit page/search exploration to stable catalog access IDs."""

    entries = [entry for entry in catalog.get("entries") or [] if isinstance(entry, Mapping)]
    request_type = str(request.get("request_type") or "material").casefold()
    if request_type == "readonly_unit":
        return [], True
    if request_type == "catalog_page":
        page = int(request.get("page", 0))
        page_size = int(request.get("page_size", 24))
        start = page * page_size
        matches = [str(entry.get("access_id") or "") for entry in entries[start:start + page_size]]
        # An out-of-range page is a normal discovery miss.  Preserve the
        # request as a zero-result trace so another useful request in the
        # same access response is still resolved.
        return [value for value in matches if value]
    if request_type == "catalog_search":
        query = str(request.get("query") or "").strip().casefold()
        limit = int(request.get("limit", 24))
        offset = int(request.get("offset", 0))
        lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
        all_matches = []
        for entry in entries:
            access_id = str(entry.get("access_id") or "")
            info = lookup.get(access_id) if isinstance(lookup, Mapping) else None
            record = info.get("record") if isinstance(info, Mapping) else None
            searchable = _locator_json({
                "identity": entry.get("identity"),
                "paths": entry.get("available_material_paths"),
                "record": record,
            })
            if query in searchable.casefold():
                all_matches.append(access_id)
        matches = all_matches[offset:offset + limit]
        return matches
    return []


def _request_access_matches(
    request: Mapping[str, Any], catalog: Mapping[str, Any],
) -> tuple[list[str], bool]:
    """Return matching paths and whether the request named an exact path."""

    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
    request_type = str(request.get("request_type") or "material").casefold()
    if request_type in {"catalog_page", "catalog_search"}:
        matches = _catalog_navigation_matches(request, catalog)
        return matches, True
    access_id = str(request.get("access_id") or "").strip()
    if access_id and access_id in lookup:
        matches = [access_id]
        explicit = True
    elif access_id:
        # A planner may name a concrete field or passage as
        # ``source_materials[2].deep_read_material``.  Resolve the record
        # prefix while retaining the suffix in the access trace.
        prefixes = [str(key) for key in lookup if access_id.startswith(str(key) + ".")]
        if not prefixes:
            raise OnDemandMaterialError("material_request_identity_ambiguous_or_missing")
        matches = [max(prefixes, key=len)]
        explicit = True
    else:
        handle = str(request.get("source_handle") or request.get("paper_id") or "").strip()
        if not handle:
            raise OnDemandMaterialError("material_request_identity_missing")
        matches = []
        for key, value in lookup.items():
            identity = value.get("entry", {}).get("identity", {}) if isinstance(value, Mapping) else {}
            if handle in {str(identity.get("source_handle") or ""), str(identity.get("paper_id") or "")}:
                matches.append(str(key))
        if not matches:
            raise OnDemandMaterialError("material_request_identity_ambiguous_or_missing")
        explicit = False

    # When a request includes both a path and an identity, the identity must
    # agree with that path.  This prevents an explicit access_id from being
    # used to smuggle a different paper identity into the owner request.
    requested_identity = {
        key: request.get(key)
        for key in _STABLE_IDENTITY_FIELDS
        if request.get(key) not in (None, "")
    }
    if requested_identity:
        entry = lookup[matches[0]].get("entry", {})
        identity = entry.get("identity", {}) if isinstance(entry, Mapping) else {}
        if not _identities_compatible(requested_identity, identity):
            raise OnDemandMaterialError("material_request_identity_conflict")

    identities = [
        (lookup[match].get("entry", {}) or {}).get("identity", {})
        for match in matches
    ]
    if len(matches) > 1 and not _identity_group_compatible(identities):
        raise OnDemandMaterialError("material_request_identity_conflict")
    return matches, explicit


def _identity_aliases(canonical: str, lookup: Mapping[str, Any]) -> list[str]:
    """Return all catalog paths for the canonical study identity."""

    info = lookup.get(canonical) if isinstance(lookup, Mapping) else None
    entry = info.get("entry") if isinstance(info, Mapping) else {}
    identity = entry.get("identity", {}) if isinstance(entry, Mapping) else {}
    aliases = []
    for access_id, value in lookup.items():
        candidate_entry = value.get("entry", {}) if isinstance(value, Mapping) else {}
        candidate_identity = candidate_entry.get("identity", {}) if isinstance(candidate_entry, Mapping) else {}
        if _identities_compatible(identity, candidate_identity):
            aliases.append(str(access_id))
    matching_identities = [
        (lookup[access_id].get("entry", {}) or {}).get("identity", {})
        for access_id in aliases
    ]
    if not _identity_group_compatible(matching_identities):
        raise OnDemandMaterialError("material_request_identity_conflict")
    return sorted(aliases)


def _find_access_id(request: Mapping[str, Any], catalog: Mapping[str, Any]) -> str:
    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
    matches, _explicit = _request_access_matches(request, catalog)
    return _canonical_access_id(matches, lookup)


def _navigation_request_metadata(
    request: Mapping[str, Any], catalog: Mapping[str, Any], matches: Sequence[str],
) -> dict[str, Any]:
    """Record paging/search coverage so a bounded page cannot silently omit rows."""

    request_type = str(request.get("request_type") or "material").casefold()
    if request_type == "catalog_page":
        entries = [entry for entry in catalog.get("entries") or [] if isinstance(entry, Mapping)]
        page = int(request.get("page", 0))
        page_size = int(request.get("page_size", 24))
        total = len(entries)
        offset = page * page_size
        return {
            "catalog_offset": offset,
            "catalog_limit": page_size,
            "catalog_total_entries": total,
            "catalog_returned_count": len(matches),
            "catalog_remaining_count": max(0, total - offset - len(matches)),
            "catalog_next_request": (
                {"request_type": "catalog_page", "page": page + 1, "page_size": page_size}
                if offset + len(matches) < total else None
            ),
        }
    if request_type == "catalog_search":
        query = str(request.get("query") or "").strip().casefold()
        lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
        all_matches = []
        for entry in catalog.get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            access_id = str(entry.get("access_id") or "")
            info = lookup.get(access_id) if isinstance(lookup, Mapping) else None
            record = info.get("record") if isinstance(info, Mapping) else None
            searchable = _locator_json({
                "identity": entry.get("identity"),
                "paths": entry.get("available_material_paths"),
                "record": record,
            })
            if query in searchable.casefold():
                all_matches.append(access_id)
        offset = int(request.get("offset", 0))
        limit = int(request.get("limit", 24))
        return {
            "catalog_offset": offset,
            "catalog_limit": limit,
            "catalog_total_matches": len(all_matches),
            "catalog_returned_count": len(matches),
            "catalog_remaining_count": max(0, len(all_matches) - offset - len(matches)),
            "catalog_next_request": (
                {"request_type": "catalog_search", "query": request.get("query"), "offset": offset + len(matches), "limit": limit}
                if offset + len(matches) < len(all_matches) else None
            ),
        }
    return {}


def resolve_material_requests(
    payload: Mapping[str, Any], catalog: Mapping[str, Any], parsed: Mapping[str, Any],
    *, prior_access_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolve requests to original rows, retaining identity and conditions."""

    validated = validate_access_response(parsed, catalog)
    lookup = catalog["_lookup"]
    selected: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    readonly_context_reads: list[dict[str, Any]] = []
    seen = set(str(value) for value in prior_access_ids)
    for request in validated["material_requests"]:
        request_type = str(request.get("request_type") or "material").casefold()
        if request_type == "readonly_unit":
            unit_id = str(request.get("unit_id") or "").strip()
            if unit_id in set(str(value) for value in payload.get("modifiable_unit_ids") or []):
                raise OnDemandMaterialError("readonly_unit_is_editable")
            role = next(
                (row for row in payload.get("readonly_neighbor_unit_roles") or ()
                 if isinstance(row, Mapping) and str(row.get("unit_id") or row.get("id") or "") == unit_id),
                None,
            )
            if role is None:
                raise OnDemandMaterialError("readonly_unit_not_found")
            readonly_context_reads.append({"unit_id": unit_id, "role": _copy(role), "request": _copy(request)})
            continue
        requested_matches, explicit_path = _request_access_matches(request, catalog)
        navigation_meta = _navigation_request_metadata(request, catalog, requested_matches)
        if request_type in {"catalog_page", "catalog_search"} and not requested_matches:
            trace_rows.append({
                "request_type": request_type,
                "requested_paths": [],
                "unit_ids": [str(value) for value in request.get("unit_ids") or []],
                "catalog_no_matches": True,
                **_copy(navigation_meta),
            })
            continue
        canonical_access_id = _canonical_access_id(requested_matches, lookup)
        # A handle/paper_id request may match the same study in multiple
        # channels.  Keep every alias so tool retellings and source cards
        # contribute their distinct fields.  An explicit access_id remains
        # path-scoped, while its alias list is still visible to the owner.
        if request_type in {"catalog_page", "catalog_search"}:
            # Exploration requests intentionally name a set of concrete
            # catalog rows; do not collapse the page to its first row.
            access_ids = [str(value) for value in requested_matches]
            alias_ids = list(access_ids)
        else:
            access_ids = [canonical_access_id] if explicit_path else _identity_aliases(canonical_access_id, lookup)
            alias_ids = [canonical_access_id] if explicit_path else _identity_aliases(canonical_access_id, lookup)
        requested_paths = request.get("material_paths") or request.get("paths") or []
        raw_access_id = str(request.get("access_id") or "")
        if not requested_paths and raw_access_id.startswith(canonical_access_id + "."):
            requested_paths = [raw_access_id[len(canonical_access_id) + 1:]]
        for access_id in access_ids:
            if access_id in seen:
                continue
            seen.add(access_id)
            if request_type in {"catalog_page", "catalog_search"}:
                # A discovery page contains different papers, not aliases of
                # its first result.  Keep each identity group independent for
                # later complete-record batching and audit.
                row_alias_ids = _identity_aliases(access_id, lookup)
                row_canonical_id = _canonical_access_id(row_alias_ids, lookup)
            else:
                row_alias_ids = alias_ids
                row_canonical_id = canonical_access_id
            record_info = lookup[access_id]
            row = _copy(record_info["record"])
            entry = _copy(record_info["entry"])
            selected.append({
                "access_id": access_id,
                "canonical_access_id": row_canonical_id,
                "identity_aliases": _copy(row_alias_ids),
                "channel": entry["channel"],
                "index_path": entry["index_path"],
                "identity": entry["identity"],
                "request_type": request_type,
                "requested_paths": _copy(requested_paths),
                "unit_ids": [str(value) for value in request.get("unit_ids") or []],
                **_copy(navigation_meta),
                "record": row,
            })
            trace_rows.append({
                "access_id": access_id,
                "canonical_access_id": row_canonical_id,
                "identity_aliases": _copy(row_alias_ids),
                "channel": entry["channel"],
                "index_path": entry["index_path"],
                "identity": entry["identity"],
                "record_sha256": entry["record_sha256"],
                "request_type": request_type,
                "requested_paths": _copy(requested_paths),
                "unit_ids": [str(value) for value in request.get("unit_ids") or []],
                **_copy(navigation_meta),
            })
    return {
        "status": validated["status"],
        "material_requests": _copy(validated["material_requests"]),
        "selected_materials": selected,
        "trace": trace_rows,
        "resolved_count": len(selected),
        "access_ids": sorted(seen),
        "catalog_sha256": catalog.get("catalog_sha256"),
        "selection_diagnostics": _selection_diagnostics(payload, selected),
        "readonly_context_reads": readonly_context_reads,
    }


def _selection_diagnostics(payload: Mapping[str, Any], selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Expose generic scope diagnostics without changing an access result.

    An access model may legitimately choose evidence attached to a read-only
    neighbour.  Preserve that evidence and make the scope mismatch visible to
    the owner instead of silently reselecting or discarding it.
    """

    modifiable, readonly = _unit_ids(payload)
    modifiable_set = set(modifiable)
    readonly_set = set(readonly)
    readonly_only = []
    unknown = []
    for item in selected:
        ids = [str(value) for value in item.get("unit_ids") or [] if str(value).strip()]
        item_id = str(item.get("access_id") or "")
        if ids and not (set(ids) & modifiable_set):
            if set(ids) & readonly_set:
                readonly_only.append({"access_id": item_id, "unit_ids": ids})
            else:
                unknown.append({"access_id": item_id, "unit_ids": ids})
    return {
        "modifiable_unit_ids": modifiable,
        "read_only_unit_ids": readonly,
        "readonly_only_materials": readonly_only,
        "unknown_unit_materials": unknown,
        "evidence_preserved": True,
        "action": "owner_may_use_shared_evidence; no_automatic_reselection",
    }


_NESTED_ACCESS_RE = re.compile(r"^(?P<parent>.+)\.(?P<key>[A-Za-z_][A-Za-z0-9_]*)\[(?P<index>\d+)\]$")


def _nested_access_parent(access_id: str) -> tuple[str, str] | None:
    match = _NESTED_ACCESS_RE.match(str(access_id or ""))
    if not match:
        return None
    suffix = f"/{match.group('key')}/{match.group('index')}"
    return match.group("parent"), suffix


def full_catalog_trace(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Build a conservative offline upper-bound trace from unique catalog rows."""

    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else None
    if not isinstance(lookup, Mapping):
        raise OnDemandMaterialError("catalog_lookup_missing")
    selected = []
    trace_rows = []
    seen_hashes: set[str] = set()
    duplicate_access_ids: list[str] = []
    all_access_ids: list[str] = []
    for access_id, info in lookup.items():
        if not isinstance(info, Mapping):
            continue
        entry = info.get("entry") if isinstance(info.get("entry"), Mapping) else {}
        record = info.get("record")
        if not isinstance(record, Mapping):
            continue
        all_access_ids.append(str(access_id))
        record_hash = str(entry.get("record_sha256") or _hash(record))
        if record_hash in seen_hashes:
            duplicate_access_ids.append(str(access_id))
            continue
        seen_hashes.add(record_hash)
        selected.append({
            "access_id": str(access_id),
            "channel": entry.get("channel"),
            "index_path": entry.get("index_path"),
            "identity": _copy(entry.get("identity") or {}),
            "requested_paths": [],
            "unit_ids": [],
            "record": _copy(record),
        })
        trace_rows.append({
            "access_id": str(access_id),
            "channel": entry.get("channel"),
            "index_path": entry.get("index_path"),
            "identity": _copy(entry.get("identity") or {}),
            "record_sha256": record_hash,
            "requested_paths": [],
            "unit_ids": [],
        })
    return {
        "status": "upper_bound_all_catalog_materials",
        "material_requests": [],
        "selected_materials": selected,
        "trace": trace_rows,
        "resolved_count": len(selected),
        "access_ids": all_access_ids,
        "catalog_sha256": catalog.get("catalog_sha256"),
        "duplicate_access_ids_omitted": duplicate_access_ids,
    }


def _owner_payload(payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *, continuation: bool = False) -> dict[str, Any]:
    result = _copy(payload)
    result["on_demand_material_access"] = {
        "schema_version": "optomind.outline_on_demand_access.v1",
        "catalog": public_catalog(catalog),
        "resolved_materials": _copy(trace.get("selected_materials") or []),
        "trace": _copy(trace.get("trace") or []),
        "selection_diagnostics": _copy(trace.get("selection_diagnostics") or {}),
        "readonly_context_reads": _copy(trace.get("readonly_context_reads") or []),
        "unread_materials_remain_requestable": True,
        "continuation": bool(continuation),
    }
    result["autonomous_outline_strengthening"] = {
        **dict(result.get("autonomous_outline_strengthening") or {}),
        "input_mode": "on_demand_material_access",
    }
    return result


def _model_visible_readonly_roles(
    payload: Mapping[str, Any], readonly_context_reads: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Keep direct related duties complete and make other duties expandable."""

    rows = payload.get("readonly_neighbor_unit_roles") or []
    context = payload.get("selection_context")
    direct: set[str] = set(str(value) for value in payload.get("related_read_only_unit_ids") or [])
    if isinstance(context, Mapping):
        direct.update(str(value) for value in context.get("related_read_only_unit_ids") or [])
    direct.update(str(row.get("unit_id") or "") for row in readonly_context_reads if isinstance(row, Mapping))
    compact_keys = {
        "unit_id", "id", "role", "responsibility", "thesis", "scope", "boundary",
        "argument_relations", "reader_objective", "source_handles", "expandable_ids",
        # Preserve the complete compact responsibility needed to place a unit
        # in the chapter argument.  Paragraph briefs and supporting studies
        # remain expandable; these fields are the unit-level boundary/flow.
        "chapter_id", "chapter_title", "title", "substantive_point",
        "ordered_development", "synthesis", "synthesis_and_transition", "transition",
    }
    result: list[dict[str, Any]] = []
    expandable: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        unit_id = str(row.get("unit_id") or row.get("id") or "")
        if unit_id and unit_id in direct:
            result.append(_copy(row))
            continue
        compact = {key: _copy(row[key]) for key in sorted(compact_keys) if key in row}
        if unit_id:
            compact["expandable_readback"] = {"unit_id": unit_id, "source": "local_full_payload"}
            expandable.append(unit_id)
        result.append(compact)
    if expandable:
        result.append({
            "_navigation": "non_direct_readonly_roles_are_expandable",
            "expandable_unit_ids": expandable,
            "source": "local_full_payload",
        })
    return result


def _model_visible_payload(full_payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *, continuation: bool = False) -> dict[str, Any]:
    """Project only locally resolved material records into the model request.

    The full payload remains local for response validation and arrangement.  A
    model sees the current plan, identities, a complete lightweight catalog,
    and the records actually resolved for this pass; unrequested full records
    are not silently transmitted as if they had been read.
    """

    visible = _copy(full_payload)
    readonly_reads = trace.get("readonly_context_reads") or []
    visible["readonly_neighbor_unit_roles"] = _model_visible_readonly_roles(full_payload, readonly_reads)
    chapter = full_payload.get("chapter")
    if isinstance(chapter, Mapping):
        # Keep chapter purpose/scope and reading direction, but do not resend
        # the chapter-wide source-handle/excluded-ID inventory.  The complete
        # local payload remains available to validation and future readback.
        visible["chapter"] = {
            key: _copy(chapter[key])
            for key in (
                "chapter_id", "title", "coordinated_scope", "purpose", "scope",
                "reader_objective", "thesis", "argument_relations", "transition",
            )
            if key in chapter
        }
    # The full source_identity_map is retained in the local payload for
    # validation/arrangement, but repeats the identity-heavy navigation index
    # in the model request.  The model-visible catalog below is its complete
    # stable-identity replacement; do not resend card paths or excluded-ID
    # inventories a second time.
    if isinstance(full_payload.get("source_identity_map"), Mapping):
        visible["source_identity_map"] = {
            "mode": "on_demand_navigation_catalog",
            "complete_identity_index_in_request": True,
            "catalog_field": "on_demand_material_access.catalog.entries",
        }
    selected = [row for row in trace.get("selected_materials") or [] if isinstance(row, Mapping)]
    source_rows = []
    candidate_rows = []
    tool_rows = []
    pointers: dict[str, str] = {}

    def append_unique(rows: list[dict[str, Any]], record: Mapping[str, Any], access_id: str, root_key: str) -> str:
        record_hash = _hash(record)
        for index, existing in enumerate(rows):
            if _hash(existing) == record_hash:
                pointer = f"#/{root_key}/{index}"
                pointers[access_id] = pointer
                return pointer
        rows.append(_copy(record))
        pointer = f"#/{root_key}/{len(rows) - 1}"
        pointers[access_id] = pointer
        return pointer

    navigation_refs = []
    selected_ids = {str(item.get("access_id") or "") for item in selected if isinstance(item, Mapping)}
    ordered_selected = sorted(
        selected,
        key=lambda item: 1 if _nested_access_parent(str(item.get("access_id") or ""))
        and _nested_access_parent(str(item.get("access_id") or ""))[0] in selected_ids else 0,
    )
    for item in ordered_selected:
        record = item.get("record")
        if not isinstance(record, Mapping):
            continue
        channel = str(item.get("channel") or "")
        access_id = str(item.get("access_id") or "")
        nested = _nested_access_parent(access_id)
        if nested and nested[0] in pointers:
            pointers[access_id] = pointers[nested[0]] + nested[1]
            if channel in {"candidate_materials", "candidate_navigation"}:
                navigation_refs.append({"access_id": access_id, "material_ref": pointers[access_id]})
            continue
        if channel == "source_materials":
            append_unique(source_rows, record, access_id, "source_materials")
        elif channel in {"candidate_materials", "candidate_navigation"}:
            pointer = append_unique(candidate_rows, record, access_id, "candidate_materials")
            navigation_refs.append({"access_id": access_id, "material_ref": pointer})
        elif channel == "tool_materials":
            append_unique(tool_rows, record, access_id, "tool_materials")
    visible["source_materials"] = source_rows
    visible["candidate_materials"] = candidate_rows
    visible["tool_materials"] = tool_rows
    navigation = full_payload.get("candidate_navigation")
    nav_shell = {}
    if isinstance(navigation, Mapping):
        for key in ("access_contract", "chapter_id", "query", "candidate_count", "returned_count", "omitted_candidate_count", "unassigned_relevant_count"):
            if key in navigation:
                nav_shell[key] = _copy(navigation[key])
    # Candidate records have one canonical location in the request.  The
    # navigation shell points there instead of sending a second full copy.
    nav_shell["candidate_materials"] = []
    nav_shell["candidate_material_refs"] = navigation_refs
    visible["candidate_navigation"] = nav_shell
    visible_access = _copy(visible.get("on_demand_material_access") or {})
    visible_access["catalog"] = _catalog_with_material_refs(catalog, full_payload, pointers)
    visible_access["resolved_materials"] = [
        {
            key: _copy(item[key])
            for key in (
                "access_id", "canonical_access_id", "identity_aliases", "channel", "index_path",
                "identity", "request_type", "requested_paths", "unit_ids",
            )
            if key in item
        }
        for item in selected
    ]
    visible_access["trace"] = _copy(trace.get("trace") or [])
    visible_access["selection_diagnostics"] = _copy(trace.get("selection_diagnostics") or {})
    visible_access["continuation"] = bool(continuation)
    visible["on_demand_material_access"] = visible_access
    return visible


def _catalog_with_material_refs(
    catalog: Mapping[str, Any], payload: Mapping[str, Any], pointers: Mapping[str, str],
) -> dict[str, Any]:
    """Point selected catalog entries at full records in the request root."""

    output = navigation_catalog(catalog, payload)
    # The owner receives only the complete records actually resolved by the
    # access pass plus all remaining light entries.  Priority/page semantic
    # locators belong to the access planner; the owner may ask for a concrete
    # page or record through the existing continuation contract.
    output.pop("priority_semantic_entries", None)
    semantic_page = output.get("semantic_page")
    if isinstance(semantic_page, Mapping):
        output["semantic_page"] = {
            key: _copy(value)
            for key, value in semantic_page.items()
            if key != "entries"
        }
        output["semantic_page"]["returned_count"] = 0
    def annotate(entries: Sequence[Any]) -> list[dict[str, Any]]:
        annotated = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            current = _copy(entry)
            access_id = str(current.get("access_id") or "")
            pointer = pointers.get(access_id)
            nested = _nested_access_parent(access_id)
            if pointer is None and nested:
                parent_pointer = pointers.get(nested[0])
                if parent_pointer:
                    pointer = parent_pointer + nested[1]
            if pointer:
                current.pop("source_supplied_locators", None)
                current["material_ref"] = pointer
                current["material_ref_scope"] = "model_payload_root"
                current["full_record_in_request"] = True
                if nested and pointers.get(access_id) is None:
                    current["material_ref_reason"] = "nested_record_of_selected_parent"
            annotated.append(current)
        return annotated

    output["entries"] = annotate(output.get("entries") or [])
    output["priority_semantic_entries"] = annotate(output.get("priority_semantic_entries") or [])
    semantic_page = output.get("semantic_page")
    if isinstance(semantic_page, Mapping):
        semantic_page = _copy(semantic_page)
        semantic_page["entries"] = annotate(semantic_page.get("entries") or [])
        output["semantic_page"] = semantic_page
    output["selected_entries_point_to_full_records"] = True
    return output


def owner_messages(
    payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *, continuation: bool = False,
) -> list[dict[str, str]]:
    owner_payload = _owner_payload(payload, catalog, trace, continuation=continuation)
    model_payload = _model_visible_payload(owner_payload, catalog, trace, continuation=continuation)
    messages = strengthening.strengthening_messages(owner_payload, model_payload=model_payload)
    overlay = (
        "【按需材料读取合同】本轮消息同时包含已经取得的材料记录和覆盖全部材料通道的轻量目录。"
        "目录中未附完整记录表示尚未读取，不表示没有相关材料；不得把 Plus 访问计划当作科学问题清单或材料硬过滤。"
        "已读取记录的目录项含指向当前请求完整记录的 JSON pointer，使用该记录且不要重复发送候选内容。"
        "选材若只标注只读邻居，保留共享证据并按 selection_diagnostics 作为范围诊断，不自动重选。"
        "你可以在证据不足时只返回 status=needs_materials 及 material_requests，请求目录中的未初选材料或具体材料路径；"
        "如需恢复一个只读职责的完整上下文，可请求 request_type=readonly_unit 并提供合法 read_only_unit_id；不得请求可编辑任务作为只读上下文。"
        "本运行最多再进行一次本地取材续读。材料请求必须使用已有 access_id/source_handle，保留原研究身份、综述转述身份、条件和限制。"
        "材料充分时返回现有负责人合同要求的 status=updated 和完整可编辑计划，或合理 status=no_change；不要返回正文。"
    )
    if continuation:
        overlay += " 本次是唯一允许的续读回合，必须直接返回最终 updated 或 no_change，不再请求材料。"
    return [
        {**messages[0], "content": str(messages[0].get("content") or "") + "\n\n" + overlay},
        {**messages[1], "content": str(messages[1].get("content") or "") + "\n\n" + overlay},
    ]


def _access_record(messages: Sequence[Mapping[str, Any]], parsed: Mapping[str, Any], raw: Mapping[str, Any], telemetry: Mapping[str, Any], *, errors: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "request_sha256": _hash(messages),
        "messages": _copy(messages),
        "parsed_response": _copy(parsed),
        "raw_response": _copy(raw),
        "telemetry": _copy(telemetry),
        "validation_errors": list(errors),
    }


def _checkpoint_signature(
    messages: Sequence[Mapping[str, Any]], *, payload: Mapping[str, Any], catalog: Mapping[str, Any],
    model: str, thinking_budget: int, max_output_tokens: int, profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_sha256": _hash(messages),
        "payload_sha256": _hash(payload),
        "catalog_sha256": str(catalog.get("catalog_sha256") or ""),
        "model": str(model),
        "thinking_budget": int(thinking_budget),
        "max_output_tokens": int(max_output_tokens),
        "profile": _copy(profile or {}),
    }


def _semantic_signature(signature: Mapping[str, Any]) -> dict[str, Any]:
    """Transport changes do not invalidate a completed scientific result.

    Keep old signatures readable, including the old full-profile shape. The
    execution policy is separate evidence and is never rewritten on a hit.
    """
    semantic = _copy(signature)
    profile = semantic.get("profile")
    if isinstance(profile, dict):
        for key in ("timeout_seconds", "stream", "stream_overall_timeout_seconds"):
            profile.pop(key, None)
    return semantic


def _call_policy(client: Any, *, model: str, thinking_budget: int, max_output_tokens: int,
                 profile: Mapping[str, Any] | None, stream: bool,
                 timeout_seconds: float | None, overall_timeout: float | None) -> dict[str, Any]:
    inner = getattr(client, "inner", client)
    timeout = timeout_seconds if timeout_seconds is not None else getattr(inner, "timeout_seconds", None)
    overall = overall_timeout if overall_timeout is not None else getattr(inner, "stream_overall_timeout_seconds", None)
    timeout = max(5.0, float(timeout)) if timeout is not None else None
    overall = max(timeout or 0, float(overall)) if overall is not None else None
    return {
        "model": model, "thinking": True, "thinking_budget": int(thinking_budget),
        "max_output_tokens": int(max_output_tokens),
        "json_mode": getattr(inner, "json_mode", (profile or {}).get("json_mode")),
        "stream": bool(stream), "timeout_seconds": timeout,
        "stream_overall_timeout_seconds": overall if stream else None,
    }


def _execution_record(raw: Mapping[str, Any], requested: Mapping[str, Any] | None = None) -> dict[str, Any]:
    # Observed wire metadata wins over wrapper/client defaults. Legacy caches
    # without these fields stay usable but their missing policy stays unknown.
    policy = _copy(requested or {})
    effective = raw.get("effective_request") or {}
    for source, target in (("model", "model"), ("enable_thinking", "thinking"),
                           ("thinking_budget", "thinking_budget"), ("answer_tokens", "max_output_tokens")):
        if source in effective:
            policy[target] = effective[source]
    if effective:
        policy["stream"] = bool(effective.get("stream", False))
        policy["json_mode"] = bool(effective.get("response_format"))
    observed = False
    for event in raw.get("transport_events") or []:
        if event.get("stage") == "request_open_start":
            policy.update(stream=bool(event.get("stream")), timeout_seconds=event.get("timeout_seconds"),
                          stream_overall_timeout_seconds=event.get("overall_timeout_seconds"))
            observed = True
            break
    return {"policy": policy, "policy_sha256": _hash(policy),
            "source": "observed_transport" if observed else "invocation_policy" if requested is not None else "legacy_response_only"}


def _execution_provenance(requested: Mapping[str, Any], executed: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
    policy = executed.get("policy") or {}
    known = set(requested).issubset(policy) and all(value is not None or key == "stream_overall_timeout_seconds"
                                                    for key, value in policy.items())
    matches = policy == dict(requested) if known else None
    return {"requested_policy": _copy(requested), "original_execution": _copy(executed),
            "reused": reused, "physical_call_this_run": not reused,
            "requested_policy_exercised_this_run": not reused and matches is True,
            "matches_requested_policy": matches,
            "reuse_policy": "semantic_result_preserving_original_execution"}


def _checkpoint_path(stage_dir: str | Path | None, name: str) -> Path | None:
    if stage_dir is None:
        return None
    path = Path(stage_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_checkpoint(path: Path | None, value: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    # Keep the last accepted owner result available if a new attempt fails.
    if isinstance(value.get("result"), Mapping) and value["result"].get("status") in {"updated", "no_change"}:
        _atomic_checkpoint_write(path.with_name(path.stem + ".last_valid.json"), encoded)
    _atomic_checkpoint_write(path, encoded)


def _atomic_checkpoint_write(path: Path, encoded: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_compatible_checkpoint(path: Path | None, signature: Mapping[str, Any], *, resume: bool) -> dict[str, Any] | None:
    if path is None or not resume:
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            saved = json.loads(path.with_name(path.stem + ".last_valid.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(saved, dict) and _semantic_signature(saved.get("signature") or {}) == _semantic_signature(signature):
            return saved
        return None
    if not isinstance(saved, dict):
        return None
    if _semantic_signature(saved.get("signature") or {}) == _semantic_signature(signature):
        return saved
    history = path.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    identity = _hash(saved.get("signature") or {"path": str(path), "bytes": path.stat().st_size})[:16]
    preserved = history / f"{path.stem}.{identity}{path.suffix}"
    if not preserved.exists():
        preserved.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return None


def _archive_rejected_checkpoints(paths: Sequence[Path | None], signature: Mapping[str, Any]) -> None:
    """Retire only this validated rejection, keeping all paid evidence.

    Called once before an explicitly requested retry, never after an exception
    or merely because a RAW checkpoint exists. Copy every file before removal.
    """
    archived = []
    for path in paths:
        if path is None or not path.is_file():
            continue
        saved = json.loads(path.read_text(encoding="utf-8"))
        if _semantic_signature(saved.get("signature") or {}) != _semantic_signature(signature):
            continue
        history = path.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        destination = history / f"{path.stem}.rejected-{_hash(saved)[:16]}{path.suffix}"
        _atomic_checkpoint_write(destination, json.dumps(saved, ensure_ascii=False, indent=2, default=str) + "\n")
        archived.append(path)
    for path in archived:
        path.unlink()


def _needs_materials(parsed: Mapping[str, Any]) -> bool:
    return str(parsed.get("status") or "").casefold() in {"needs_materials", "needs_more_materials", "needs_material"}


def _run_complete_material_batches(
    payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *,
    access_state: Mapping[str, Any], counter: Any, capacity: int, **run_kwargs: Any,
) -> dict[str, Any]:
    """Only over-capacity reads use whole research-record batches.

    Reuse the planner's weighted complete-record partitioner. Each accepted
    local plan becomes the next pass's starting plan; no separate final merge
    model is needed. Normal requests never enter this path.
    """
    from .module4 import runtime
    rows = list(trace.get("selected_materials") or [])
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("canonical_access_id") or row.get("access_id"))
        groups.setdefault(key, {"identity": key, "records": []})["records"].append(_copy(row))
    research_records = list(groups.values())
    if not research_records:
        return {"status": "capacity_blocked", "updated_plan": None,
                "structural_errors": ["task_context_requires_smaller_editable_cluster"]}
    if counter is None:
        counter = lambda raw, messages: runtime.estimate_prompt_tokens(
            raw or json.dumps(messages, ensure_ascii=False).encode(), messages,
        )["prompt_tokens"]
    weights = [planning._chapter_details_weight(counter, row) for row in research_records]
    profile = run_kwargs["owner_profile"]
    stream = bool(run_kwargs.get("owner_stream"))

    def sliced(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        result = _copy(trace)
        result["selected_materials"] = [_copy(row) for item in items for row in item["records"]]
        result["resolved_count"] = len(result["selected_materials"])
        return result

    partitions = None
    # Bounded search over complete-record partitions, not recursive clipping.
    # A little space also accommodates the updated local plan in later passes.
    for count in range(2, len(research_records) + 1):
        candidate = planning._chapter_details_partitions(research_records, weights, count)
        if all(strengthening.estimate_strengthening_request(
            owner_messages(payload, catalog, sliced(part)), profile=profile,
            token_counter=counter, stream=stream,
        )["total_context_tokens"] <= int(capacity * 0.9) for part in candidate):
            partitions = candidate
            break
    if partitions is None:
        return {"status": "capacity_blocked", "updated_plan": None,
                "structural_errors": ["complete_record_or_task_context_requires_smaller_cluster"],
                "material_records_preserved": len(rows), "context_limit_tokens": capacity}
    current = _copy(payload)
    results = []
    changed = False
    base_dir = run_kwargs.get("checkpoint_dir")
    base_call = run_kwargs.get("owner_call_id") or str(payload.get("call_id") or "outline-on-demand") + ":owner"
    for index, part in enumerate(partitions):
        partial_trace = sliced(part)
        # All other records stay locally readable, not silently discarded.
        state = {**_copy(access_state), "trace": partial_trace}
        kwargs = dict(run_kwargs)
        kwargs.update(
            owner_call_id=base_call + f":material-batch-{index + 1}",
            checkpoint_dir=(Path(base_dir) / f"material_batch_{index + 1:03d}") if base_dir else None,
            owner_token_counter=counter, owner_context_limit_tokens=capacity,
            _resolved_access_state=state, _allow_material_batches=False,
        )
        result = run_on_demand_strengthening(current, **kwargs)
        results.append({"batch": index + 1, "access_ids": [row["access_id"] for row in partial_trace["selected_materials"]],
                        "result": _copy(result)})
        if result.get("status") not in {"updated", "no_change"}:
            return {**result, "last_valid_plan": _copy(current["chapter_plan"]),
                    "last_valid_source_materials": _copy(current.get("source_materials") or []),
                    "material_batch_results": results, "material_batch_count": len(partitions)}
        # Adoption is cumulative just like the plan: later passes can retain or
        # stop citing a newly adopted study without losing its supplied material.
        # The accepted pool already includes the previous authoritative rows.
        current["source_materials"] = _copy(result.get("accepted_source_materials")
                                            or current.get("source_materials") or [])
        if result.get("status") == "updated":
            changed = True
            current["chapter_plan"] = _copy(result["updated_plan"])
            # Split/merge IDs require the caller's explicit remap integration.
            ids = [str(unit.get("unit_id") or unit.get("id")) for unit in current["chapter_plan"].get("units") or []]
            if set(ids) != set(current.get("modifiable_unit_ids") or []):
                return {"status": "partial", "last_valid_plan": current["chapter_plan"],
                        "last_valid_source_materials": _copy(current["source_materials"]),
                        "structural_errors": ["batch_unit_id_remap_requires_integration"],
                        "material_batch_results": results}
        current["input_integrity"] = strengthening._input_integrity(current)
        _write_checkpoint(_checkpoint_path(base_dir, "MATERIAL_BATCH_PROGRESS.json"), {
            "catalog_sha256": catalog.get("catalog_sha256"), "completed_batches": index + 1,
            "batch_count": len(partitions), "latest_plan": current["chapter_plan"],
            "accepted_source_materials": current["source_materials"],
            "material_record_sha256s": [_hash(row) for row in rows],
        })
    final = dict(results[-1]["result"])
    final.update(status="updated" if changed else "no_change",
                 updated_plan=_copy(current["chapter_plan"]),
                 accepted_source_materials=_copy(current.get("source_materials") or []),
                 material_batch_results=results, material_batch_count=len(partitions))
    final.setdefault("on_demand_strengthening", {})["material_access_trace"] = _copy(trace)
    return final


def run_on_demand_strengthening(
    payload: Mapping[str, Any], *,
    access_client: Any, access_model: str, access_thinking_budget: int, access_max_output_tokens: int,
    owner_client: Any, owner_model: str, owner_thinking_budget: int, owner_max_output_tokens: int,
    access_call_id: str | None = None, owner_call_id: str | None = None,
    checkpoint_dir: str | Path | None = None, resume: bool = False,
    retry_unresolved: bool = False,
    access_profile: Mapping[str, Any] | None = None, owner_profile: Mapping[str, Any] | None = None,
    access_stream: bool = False, access_stream_overall_timeout_seconds: float | None = None,
    access_timeout_seconds: float | None = None,
    access_transport_observer: Any | None = None,
    owner_stream: bool = False, owner_stream_overall_timeout_seconds: float | None = None,
    owner_timeout_seconds: float | None = None,
    owner_transport_observer: Any | None = None,
    owner_token_counter: Any | None = None, owner_context_limit_tokens: int | None = None,
    _resolved_access_state: Mapping[str, Any] | None = None,
    _allow_material_batches: bool = True,
) -> dict[str, Any]:
    """Run Plus access planning, local resolution, and one Max owner call.

    A Max response may request one validated continuation read.  No caller
    supplied issue list or answer enters either model request.
    """

    strengthening._verify_envelope(payload)
    catalog = build_material_catalog(payload)
    access_request = access_messages(payload, catalog)
    from .module4 import runtime
    access_policy = _call_policy(
        access_client, model=access_model, thinking_budget=access_thinking_budget,
        max_output_tokens=access_max_output_tokens, profile=access_profile, stream=access_stream,
        timeout_seconds=access_timeout_seconds, overall_timeout=access_stream_overall_timeout_seconds,
    )
    owner_policy = _call_policy(
        owner_client, model=owner_model, thinking_budget=owner_thinking_budget,
        max_output_tokens=owner_max_output_tokens, profile=owner_profile, stream=owner_stream,
        timeout_seconds=owner_timeout_seconds, overall_timeout=owner_stream_overall_timeout_seconds,
    )
    if _resolved_access_state is not None:
        access_request = _copy(_resolved_access_state["access_request"])
        raw_access = _copy(_resolved_access_state["raw_access"])
        access_parsed = _copy(_resolved_access_state["access_parsed"])
        access_telemetry = _copy(_resolved_access_state["access_telemetry"])
        access_execution = _copy(_resolved_access_state.get("access_execution") or _execution_record(raw_access))
        access_reused = True
        access_raw_reused = False
        trace = _copy(_resolved_access_state["trace"])
    else:
        access_signature = _checkpoint_signature(
            access_request, payload=payload, catalog=catalog, model=access_model,
            thinking_budget=access_thinking_budget, max_output_tokens=access_max_output_tokens,
            profile=access_profile,
        )
        access_stage_path = _checkpoint_path(checkpoint_dir, "ACCESS_STAGE.json")
        access_stage = _load_compatible_checkpoint(access_stage_path, access_signature, resume=resume)
        access_reused = access_stage is not None
        access_raw_reused = False
        if access_reused:
            raw_access = access_stage.get("raw_response") or {}
            access_parsed = access_stage.get("parsed_response") or {}
            access_telemetry = access_stage.get("telemetry") or {}
            access_execution = access_stage.get("execution") or _execution_record(raw_access)
        else:
            access_raw_path = _checkpoint_path(checkpoint_dir, "ACCESS_RAW_STAGE.json")
            raw_stage = _load_compatible_checkpoint(access_raw_path, access_signature, resume=resume)
            if raw_stage is not None:
                raw_access = raw_stage.get("raw_response") or {}
                access_execution = raw_stage.get("execution") or _execution_record(raw_access)
                access_raw_reused = True
            else:
                access_invoke_kwargs = {
                    "model": access_model,
                    "max_output_tokens": int(access_max_output_tokens),
                    "thinking": True,
                    "thinking_budget": int(access_thinking_budget),
                    "call_id": access_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":access"),
                }
                if access_stream:
                    access_invoke_kwargs["stream"] = True
                if access_stream_overall_timeout_seconds is not None:
                    access_invoke_kwargs["stream_overall_timeout_seconds"] = float(access_stream_overall_timeout_seconds)
                if access_timeout_seconds is not None:
                    access_invoke_kwargs["timeout_seconds"] = float(access_timeout_seconds)
                if access_transport_observer is not None:
                    access_invoke_kwargs["transport_observer"] = access_transport_observer
                raw_access = runtime.invoke_client(access_client, access_request, **access_invoke_kwargs)
                access_execution = _execution_record(raw_access, access_policy)
                _write_checkpoint(access_raw_path, {
                    "signature": access_signature, "raw_response": _copy(raw_access),
                    "execution": access_execution,
                })
            access_parsed, access_telemetry = planning._parse_planner_response(raw_access)
            _write_checkpoint(access_stage_path, {
                "signature": access_signature, "raw_response": _copy(raw_access),
                "parsed_response": _copy(access_parsed), "telemetry": _copy(access_telemetry),
                "execution": access_execution,
            })
        try:
            access_validated = validate_access_response(access_parsed, catalog)
        except OnDemandMaterialError as exc:
            record = _access_record(access_request, access_parsed, raw_access, access_telemetry, errors=[str(exc)])
            return {
                "status": "unresolved",
                "chapter_id": str(payload.get("chapter_id") or ""),
                "updated_plan": None,
                "unit_id_remap": {},
                "structural_errors": ["access_plan_not_accepted", str(exc)],
                "accepted_source_materials": _copy(payload.get("source_materials") or []),
                "request_sha256": _hash(access_request),
                "parsed_response": access_parsed,
                "telemetry": access_telemetry,
                "raw_response": raw_access,
                "messages": access_request,
                "on_demand_strengthening": {
                    "access": record,
                    "owner_not_called": True,
                    "access_reused": access_reused,
                    "access_raw_reused": access_raw_reused,
                    "execution_provenance": {"access": _execution_provenance(
                        access_policy, access_execution, reused=access_reused or access_raw_reused)},
                    "catalog": public_catalog(catalog),
                },
            }
        trace = resolve_material_requests(payload, catalog, access_validated)
    final_read = bool((_resolved_access_state or {}).get("final_read"))
    owner_payload = _owner_payload(payload, catalog, trace, continuation=final_read)
    owner_request = owner_messages(payload, catalog, trace, continuation=final_read)
    counter = owner_token_counter
    if counter is None:
        counter = getattr(owner_client, "counter", None) or getattr(owner_client, "prompt_token_counter", None)
        inner = getattr(owner_client, "inner", None)
        if counter is None and inner is not None:
            counter = getattr(inner, "prompt_token_counter", None)
    capacity = int(owner_context_limit_tokens or runtime.model_pricing(owner_model)["context_window"])
    effective_profile = dict(owner_profile or {})
    effective_profile.update(model=owner_model, thinking_budget=owner_thinking_budget,
                             max_output_tokens=owner_max_output_tokens)
    estimate = strengthening.estimate_strengthening_request(
        owner_request, profile=effective_profile, token_counter=counter, stream=owner_stream,
    )
    if estimate["total_context_tokens"] > capacity:
        if not _allow_material_batches:
            return {"status": "capacity_blocked", "updated_plan": None,
                    "structural_errors": ["complete_record_or_task_context_exceeds_capacity"],
                    "capacity_estimate": estimate, "context_limit_tokens": capacity}
        batched = _run_complete_material_batches(
            payload, catalog, trace,
            access_state={"access_request": access_request, "raw_access": raw_access,
                          "access_parsed": access_parsed, "access_telemetry": access_telemetry,
                          "access_execution": access_execution,
                          "final_read": final_read},
            owner_client=owner_client, owner_model=owner_model,
            owner_thinking_budget=owner_thinking_budget, owner_max_output_tokens=owner_max_output_tokens,
            access_client=access_client, access_model=access_model,
            access_thinking_budget=access_thinking_budget, access_max_output_tokens=access_max_output_tokens,
            access_profile=access_profile, owner_profile=effective_profile,
            access_stream=access_stream, access_timeout_seconds=access_timeout_seconds,
            access_stream_overall_timeout_seconds=access_stream_overall_timeout_seconds,
            owner_call_id=owner_call_id, checkpoint_dir=checkpoint_dir, resume=resume,
            retry_unresolved=retry_unresolved,
            owner_stream=owner_stream, owner_stream_overall_timeout_seconds=owner_stream_overall_timeout_seconds,
            owner_timeout_seconds=owner_timeout_seconds,
            owner_transport_observer=owner_transport_observer, counter=counter, capacity=capacity,
        )
        metadata = batched.setdefault("on_demand_strengthening", {})
        metadata.update(access_reused=access_reused, access_raw_reused=access_raw_reused)
        batch_results = batched.get("material_batch_results") or []
        # Do not present the last batch's execution policy as the whole run.
        metadata["execution_provenance"] = {
            "access": _execution_provenance(access_policy, access_execution, reused=access_reused or access_raw_reused),
            "material_batches": [{"batch": item["batch"],
                                  "execution_provenance": (item["result"].get("on_demand_strengthening") or {}).get("execution_provenance")}
                                 for item in batch_results],
        }
        metadata["owner_call_count"] = sum((item["result"].get("on_demand_strengthening") or {}).get("owner_call_count", 0)
                                           for item in batch_results)
        return batched
    owner_signature = _checkpoint_signature(
        owner_request, payload=owner_payload, catalog=catalog, model=owner_model,
        thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
        profile=owner_profile,
    )
    owner_stage_path = _checkpoint_path(checkpoint_dir, "OWNER_STAGE.json")
    owner_raw_path = _checkpoint_path(checkpoint_dir, "OWNER_RAW_STAGE.json")
    owner_stage = _load_compatible_checkpoint(owner_stage_path, owner_signature, resume=resume)
    cached_owner = _copy((owner_stage or {}).get("result") or {})
    cached_execution = (owner_stage or {}).get("execution")
    owner_revalidated = False
    owner_revalidation_source = None
    revalidation_raw = cached_owner.get("raw_response")
    if owner_stage is not None:
        if isinstance(revalidation_raw, Mapping) and revalidation_raw:
            owner_revalidation_source = "stage_raw_response"
        else:
            saved_raw = _load_compatible_checkpoint(owner_raw_path, owner_signature, resume=resume)
            revalidation_raw = (saved_raw or {}).get("raw_response")
            if isinstance(revalidation_raw, Mapping) and revalidation_raw:
                owner_revalidation_source = "matching_raw_checkpoint"
                cached_execution = cached_execution or (saved_raw or {}).get("execution")
            elif isinstance(cached_owner.get("parsed_response"), Mapping) and cached_owner["parsed_response"]:
                # Imported parsed-only artifacts can still be revalidated. This
                # adapter is local reconstruction, not original provider bytes.
                owner_revalidation_source = "saved_parsed_response"
                revalidation_raw = {"_planner_call": True, "response": cached_owner["parsed_response"],
                                    "telemetry": cached_owner.get("telemetry") or {}}
    if owner_stage is not None and owner_revalidation_source is not None:
        # Reapply local parsing/material closure fixes without another paid
        # request. Original wire policy remains attached to the saved response.
        cached_owner = strengthening._run_owner_with_messages(
            owner_payload, messages=owner_request, client=owner_client, model=owner_model,
            thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
            raw_response=revalidation_raw,
        )
        owner_revalidated = owner_revalidation_source != "saved_parsed_response"
    if (retry_unresolved and owner_stage is not None and cached_owner.get("status") == "unresolved"
            and not _needs_materials(cached_owner.get("parsed_response") or {})):
        _archive_rejected_checkpoints((owner_stage_path, owner_raw_path), owner_signature)
        owner_stage = None
        owner_revalidated = False
        owner_revalidation_source = None
    owner_reused = owner_stage is not None
    owner_raw_reused = False
    if owner_reused:
        first_owner = cached_owner
        owner_execution = cached_execution or _execution_record(first_owner.get("raw_response") or {})
        if cached_execution is None and owner_revalidation_source == "saved_parsed_response":
            owner_execution["source"] = "reconstructed_from_saved_parsed_response"
    else:
        raw_owner_stage = _load_compatible_checkpoint(owner_raw_path, owner_signature, resume=resume)
        if raw_owner_stage is not None:
            owner_raw_reused = True
            owner_execution = raw_owner_stage.get("execution") or _execution_record(raw_owner_stage.get("raw_response") or {})
            first_owner = strengthening._run_owner_with_messages(
                owner_payload, messages=owner_request, client=owner_client, model=owner_model,
                thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
                raw_response=raw_owner_stage.get("raw_response") or {},
                call_id=owner_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":owner"),
                stream=owner_stream,
                stream_overall_timeout_seconds=owner_stream_overall_timeout_seconds,
                timeout_seconds=owner_timeout_seconds,
                transport_observer=owner_transport_observer,
            )
        else:
            def save_owner_raw(raw: Mapping[str, Any]) -> None:
                _write_checkpoint(owner_raw_path, {
                    "signature": owner_signature, "raw_response": _copy(raw),
                    "execution": _execution_record(raw, owner_policy),
                })

            first_owner = strengthening._run_owner_with_messages(
                owner_payload, messages=owner_request, client=owner_client, model=owner_model,
                thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
                raw_response_callback=save_owner_raw,
                call_id=owner_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":owner"),
                stream=owner_stream,
                stream_overall_timeout_seconds=owner_stream_overall_timeout_seconds,
                timeout_seconds=owner_timeout_seconds,
                transport_observer=owner_transport_observer,
            )
            owner_execution = _execution_record(first_owner.get("raw_response") or {}, owner_policy)
        _write_checkpoint(owner_stage_path, {"signature": owner_signature, "result": _copy(first_owner),
                                             "execution": owner_execution})
    final_owner = first_owner
    continuation = None
    continuation_reused = False
    parsed_owner = first_owner.get("parsed_response") if isinstance(first_owner.get("parsed_response"), Mapping) else {}
    if _needs_materials(parsed_owner) and not final_read:
        try:
            continuation_validated = validate_access_response(
                {"status": "partial", "material_requests": _request_rows(parsed_owner)}, catalog,
            )
            continuation_trace = resolve_material_requests(
                payload, catalog, continuation_validated, prior_access_ids=trace.get("access_ids") or [],
            )
        except OnDemandMaterialError as exc:
            first_owner.setdefault("structural_errors", []).append(f"continuation_material_request_invalid:{exc}")
            final_owner = first_owner
        else:
            combined_trace = {
                "status": "partial",
                "material_requests": [*(trace.get("material_requests") or []), *(continuation_trace.get("material_requests") or [])],
                "selected_materials": [*(trace.get("selected_materials") or []), *(continuation_trace.get("selected_materials") or [])],
                "trace": [*(trace.get("trace") or []), *(continuation_trace.get("trace") or [])],
                "resolved_count": len([*(trace.get("selected_materials") or []), *(continuation_trace.get("selected_materials") or [])]),
                "access_ids": continuation_trace.get("access_ids") or trace.get("access_ids") or [],
                "catalog_sha256": catalog.get("catalog_sha256"),
            }
            combined_trace["selection_diagnostics"] = _selection_diagnostics(
                payload, combined_trace["selected_materials"],
            )
            combined_trace["readonly_context_reads"] = [
                *(trace.get("readonly_context_reads") or []),
                *(continuation_trace.get("readonly_context_reads") or []),
            ]
            continuation = run_on_demand_strengthening(
                payload, access_client=access_client, access_model=access_model,
                access_thinking_budget=access_thinking_budget, access_max_output_tokens=access_max_output_tokens,
                owner_client=owner_client, owner_model=owner_model,
                owner_thinking_budget=owner_thinking_budget, owner_max_output_tokens=owner_max_output_tokens,
                owner_call_id=(owner_call_id or "outline-on-demand:owner") + ":continuation",
                checkpoint_dir=(Path(checkpoint_dir) / "continuation") if checkpoint_dir else None,
                resume=resume, access_profile=access_profile, owner_profile=owner_profile,
                retry_unresolved=retry_unresolved,
                access_stream=access_stream, access_timeout_seconds=access_timeout_seconds,
                access_stream_overall_timeout_seconds=access_stream_overall_timeout_seconds,
                owner_stream=owner_stream, owner_stream_overall_timeout_seconds=owner_stream_overall_timeout_seconds,
                owner_timeout_seconds=owner_timeout_seconds,
                owner_transport_observer=owner_transport_observer,
                owner_token_counter=counter, owner_context_limit_tokens=capacity,
                _resolved_access_state={"access_request": access_request, "raw_access": raw_access,
                                        "access_parsed": access_parsed, "access_telemetry": access_telemetry,
                                        "access_execution": access_execution,
                                        "trace": combined_trace, "final_read": True},
                _allow_material_batches=_allow_material_batches,
            )
            continuation_reused = bool((continuation.get("on_demand_strengthening") or {}).get("owner_reused"))
            final_owner = continuation
            trace = combined_trace
    result = dict(final_owner)
    result["on_demand_strengthening"] = {
        "catalog": public_catalog(catalog),
        "access": _access_record(access_request, access_parsed, raw_access, access_telemetry),
        "material_access_trace": _copy(trace),
        "owner_initial": _copy(first_owner),
        "owner_continuation": _copy(continuation) if continuation is not None else None,
        "access_reused": access_reused,
        "access_raw_reused": access_raw_reused,
        "owner_reused": owner_reused,
        "owner_raw_reused": owner_raw_reused,
        "owner_revalidated_from_raw": owner_revalidated,
        "owner_revalidation_source": owner_revalidation_source,
        "owner_continuation_reused": continuation_reused,
        "checkpoint_dir": str(checkpoint_dir) if checkpoint_dir is not None else None,
        "owner_call_count": 2 if continuation is not None else 1,
        "execution_provenance": {
            "access": _execution_provenance(access_policy, access_execution, reused=access_reused or access_raw_reused),
            "owner": _execution_provenance(owner_policy, owner_execution, reused=owner_reused or owner_raw_reused),
            "owner_continuation": (continuation.get("on_demand_strengthening") or {}).get("execution_provenance") if continuation is not None else None,
        },
    }
    return result


__all__ = [
    "ACCESS_STAGE", "OnDemandMaterialError", "build_material_catalog",
    "public_catalog", "navigation_catalog", "access_messages", "validate_access_response",
    "resolve_material_requests", "full_catalog_trace", "owner_messages", "run_on_demand_strengthening",
]
