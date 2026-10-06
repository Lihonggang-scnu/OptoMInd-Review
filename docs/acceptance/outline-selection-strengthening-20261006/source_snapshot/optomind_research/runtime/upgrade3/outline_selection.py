"""Autonomous unit selection for opt-in outline strengthening.

This layer chooses worthwhile outline work and groups it.  It never rewrites a
plan or supplies scientific answers.  The existing on-demand owner path then
receives one chapter-local projection for each selected group/chapter pair.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import outline_strengthening as strengthening
from . import progressive_review_plan as planning


SELECTION_SCHEMA = "optomind.outline_unit_selection.v1"
SELECTION_ROLE = "outline_selection"
_FORBIDDEN_TOP_LEVEL = frozenset({
    "review_targets", "expected_answers", "prior_candidate", "edited_body",
    "replacement_text", "chapter_feedback", "editorial_feedback_for_chapter",
})
_NO_SELECTION_STATUSES = frozenset({"none", "no_change", "unchanged", "no-change"})
_IDENTITY_FIELDS = ("source_handle", "paper_id", "original_paper_id", "doi", "title", "year", "study_id")
_MODEL_STRIP_KEYS = frozenset({"card_path", "excluded_source_ids", "excluded_source_handles", "source_exclusion_notes"})


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _unit_id(unit: Mapping[str, Any]) -> str:
    return str(unit.get("unit_id") or unit.get("id") or "").strip()


def _chapter_id(row: Mapping[str, Any]) -> str:
    return str(row.get("chapter_id") or row.get("chapter", {}).get("chapter_id") or "").strip()


def _unit_rows(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    plan = chapter.get("chapter_plan") if isinstance(chapter.get("chapter_plan"), Mapping) else {}
    rows = plan.get("units") if isinstance(plan, Mapping) else []
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def _material_index(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose identity and readable locators without duplicating full material cards."""

    channels = {}
    for channel in ("source_materials", "candidate_materials", "tool_materials"):
        rows = []
        for row in payload.get(channel) or []:
            if not isinstance(row, Mapping):
                continue
            identity_keys = (
                "source_handle", "paper_id", "original_paper_id", "original_source_handle",
                "title", "doi", "study_id",
            )
            item = {key: _copy(row[key]) for key in identity_keys if key in row}
            paths = row.get("available_material_paths") or row.get("material_paths") or row.get("available_paths")
            if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
                item["available_material_paths"] = [str(path) for path in paths]
            elif isinstance(row.get("source_supplied_locators"), Sequence):
                item["available_material_paths"] = [
                    str(locator.get("path")) for locator in row.get("source_supplied_locators") or []
                    if isinstance(locator, Mapping) and str(locator.get("path") or "").strip()
                ]
            for key in ("research_scope", "topic", "study_type", "source_description"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value
            item["full_record_available_for_owner"] = True
            rows.append(item)
        channels[channel] = rows
    return channels


def build_selection_payload(
    chapter_payloads: Sequence[Mapping[str, Any]], *,
    research_question: str = "", shared_outline: Any = None, shared_scope: Mapping[str, Any] | None = None,
    review_argument: Any = "", review_argument_status: str = "", review_argument_source: str = "",
    actual_local_body_by_chapter: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a complete outline-only selector input from genuine chapter packets.

    Chapter plans are copied in full.  Body text is optional and, when supplied,
    is copied in full per chapter; no character-prefix projection is used.
    Full source records remain in the chapter payloads for the downstream owner
    projection and are represented here by identity/planning locators only.
    """

    chapters = []
    seen_units: set[str] = set()
    for raw in chapter_payloads:
        if not isinstance(raw, Mapping):
            raise ValueError("selection_chapter_payload_must_be_object")
        chapter_id = _chapter_id(raw)
        if not chapter_id:
            raise ValueError("selection_chapter_id_required")
        rows = _unit_rows(raw)
        if not rows:
            raise ValueError(f"selection_chapter_units_missing:{chapter_id}")
        current_ids = [_unit_id(row) for row in rows]
        if any(not value for value in current_ids):
            raise ValueError(f"selection_unit_id_missing:{chapter_id}")
        overlap = seen_units & set(current_ids)
        if overlap:
            raise ValueError("selection_unit_id_duplicate:" + ",".join(sorted(overlap)))
        seen_units.update(current_ids)
        context = _copy(raw.get("full_chapter_context") or {})
        chapter = _copy(raw.get("chapter") or {})
        body_map = actual_local_body_by_chapter or {}
        body = body_map.get(chapter_id, raw.get("actual_local_body") or "")
        chapters.append({
            "chapter_id": chapter_id,
            "chapter": chapter,
            "chapter_plan": _copy(raw.get("chapter_plan") or {}),
            "modifiable_unit_ids": _ids(raw.get("modifiable_unit_ids") or current_ids),
            "read_only_unit_ids": _ids(raw.get("read_only_unit_ids") or []),
            "readonly_neighbor_unit_roles": _copy(raw.get("readonly_neighbor_unit_roles") or []),
            "full_chapter_context": context,
            "actual_local_body": str(body or ""),
            "material_index": _material_index(raw),
            "source_identity_map": _copy(raw.get("source_identity_map") or {}),
        })
    if not chapters:
        raise ValueError("selection_chapters_required")
    declared_forbidden = sorted({
        unit_id
        for chapter in chapters
        for unit_id in (
            (
                set(_ids(chapter.get("read_only_unit_ids") or [])) - seen_units
            )
            | (
                set(_unit_id(row) for row in _unit_rows(chapter))
                - set(_ids(chapter.get("modifiable_unit_ids") or []))
            )
        )
    })
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "selection_mode": {
            "enabled": True,
            "input_mode": "autonomous_from_full_outline",
            "allow_none": True,
            "allow_cross_chapter_groups": True,
            "rewrite_plan": False,
            "do_not_add_evidence": True,
        },
        "research_question": str(research_question or ""),
        "shared_outline": _copy(shared_outline or []),
        "shared_scope": _copy(shared_scope or {}),
        "review_argument": _copy(review_argument),
        "review_argument_status": str(review_argument_status or ""),
        "review_argument_source": str(review_argument_source or ""),
        "chapters": chapters,
        "all_unit_ids": sorted(seen_units),
        "selector_forbidden_unit_ids": declared_forbidden,
        "call_id": "outline-unit-selection",
    }
    _verify_selection_payload(payload)
    return payload


def _identity_key(value: Mapping[str, Any]) -> str:
    """Use all available identity fields so same handles with different papers stay separate."""

    fields = {key: str(value.get(key) or "").strip() for key in _IDENTITY_FIELDS}
    fields = {key: item for key, item in fields.items() if item}
    if not fields:
        fields = {"serialized_identity": json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}
    return _hash(fields)


def _identity_entry(catalog: dict[str, dict[str, Any]], value: Mapping[str, Any]) -> dict[str, Any]:
    key = _identity_key(value)
    entry = catalog.setdefault(key, {
        "identity_key": key,
        "source_handles": [],
        "paper_ids": [],
        "titles": [],
        "dois": [],
        "years": [],
        "local_position_ids": [],
        "locations": [],
        "full_record_available_for_owner": True,
    })
    for field, output in (("source_handle", "source_handles"), ("paper_id", "paper_ids"),
                          ("original_paper_id", "paper_ids"), ("title", "titles"),
                          ("doi", "dois"), ("year", "years")):
        item = str(value.get(field) or "").strip()
        if item and item not in entry[output]:
            entry[output].append(item)
    return entry


def _compact_runtime_metadata(value: Any) -> Any:
    """Remove only transport/runtime metadata outside the lossless plans."""

    if isinstance(value, Mapping):
        return {
            str(key): _compact_runtime_metadata(child)
            for key, child in value.items()
            if str(key) not in _MODEL_STRIP_KEYS
        }
    if isinstance(value, list):
        return [_compact_runtime_metadata(child) for child in value]
    return _copy(value)


def model_visible_selection_payload(
    payload: Mapping[str, Any], *, include_material_index: bool = False,
) -> dict[str, Any]:
    """Project a compact selector view without truncating any chapter plan.

    The complete payload remains the local source of truth for owner requests.
    This view removes absolute card paths, exclusion inventories, repeated
    neighbour prose, and per-chapter duplicate identity maps.  It retains a
    shared identity catalog with stable local position IDs and JSON pointers.
    """

    _verify_selection_payload(payload)
    catalog: dict[str, dict[str, Any]] = {}
    chapter_material_keys: dict[str, set[str]] = {}
    original_chapters = payload.get("chapters") or []
    if include_material_index:
        for chapter in original_chapters:
            chapter_id = _chapter_id(chapter)
            material_keys: set[str] = set()
            for handle, raw in (chapter.get("source_identity_map") or {}).items():
                if not isinstance(raw, Mapping):
                    continue
                identity = dict(raw)
                identity.setdefault("source_handle", str(handle))
                entry = _identity_entry(catalog, identity)
                material_keys.add(entry["identity_key"])
                local_id = f"{chapter_id}:identity:{str(handle)}"
                if local_id not in entry["local_position_ids"]:
                    entry["local_position_ids"].append(local_id)
                location = {"chapter_id": chapter_id, "channel": "source_identity_map", "local_position_id": local_id}
                if location not in entry["locations"]:
                    entry["locations"].append(location)
            for channel, rows in (chapter.get("material_index") or {}).items():
                for index, raw in enumerate(rows or []):
                    if not isinstance(raw, Mapping):
                        continue
                    entry = _identity_entry(catalog, raw)
                    material_keys.add(entry["identity_key"])
                    local_id = f"{chapter_id}:{channel}:{index}"
                    if local_id not in entry["local_position_ids"]:
                        entry["local_position_ids"].append(local_id)
                    location = {"chapter_id": chapter_id, "channel": str(channel), "record_index": index, "local_position_id": local_id}
                    if location not in entry["locations"]:
                        entry["locations"].append(location)
            chapter_material_keys[chapter_id] = material_keys

    material_catalog = []
    catalog_index: dict[str, int] = {}
    for key in sorted(catalog):
        entry = _copy(catalog[key])
        for field in ("source_handles", "paper_ids", "titles", "dois", "years", "local_position_ids"):
            entry[field] = sorted(set(entry[field]))
        # The compact local IDs retain a readable lookup route.  Repeating a
        # full location object for every chapter/channel adds no selector
        # information and needlessly recreates the source directory.
        entry["location_count"] = len(entry.pop("locations", []))
        catalog_index[key] = len(material_catalog)
        material_catalog.append(entry)

    chapters = []
    for chapter in original_chapters:
        chapter_id = _chapter_id(chapter)
        chapter_raw = _compact_runtime_metadata(chapter.get("chapter") or {})
        chapter_view = {
            key: _copy(chapter_raw[key])
            for key in ("chapter_id", "title", "purpose", "scope", "coordinated_scope", "reader_objective")
            if key in chapter_raw
        }
        role_ids = _ids([
            row.get("unit_id") for row in (chapter.get("readonly_neighbor_unit_roles") or [])
            if isinstance(row, Mapping)
        ])
        role_chapters = sorted({str(row.get("chapter_id") or "") for row in (chapter.get("readonly_neighbor_unit_roles") or []) if isinstance(row, Mapping) and str(row.get("chapter_id") or "").strip()})
        chapters.append({
            "chapter_id": chapter_id,
            "chapter": chapter_view,
            # Exact equality is intentional: this layer must not trim science
            # or writing duties from any unit.
            "chapter_plan": _copy(chapter.get("chapter_plan") or {}),
            "modifiable_unit_ids": _ids(chapter.get("modifiable_unit_ids") or []),
            "read_only_unit_ids": _ids(chapter.get("read_only_unit_ids") or []),
            "readonly_neighbor_unit_ids": role_ids,
            "readonly_neighbor_chapter_ids": role_chapters,
            "full_chapter_context": _compact_runtime_metadata(chapter.get("full_chapter_context") or {}),
            "actual_local_body": str(chapter.get("actual_local_body") or ""),
            "material_identity_refs": [f"#/material_identity_catalog/{catalog_index[key]}" for key in sorted(chapter_material_keys.get(chapter_id) or [])],
        })
    projected = {
        "schema_version": SELECTION_SCHEMA,
        "selection_mode": _copy(payload.get("selection_mode") or {}),
        "research_question": str(payload.get("research_question") or ""),
        "shared_outline": _compact_runtime_metadata(payload.get("shared_outline") or []),
        "shared_scope": _compact_runtime_metadata(payload.get("shared_scope") or {}),
        "review_argument": _copy(payload.get("review_argument") or ""),
        "review_argument_status": str(payload.get("review_argument_status") or ""),
        "review_argument_source": str(payload.get("review_argument_source") or ""),
        "chapters": chapters,
        "all_unit_ids": _ids(payload.get("all_unit_ids") or []),
        "selector_forbidden_unit_ids": _ids(payload.get("selector_forbidden_unit_ids") or []),
        "call_id": str(payload.get("call_id") or "outline-unit-selection"),
        "model_visible_projection": {
            "source_payload_preserved_locally": True,
            "chapter_plan_lossless": True,
            "runtime_metadata_compacted": True,
            "neighbor_roles_as_ids_only": True,
            "shared_identity_catalog": bool(include_material_index),
            "include_material_index": bool(include_material_index),
        },
    }
    if include_material_index:
        projected["material_identity_catalog"] = material_catalog
    verify_model_visible_projection(payload, projected)
    return projected


def _pointer_target(root: Mapping[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise ValueError("selection_material_pointer_invalid")
    value: Any = root
    for part in pointer[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, Mapping):
            value = value[part]
        else:
            raise ValueError("selection_material_pointer_unresolvable")
    return value


def verify_model_visible_projection(original: Mapping[str, Any], projected: Mapping[str, Any]) -> dict[str, Any]:
    """Check complete plan equality and every shared-catalog pointer."""

    _verify_selection_payload(original)
    _verify_selection_payload(projected)
    originals = {str(chapter["chapter_id"]): chapter for chapter in original.get("chapters") or []}
    visibles = {str(chapter["chapter_id"]): chapter for chapter in projected.get("chapters") or []}
    if set(originals) != set(visibles):
        raise ValueError("selection_projection_chapters_changed")
    for chapter_id, source in originals.items():
        visible = visibles[chapter_id]
        if visible.get("chapter_plan") != source.get("chapter_plan"):
            raise ValueError(f"selection_projection_plan_changed:{chapter_id}")
        for key in ("card_path", "excluded_source_ids", "excluded_source_handles", "source_exclusion_notes"):
            if key in visible.get("chapter") or key in visible.get("full_chapter_context"):
                raise ValueError(f"selection_projection_forbidden_key:{chapter_id}:{key}")
        for pointer in visible.get("material_identity_refs") or []:
            target = _pointer_target(projected, str(pointer))
            if not isinstance(target, Mapping) or not target.get("identity_key"):
                raise ValueError(f"selection_projection_pointer_invalid:{chapter_id}")
    return {
        "chapter_count": len(visibles),
        "unit_count": len(_ids(projected.get("all_unit_ids") or [])),
        "catalog_count": len(projected.get("material_identity_catalog") or []),
        "plan_sha256_by_chapter": {chapter_id: _hash(chapter.get("chapter_plan") or {}) for chapter_id, chapter in originals.items()},
        "pointer_count": sum(len(chapter.get("material_identity_refs") or []) for chapter in visibles.values()),
    }


def _verify_selection_payload(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("selection_mode"), Mapping):
        raise ValueError("selection_payload_required")
    if not payload.get("selection_mode", {}).get("enabled"):
        raise ValueError("selection_mode_not_enabled")
    for key in _FORBIDDEN_TOP_LEVEL:
        if payload.get(key):
            raise ValueError(f"selection_manual_control_forbidden:{key}")
    chapters = payload.get("chapters") or []
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("selection_chapters_required")
    known: set[str] = set()
    for chapter in chapters:
        if not isinstance(chapter, Mapping) or not _chapter_id(chapter):
            raise ValueError("selection_chapter_invalid")
        rows = _unit_rows(chapter)
        ids = [_unit_id(row) for row in rows]
        if len(ids) != len(set(ids)) or any(not value for value in ids):
            raise ValueError(f"selection_unit_ids_invalid:{_chapter_id(chapter)}")
        known.update(ids)
    if set(_ids(payload.get("all_unit_ids"))) != known:
        raise ValueError("selection_all_unit_ids_mismatch")


def selection_messages(
    payload: Mapping[str, Any], *, model_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build a full-outline, selection-only request with no domain targets."""

    _verify_selection_payload(payload)
    visible = payload
    if model_payload is not None:
        verify_model_visible_projection(payload, model_payload)
        visible = model_payload
    system = (
        "你是综述细纲加强前的自主任务选择器。你的职责只有判断现有完整细纲中哪些单元值得投入下一轮按需加强，"
        "并把值得一起处理的单元组成逻辑组；你不改写细纲、不生成替代计划、不写正文、不补充外部证据。"
        "请完整阅读研究问题、全篇章节职责、每章完整 chapter_plan 及其单元的主张、展开、条件、论证关系、"
        "paragraph_briefs、案例或 supporting_studies 用途、综合和衔接。不要按字数、引用数量或固定配额机械选择。"
        "可以自主发现具体且有依据的组织、比较、条件边界、材料分工或跨章衔接问题；选择理由是下一层编辑诉求，"
        "不是事实答案，也不能替代负责人细纲。可以返回 none/no_change。允许跨章逻辑组；本地会按章节拆成 owner 子任务，"
        "并把跨章组中的其他单元作为只读职责保留。只输出 JSON。"
    )
    if model_payload is not None:
        system += "模型可见输入是原始选择包的无损计划投影：每章 chapter_plan 原样保留；本地绝对卡片位置、运行排除清单和重复邻居正文未发送。"
        if model_payload.get("material_identity_catalog"):
            system += "身份目录已按稳定身份合并，material_identity_refs 是当前请求内可解析的 JSON pointer。"
        else:
            system += "本轮默认不发送材料身份目录；材料目录只在显式 include_material_index 模式下提供。"
        system += "完整原始材料仍由下一层负责人按原包和目录读取，不得把投影省略误判为证据不存在。"
    user_contract = {
        "status": "selected | none | no_change",
        "groups": [{
            "group_id": "model-stable-label",
            "unit_ids": ["existing stable unit IDs"],
            "selection_reason": "material-backed reason to invest in this group",
            "improvement_focus": ["specific organization/evidence/condition/comparison responsibility"],
            "related_read_only_unit_ids": ["existing units that should remain boundary context"],
        }],
    }
    user = (
        "返回且只返回符合以下合同的 JSON：" + json.dumps(user_contract, ensure_ascii=False, separators=(",", ":"))
        + "。status=none 或 no_change 时 groups 必须为空。unit_ids 必须来自输入且不得重复；"
        "related_read_only_unit_ids 只能引用输入中已有单元，不得把只读职责变成编辑任务。"
        "不要输出 updated_plan、chapter_updates、正文、替代措辞、外部答案或人工问题清单。"
        "跨章组可包含多个 chapter_id 的 unit_ids，但只能使用输入已有稳定 ID。\n完整选择输入："
        + json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _known_units(payload: Mapping[str, Any]) -> tuple[dict[str, str], set[str]]:
    chapter_by_unit: dict[str, str] = {}
    readonly: set[str] = set(_ids(payload.get("selector_forbidden_unit_ids") or []))
    for chapter in payload.get("chapters") or []:
        chapter_id = _chapter_id(chapter)
        for row in _unit_rows(chapter):
            chapter_by_unit[_unit_id(row)] = chapter_id
    return chapter_by_unit, readonly


def _canonical_group_id(unit_ids: Sequence[str]) -> str:
    return "selection-" + _hash(sorted(str(value) for value in unit_ids))[:16]


def validate_selection_response(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Validate selector structure without guessing invalid IDs or groups."""

    _verify_selection_payload(payload)
    errors: list[str] = []
    if not isinstance(parsed, Mapping):
        return {"status": "invalid", "groups": [], "validation_errors": ["selection_response_not_object"]}
    raw_status = str(parsed.get("status") or "").strip().casefold()
    if raw_status in _NO_SELECTION_STATUSES:
        if parsed.get("groups"):
            errors.append("no_change_with_groups")
        return {"status": "no_change", "groups": [], "validation_errors": errors}
    if raw_status not in {"selected", "selection", "updated"}:
        return {"status": "invalid", "groups": [], "validation_errors": ["selection_status_required_or_invalid"]}
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        return {"status": "invalid", "groups": [], "validation_errors": ["selected_groups_required"]}
    chapter_by_unit, known_readonly = _known_units(payload)
    known_units = set(chapter_by_unit)
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            errors.append(f"group_not_object:{index}")
            continue
        unit_ids = _ids(raw.get("unit_ids") or raw.get("modifiable_unit_ids"))
        if not unit_ids:
            errors.append(f"group_units_required:{index}")
            continue
        unknown = sorted(set(unit_ids) - known_units)
        if unknown:
            errors.append(f"unknown_unit_ids:{','.join(unknown)}")
        forbidden_selected = sorted(set(unit_ids) & known_readonly)
        if forbidden_selected:
            errors.append(f"readonly_unit_ids:{','.join(forbidden_selected)}")
        duplicate = sorted(set(unit_ids) & seen)
        if duplicate:
            errors.append(f"unit_repeated_across_groups:{','.join(duplicate)}")
        seen.update(unit_ids)
        chapter_ids = sorted({chapter_by_unit.get(value, "") for value in unit_ids if value in chapter_by_unit})
        explicit_chapters = _ids(raw.get("chapter_ids") or ([raw.get("chapter_id")] if raw.get("chapter_id") else []))
        if explicit_chapters and set(explicit_chapters) != set(chapter_ids):
            errors.append(f"group_chapter_ids_mismatch:{index}")
        related = _ids(raw.get("related_read_only_unit_ids") or raw.get("read_only_unit_ids") or [])
        related_unknown = sorted(set(related) - known_units)
        if related_unknown:
            errors.append(f"unknown_related_readonly_ids:{','.join(related_unknown)}")
        overlap = sorted(set(unit_ids) & set(related))
        if overlap:
            errors.append(f"editable_readonly_overlap:{','.join(overlap)}")
        if not str(raw.get("selection_reason") or "").strip():
            errors.append(f"selection_reason_required:{index}")
        focus = raw.get("improvement_focus")
        if not isinstance(focus, list) or not any(str(item).strip() for item in focus):
            errors.append(f"improvement_focus_required:{index}")
        groups.append({
            "group_id": _canonical_group_id(unit_ids),
            "model_group_id": str(raw.get("group_id") or "").strip(),
            "chapter_ids": chapter_ids,
            "unit_ids": unit_ids,
            "selection_reason": str(raw.get("selection_reason") or ""),
            "improvement_focus": _copy(focus or []),
            "related_read_only_unit_ids": related,
            "known_readonly_unit_ids": sorted(set(related) & known_readonly),
        })
    if errors:
        return {"status": "invalid", "groups": groups, "validation_errors": list(dict.fromkeys(errors))}
    return {"status": "selected", "groups": groups, "validation_errors": []}


def _role_from_unit(unit: Mapping[str, Any], chapter_id: str, *, reason: str) -> dict[str, Any]:
    role = _copy(unit)
    role["unit_id"] = _unit_id(unit)
    role["chapter_id"] = chapter_id
    role["read_only"] = True
    role["selection_boundary_reason"] = reason
    return role


def _current_unit_roles(
    chapter_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Index the latest complete unit duties by stable ID.

    Selection payloads are projected more than once as earlier owner results
    are merged into the current total outline.  The source payload for a later
    projection is authoritative; an older ``readonly_neighbor_unit_roles``
    entry is only a fallback when its unit no longer exists in that payload.
    """

    current: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for chapter_id, payload in chapter_payloads.items():
        for row in _unit_rows(payload):
            unit_id = _unit_id(row)
            if unit_id:
                current[unit_id] = (str(chapter_id), row)
    return current


def _chapter_sequence_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact complete-chapter order metadata for a local owner task.

    A projected payload contains only the selected editable units in
    ``chapter_plan.units``.  The owner still needs to know where those units
    sit in the complete chapter when it writes transitions.  Keep that
    information as stable IDs only; the full unit duties remain in the
    read-only role objects and are not duplicated here.
    """

    ordered = [_unit_id(row) for row in _unit_rows(payload) if _unit_id(row)]
    adjacent: dict[str, dict[str, str | None]] = {}
    for index, unit_id in enumerate(ordered):
        adjacent[unit_id] = {
            "previous_unit_id": ordered[index - 1] if index else None,
            "next_unit_id": ordered[index + 1] if index + 1 < len(ordered) else None,
        }
    return {
        "full_unit_order": ordered,
        "adjacent_unit_ids": adjacent,
        "order_source": "current_complete_chapter_payload",
    }


def project_selection_group(
    chapter_payloads: Mapping[str, Mapping[str, Any]], group: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Create chapter-local owner payloads while preserving group provenance."""

    unit_to_chapter: dict[str, str] = {}
    for chapter_id, payload in chapter_payloads.items():
        for row in _unit_rows(payload):
            unit_to_chapter[_unit_id(row)] = str(chapter_id)
    selected = _ids(group.get("unit_ids"))
    selected_set = set(selected)
    group_id = str(group.get("group_id") or _canonical_group_id(selected))
    related = set(_ids(group.get("related_read_only_unit_ids") or []))
    chapter_ids = _ids(group.get("chapter_ids") or sorted({unit_to_chapter[value] for value in selected if value in unit_to_chapter}))
    current_roles = _current_unit_roles(chapter_payloads)
    outputs = []
    for chapter_id in chapter_ids:
        source = chapter_payloads.get(chapter_id)
        if not isinstance(source, Mapping):
            continue
        local_selected = [value for value in selected if unit_to_chapter.get(value) == chapter_id]
        if not local_selected:
            continue
        local_set = set(local_selected)
        projected = _copy(source)
        original_plan = projected.get("chapter_plan") if isinstance(projected.get("chapter_plan"), Mapping) else {}
        units = [row for row in original_plan.get("units") or [] if isinstance(row, Mapping) and _unit_id(row) in local_set]
        projected_plan = _copy(original_plan)
        projected_plan["units"] = units
        projected["chapter_plan"] = projected_plan
        context = _copy(projected.get("full_chapter_context") or {})
        context.update(_chapter_sequence_context(source))
        projected["full_chapter_context"] = context
        existing_roles = [row for row in projected.get("readonly_neighbor_unit_roles") or [] if isinstance(row, Mapping)]
        role_by_id = {
            _unit_id(row): _copy(row)
            for row in existing_roles
            if _unit_id(row) and _unit_id(row) not in local_set
        }
        # The current complete payload wins over a role copied from an older
        # projection.  Keep unknown legacy roles only as an audit fallback.
        for unit_id in list(role_by_id):
            current = current_roles.get(unit_id)
            if current is not None:
                owner_chapter, current_unit = current
                role_by_id[unit_id] = _role_from_unit(
                    current_unit, owner_chapter, reason="current_total_outline_context",
                )
        source_units = {_unit_id(row): row for row in _unit_rows(source)}
        # Every unselected unit in the current chapter is a read-only boundary.
        for unit_id, row in source_units.items():
            if unit_id not in local_set:
                role_by_id[unit_id] = _role_from_unit(row, chapter_id, reason="unselected_same_chapter_unit")
        # A cross-chapter group remains visible as read-only context in each child.
        for unit_id in selected_set | related:
            owner_chapter = unit_to_chapter.get(unit_id)
            if unit_id in local_set or not owner_chapter or owner_chapter == chapter_id:
                continue
            other = chapter_payloads.get(owner_chapter) or {}
            other_units = {_unit_id(row): row for row in _unit_rows(other)}
            if unit_id in other_units:
                role_by_id[unit_id] = _role_from_unit(other_units[unit_id], owner_chapter, reason="cross_chapter_group_context")
        # Existing read-only IDs may not have had a role object in a prior
        # packet.  Resolve them from the latest total outline when possible.
        for unit_id in _ids(projected.get("read_only_unit_ids") or []):
            if unit_id in local_set or unit_id in role_by_id:
                continue
            current = current_roles.get(unit_id)
            if current is not None:
                owner_chapter, current_unit = current
                role_by_id[unit_id] = _role_from_unit(
                    current_unit, owner_chapter, reason="current_total_outline_context",
                )
        readonly_ids = set(_ids(projected.get("read_only_unit_ids") or [])) | set(role_by_id)
        readonly_ids -= local_set
        projected["readonly_neighbor_unit_roles"] = list(role_by_id.values())
        projected["modifiable_unit_ids"] = local_selected
        projected["read_only_unit_ids"] = sorted(readonly_ids)
        # Keep the selector's machine-generated investment rationale visible to
        # the owner request as advisory provenance.  It is deliberately kept
        # outside chapter_plan/material channels and is never treated as fact.
        projected["selection_context"] = {
            "origin": "autonomous_unit_selector",
            "advisory_only": True,
            "group_id": group_id,
            "selection_reason": str(group.get("selection_reason") or ""),
            "improvement_focus": _copy(group.get("improvement_focus") or []),
            "group_chapter_ids": chapter_ids,
            "selected_unit_ids": selected,
            "related_read_only_unit_ids": sorted(related),
        }
        projected["call_id"] = f"outline-selection:{group_id}:{chapter_id}"
        contract = _copy(projected.get("unit_identity_contract") or {})
        contract["existing_unit_ids"] = list(local_selected)
        projected["unit_identity_contract"] = contract
        projected["input_integrity"] = strengthening._input_integrity(projected)
        strengthening._verify_envelope(projected)
        outputs.append({
            "group_id": group_id,
            "selection_reason": str(group.get("selection_reason") or ""),
            "improvement_focus": _copy(group.get("improvement_focus") or []),
            "group_chapter_ids": chapter_ids,
            "chapter_id": chapter_id,
            "modifiable_unit_ids": local_selected,
            "read_only_unit_ids": sorted(readonly_ids),
            "payload": projected,
        })
    return outputs


def selection_to_on_demand_payloads(
    chapter_payloads: Mapping[str, Mapping[str, Any]], selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if str(selection.get("status") or "").casefold() in {"none", "no_change"}:
        return []
    if str(selection.get("status") or "").casefold() != "selected":
        raise ValueError("selection_not_accepted_for_projection")
    outputs = []
    for group in selection.get("groups") or []:
        outputs.extend(project_selection_group(chapter_payloads, group))
    return outputs


def _checkpoint_signature(messages: Sequence[Mapping[str, Any]], *, model: str, thinking_budget: int, max_output_tokens: int, profile: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "request_sha256": _hash(messages),
        "model": str(model),
        "thinking_budget": int(thinking_budget),
        "max_output_tokens": int(max_output_tokens),
        "profile": _copy(profile or {}),
    }


def _checkpoint(path: str | Path | None, name: str) -> Path | None:
    if path is None:
        return None
    result = Path(path) / name
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def _write(path: Path | None, value: Mapping[str, Any]) -> None:
    if path is not None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _load(path: Path | None, signature: Mapping[str, Any], resume: bool) -> dict[str, Any] | None:
    if path is None or not resume or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if value.get("signature") == dict(signature):
        return value
    # Never overwrite an old response when the prepared payload/profile has
    # changed.  Keep it available for audit while allowing a fresh call to be
    # prepared under the new signature.
    suffix = str(signature.get("request_sha256") or "incompatible")[:16]
    archived = path.with_name(f"{path.stem}.incompatible-{suffix}{path.suffix}")
    if not archived.exists():
        try:
            archived.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        except OSError:
            pass
    return None


def estimate_selection_request(messages: Sequence[Mapping[str, Any]], *, profile: Mapping[str, Any], token_counter: Any = None) -> dict[str, Any]:
    return strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=token_counter)


def run_selection(
    payload: Mapping[str, Any], *, client: Any, model: str, thinking_budget: int, max_output_tokens: int,
    call_id: str | None = None, checkpoint_dir: str | Path | None = None, resume: bool = True,
    profile: Mapping[str, Any] | None = None, model_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _verify_selection_payload(payload)
    if model_payload is None:
        model_payload = model_visible_selection_payload(payload)
    messages = selection_messages(payload, model_payload=model_payload)
    signature = _checkpoint_signature(messages, model=model, thinking_budget=thinking_budget, max_output_tokens=max_output_tokens, profile=profile)
    stage_path = _checkpoint(checkpoint_dir, "SELECTION_STAGE.json")
    stage = _load(stage_path, signature, resume)
    raw_reused = False
    if stage is not None:
        raw = stage.get("raw_response") or {}
        parsed = stage.get("parsed_response") or {}
        telemetry = stage.get("telemetry") or {}
    else:
        raw_path = _checkpoint(checkpoint_dir, "SELECTION_RAW_STAGE.json")
        raw_stage = _load(raw_path, signature, resume)
        if raw_stage is not None:
            raw = raw_stage.get("raw_response") or {}
            raw_reused = True
        else:
            from .module4 import runtime
            raw = runtime.invoke_client(
                client, messages, model=model, max_output_tokens=int(max_output_tokens), thinking=True,
                thinking_budget=int(thinking_budget), call_id=call_id or str(payload.get("call_id") or "outline-selection"),
            )
            _write(raw_path, {"signature": signature, "raw_response": _copy(raw)})
        parsed, telemetry = planning._parse_planner_response(raw)
        validation = validate_selection_response(payload, parsed)
        result = {
            **validation, "parsed_response": parsed, "telemetry": telemetry, "raw_response": raw,
            "messages": messages, "request_sha256": _hash(messages), "raw_reused": raw_reused,
        }
        _write(stage_path, {"signature": signature, "result": result, "raw_response": _copy(raw), "parsed_response": _copy(parsed), "telemetry": _copy(telemetry)})
        return result
    validation = validate_selection_response(payload, parsed)
    return {
        **validation, "parsed_response": parsed, "telemetry": telemetry, "raw_response": raw,
        "messages": messages, "request_sha256": _hash(messages), "stage_reused": True,
    }


__all__ = [
    "SELECTION_SCHEMA", "SELECTION_ROLE", "build_selection_payload", "selection_messages",
    "validate_selection_response", "project_selection_group", "selection_to_on_demand_payloads",
    "model_visible_selection_payload", "verify_model_visible_projection",
    "estimate_selection_request", "run_selection",
]
