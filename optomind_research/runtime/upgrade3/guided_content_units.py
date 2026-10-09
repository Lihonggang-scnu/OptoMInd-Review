"""Lossless content connections for guide-organized writing units.

Task identities connect approved content to a writing batch, not to paragraphs.
No model call, scientific rewriting, or publication layout is performed here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .writer_candidates_contracts import CandidateError


def content_task_aliases(pack: Mapping[str, Any]) -> dict[str, str]:
    """Short exact addresses scoped to this immutable input's ordered tasks."""
    return {"C" + str(index).zfill(4): tid
            for index, tid in enumerate(pack["tasks"], 1)}


def content_task_catalog(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Small readable address index; full task text lives in full_outline."""
    return [{"task_id": alias, "chapter_id": row["chapter_id"],
             "unit_id": row["unit_id"], "kind": row["kind"],
             "original_task_id": row.get("original_task_id"),
             "source_handles": deepcopy(pack["task_source_handles"].get(tid, []))}
            for alias, tid in content_task_aliases(pack).items()
            for row in [pack["tasks"][tid]]]


def guide_with_task_aliases(guide: Any, pack: Mapping[str, Any]) -> Any:
    """Keep stored guide canonical; shorten only the maker's prior-guide view."""
    result = deepcopy(guide)
    reverse = {tid: alias for alias, tid in content_task_aliases(pack).items()}
    if isinstance(result, Mapping):
        for chapter in result.get("chapters", []):
            for unit in chapter.get("writing_units", []):
                unit["content_task_ids"] = [reverse.get(tid, tid)
                    for tid in unit.get("content_task_ids", [])]
    return result


def normalize_writing_units(units: Any, chapter_id: str,
                            pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate addresses and preserve unassigned approved content visibly.

    Incomplete grouping is recoverable: append original-unit groups, with an
    assembly note. Unknown/cross-chapter references and duplicate ownership
    are ambiguous and remain explicit errors instead of guesses.
    """
    if not isinstance(units, list):
        raise CandidateError("guide_writing_units_must_be_list")
    tasks = pack["tasks"]
    aliases = content_task_aliases(pack)
    allowed = {"unit_id", "title", "writing_arrangement", "content_task_ids",
               "required_content", "source_handles", "assembly_note"}
    result, assigned, identifiers = [], set(), set()
    for unit in units:
        if not isinstance(unit, Mapping) or set(unit) - allowed:
            raise CandidateError("guide_writing_unit_invalid_keys")
        for field in ("unit_id", "title", "writing_arrangement"):
            if not isinstance(unit.get(field), str) or not unit[field].strip():
                raise CandidateError("guide_writing_unit_" + field + "_missing")
        uid = unit["unit_id"]
        if uid in identifiers:
            raise CandidateError("guide_writing_unit_duplicated:" + uid)
        identifiers.add(uid)
        row = deepcopy(dict(unit))
        for field in ("content_task_ids", "required_content", "source_handles"):
            values = row.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise CandidateError("guide_writing_unit_" + field + "_must_be_string_list")
            if field in row or field == "content_task_ids":
                row[field] = list(dict.fromkeys(values))
        if "assembly_note" in row and (not isinstance(row["assembly_note"], str) or not row["assembly_note"].strip()):
            raise CandidateError("guide_writing_unit_assembly_note_invalid")
        row["content_task_ids"] = list(dict.fromkeys(
            aliases.get(tid, tid) for tid in row["content_task_ids"]))
        for tid in row["content_task_ids"]:
            if tid not in tasks:
                raise CandidateError("guide_content_task_unknown:" + tid)
            if tasks[tid]["chapter_id"] != chapter_id:
                raise CandidateError("guide_content_task_chapter_mismatch:" + tid)
            if tid in assigned:
                raise CandidateError("guide_content_task_multiple_units:" + tid)
            assigned.add(tid)
        result.append(row)
    missing: dict[str, list[str]] = {}
    for tid, task in tasks.items():
        if task["chapter_id"] == chapter_id and tid not in assigned:
            missing.setdefault(task["unit_id"], []).append(tid)
    for index, (original_unit, ids) in enumerate(missing.items(), 1):
        uid = "retained_" + str(index).zfill(3)
        while uid in identifiers:
            uid = "_" + uid
        identifiers.add(uid)
        result.append({"unit_id": uid, "title": "保留内容 " + original_unit,
            "writing_arrangement": "结合本章安排与实际前文，充分解释这些已有内容，合并相近表述，保留独立比较、条件、反例与表格。",
            "content_task_ids": ids,
            "assembly_note": "指南未分配以下已有内容；程序按原单元 " + original_unit + " 保留，组织质量待亲读。"})
    return result


def resolve_content_basis(unit: Mapping[str, Any], pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return original task wrappers byte-for-value, including unknown details."""
    result = []
    for tid in unit.get("content_task_ids", []):
        if tid not in pack["tasks"]:
            raise CandidateError("guide_content_task_unknown:" + tid)
        result.append({"task_id": tid, **deepcopy(pack["tasks"][tid])})
    return result


def assembly_warnings(guide: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{"chapter_id": chapter["chapter_id"], "unit_id": unit["unit_id"],
             "message": unit["assembly_note"]}
            for chapter in guide["chapters"] for unit in chapter.get("writing_units", [])
            if unit.get("assembly_note")]


def resolve_content_contexts(unit: Mapping[str, Any], pack: Mapping[str, Any]) -> dict[str, Any]:
    """Keep complete original focus/notes/owner context once per linked unit."""
    result = {}
    for tid in unit.get("content_task_ids", []):
        if tid not in pack["tasks"]:
            raise CandidateError("guide_content_task_unknown:" + tid)
        row = pack["tasks"][tid]
        result[row["unit_id"]] = deepcopy(pack.get("unit_contexts", {}).get(
            row["chapter_id"], {}).get(row["unit_id"], {}))
    return result
