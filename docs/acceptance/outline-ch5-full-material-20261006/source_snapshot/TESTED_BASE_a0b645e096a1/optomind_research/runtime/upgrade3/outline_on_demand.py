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
from collections.abc import Mapping, Sequence
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
    "deep_read_materials", "supplement_material", "supplement_materials",
    "supplement_gap_material", "supplement_gap_materials", "local_passages",
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


def access_messages(payload: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build an issue-free, material-location-only Plus request."""

    strengthening._verify_envelope(payload)
    modifiable, readonly = _unit_ids(payload)
    system = (
        "你是自主细纲的材料访问规划器，不是修订负责人。只返回 JSON："
        "status 为 access_plan、partial 或 no_change，并可带 material_requests。"
        "material_requests 只能请求当前目录中的 access_id、source_handle 或材料路径，"
        "每项可带 unit_ids 和 reason；不得返回 updated_plan、chapter_updates、issues、正文、答案或科学修订建议。"
        "目录覆盖 source_materials、candidate_materials、candidate_navigation、tool_materials，"
        "包含原研究身份和可用条件路径；综述转述材料不因没有自身 A/B 或全文而失去资格。"
        "目录按完整字段或记录展示原材料；excerpt=true 表示有内容省略，omitted_material_paths 中的材料仍可请求。"
        "未出现在本轮已取记录中表示尚未读取，不表示不存在；不要把本规划结果当成材料过滤硬限制。"
    )
    user_value = {
        "research_question": payload.get("research_question"),
        "chapter_id": payload.get("chapter_id"),
        "chapter_plan": payload.get("chapter_plan"),
        "modifiable_unit_ids": modifiable,
        "read_only_unit_ids": readonly,
        "readonly_neighbor_unit_roles": payload.get("readonly_neighbor_unit_roles") or [],
        "full_chapter_context": payload.get("full_chapter_context") or {},
        "actual_local_body": payload.get("actual_local_body") or "",
        "material_catalog": public_catalog(catalog),
        "return_contract": {
            "status_values": ["access_plan", "partial", "no_change"],
            "material_requests_only": True,
            "no_scientific_revision": True,
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
    return {"status": status, "material_requests": requests}


def _find_access_id(request: Mapping[str, Any], catalog: Mapping[str, Any]) -> str:
    lookup = catalog.get("_lookup") if isinstance(catalog, Mapping) else {}
    access_id = str(request.get("access_id") or "").strip()
    if access_id and access_id in lookup:
        return access_id
    if access_id:
        # A planner may name a concrete field or passage as
        # ``source_materials[2].deep_read_material``.  Resolve the record
        # prefix while retaining the suffix in the access trace.
        prefixes = [str(key) for key in lookup if access_id.startswith(str(key) + ".")]
        if prefixes:
            return max(prefixes, key=len)
    handle = str(request.get("source_handle") or request.get("paper_id") or "").strip()
    if not handle:
        raise OnDemandMaterialError("material_request_identity_missing")
    matches = []
    for key, value in lookup.items():
        identity = value.get("entry", {}).get("identity", {}) if isinstance(value, Mapping) else {}
        if handle in {str(identity.get("source_handle") or ""), str(identity.get("paper_id") or "")}:
            matches.append(str(key))
    if len(matches) != 1:
        raise OnDemandMaterialError("material_request_identity_ambiguous_or_missing")
    return matches[0]


def resolve_material_requests(
    payload: Mapping[str, Any], catalog: Mapping[str, Any], parsed: Mapping[str, Any],
    *, prior_access_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolve requests to original rows, retaining identity and conditions."""

    validated = validate_access_response(parsed, catalog)
    lookup = catalog["_lookup"]
    selected: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    seen = set(str(value) for value in prior_access_ids)
    for request in validated["material_requests"]:
        access_id = _find_access_id(request, catalog)
        record_info = lookup[access_id]
        if access_id in seen:
            continue
        seen.add(access_id)
        row = _copy(record_info["record"])
        entry = _copy(record_info["entry"])
        requested_paths = request.get("material_paths") or request.get("paths") or []
        raw_access_id = str(request.get("access_id") or "")
        if not requested_paths and raw_access_id.startswith(access_id + "."):
            requested_paths = [raw_access_id[len(access_id) + 1:]]
        selected.append({
            "access_id": access_id,
            "channel": entry["channel"],
            "index_path": entry["index_path"],
            "identity": entry["identity"],
            "requested_paths": _copy(requested_paths),
            "unit_ids": [str(value) for value in request.get("unit_ids") or []],
            "record": row,
        })
        trace_rows.append({
            "access_id": access_id,
            "channel": entry["channel"],
            "index_path": entry["index_path"],
            "identity": entry["identity"],
            "record_sha256": entry["record_sha256"],
            "requested_paths": _copy(requested_paths),
            "unit_ids": [str(value) for value in request.get("unit_ids") or []],
        })
    return {
        "status": validated["status"],
        "material_requests": _copy(validated["material_requests"]),
        "selected_materials": selected,
        "trace": trace_rows,
        "resolved_count": len(selected),
        "access_ids": sorted(seen),
        "catalog_sha256": catalog.get("catalog_sha256"),
    }


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
        "unread_materials_remain_requestable": True,
        "continuation": bool(continuation),
    }
    result["autonomous_outline_strengthening"] = {
        **dict(result.get("autonomous_outline_strengthening") or {}),
        "input_mode": "on_demand_material_access",
    }
    return result


def _model_visible_payload(full_payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *, continuation: bool = False) -> dict[str, Any]:
    """Project only locally resolved material records into the model request.

    The full payload remains local for response validation and arrangement.  A
    model sees the current plan, identities, a complete lightweight catalog,
    and the records actually resolved for this pass; unrequested full records
    are not silently transmitted as if they had been read.
    """

    visible = _copy(full_payload)
    selected = [row for row in trace.get("selected_materials") or [] if isinstance(row, Mapping)]
    source_rows = []
    candidate_rows = []
    tool_rows = []
    for item in selected:
        record = item.get("record")
        if not isinstance(record, Mapping):
            continue
        channel = str(item.get("channel") or "")
        if channel == "source_materials":
            source_rows.append(_copy(record))
        elif channel in {"candidate_materials", "candidate_navigation"}:
            candidate_rows.append(_copy(record))
        elif channel == "tool_materials":
            tool_rows.append(_copy(record))
    visible["source_materials"] = source_rows
    visible["candidate_materials"] = candidate_rows
    visible["tool_materials"] = tool_rows
    navigation = full_payload.get("candidate_navigation")
    nav_shell = {}
    if isinstance(navigation, Mapping):
        for key in ("access_contract", "chapter_id", "query", "candidate_count", "returned_count", "omitted_candidate_count", "unassigned_relevant_count"):
            if key in navigation:
                nav_shell[key] = _copy(navigation[key])
    nav_shell["candidate_materials"] = candidate_rows
    visible["candidate_navigation"] = nav_shell
    visible_access = _copy(visible.get("on_demand_material_access") or {})
    visible_access["resolved_materials"] = [
        {
            key: _copy(item[key])
            for key in ("access_id", "channel", "index_path", "identity", "requested_paths", "unit_ids")
            if key in item
        }
        for item in selected
    ]
    visible_access["trace"] = _copy(trace.get("trace") or [])
    visible_access["continuation"] = bool(continuation)
    visible["on_demand_material_access"] = visible_access
    return visible


def owner_messages(
    payload: Mapping[str, Any], catalog: Mapping[str, Any], trace: Mapping[str, Any], *, continuation: bool = False,
) -> list[dict[str, str]]:
    owner_payload = _owner_payload(payload, catalog, trace, continuation=continuation)
    model_payload = _model_visible_payload(owner_payload, catalog, trace, continuation=continuation)
    messages = strengthening.strengthening_messages(owner_payload, model_payload=model_payload)
    overlay = (
        "【按需材料读取合同】本轮消息同时包含已经取得的材料记录和覆盖全部材料通道的轻量目录。"
        "目录中未附完整记录表示尚未读取，不表示没有相关材料；不得把 Plus 访问计划当作科学问题清单或材料硬过滤。"
        "你可以在证据不足时只返回 status=needs_materials 及 material_requests，请求目录中的未初选材料或具体材料路径；"
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


def _needs_materials(parsed: Mapping[str, Any]) -> bool:
    return str(parsed.get("status") or "").casefold() in {"needs_materials", "needs_more_materials", "needs_material"}


def run_on_demand_strengthening(
    payload: Mapping[str, Any], *,
    access_client: Any, access_model: str, access_thinking_budget: int, access_max_output_tokens: int,
    owner_client: Any, owner_model: str, owner_thinking_budget: int, owner_max_output_tokens: int,
    access_call_id: str | None = None, owner_call_id: str | None = None,
) -> dict[str, Any]:
    """Run Plus access planning, local resolution, and one Max owner call.

    A Max response may request one validated continuation read.  No caller
    supplied issue list or answer enters either model request.
    """

    strengthening._verify_envelope(payload)
    catalog = build_material_catalog(payload)
    access_request = access_messages(payload, catalog)
    from .module4 import runtime
    raw_access = runtime.invoke_client(
        access_client, access_request, model=access_model,
        max_output_tokens=int(access_max_output_tokens), thinking=True,
        thinking_budget=int(access_thinking_budget),
        call_id=access_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":access"),
    )
    access_parsed, access_telemetry = planning._parse_planner_response(raw_access)
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
                "catalog": public_catalog(catalog),
            },
        }
    trace = resolve_material_requests(payload, catalog, access_validated)
    owner_payload = _owner_payload(payload, catalog, trace)
    owner_request = owner_messages(payload, catalog, trace)
    first_owner = strengthening._run_owner_with_messages(
        owner_payload, messages=owner_request, client=owner_client, model=owner_model,
        thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
        call_id=owner_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":owner"),
    )
    final_owner = first_owner
    continuation = None
    parsed_owner = first_owner.get("parsed_response") if isinstance(first_owner.get("parsed_response"), Mapping) else {}
    if _needs_materials(parsed_owner):
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
            continuation_payload = _owner_payload(payload, catalog, combined_trace, continuation=True)
            continuation_request = owner_messages(payload, catalog, combined_trace, continuation=True)
            continuation = strengthening._run_owner_with_messages(
                continuation_payload, messages=continuation_request, client=owner_client, model=owner_model,
                thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
                call_id=(owner_call_id or (str(payload.get("call_id") or "outline-on-demand") + ":owner")) + ":continuation",
            )
            final_owner = continuation
            trace = combined_trace
    result = dict(final_owner)
    result["on_demand_strengthening"] = {
        "catalog": public_catalog(catalog),
        "access": _access_record(access_request, access_parsed, raw_access, access_telemetry),
        "material_access_trace": _copy(trace),
        "owner_initial": _copy(first_owner),
        "owner_continuation": _copy(continuation) if continuation is not None else None,
        "owner_call_count": 2 if continuation is not None else 1,
    }
    return result


__all__ = [
    "ACCESS_STAGE", "OnDemandMaterialError", "build_material_catalog",
    "public_catalog", "access_messages", "validate_access_response",
    "resolve_material_requests", "full_catalog_trace", "owner_messages", "run_on_demand_strengthening",
]
