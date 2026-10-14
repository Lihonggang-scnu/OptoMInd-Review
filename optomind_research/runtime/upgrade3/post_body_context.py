"""Read-only, domain-independent context from this BODY's delivered input.

The manuscript remains the primary evidence. This projection carries positioning
and explicit background/definition uses, not another planner or a source pool.
All scientific values are copied whole; only demonstrably equal repeats go away.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .serial_manuscript_parts import (
    SerialPartsError, _dump, merge_source_identity_maps, normalize_source_identity_map,
)

_BACKGROUND = re.compile(r"背景|定义|\b(?:background|definition|definitions)\b", re.I)


def body_input_sha256(book: Mapping[str, Any]) -> str:
    """The existing legacy BODY run's exact semantic JSON hash."""
    from .legacy_unit_route import _hash
    return _hash(book)


def _uses(value):
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "source_uses" and isinstance(child, list):
                yield from (row for row in child if isinstance(row, Mapping))
            else:
                yield from _uses(child)
    elif isinstance(value, list):
        for child in value:
            yield from _uses(child)


def _task(task):
    result = deepcopy(task)
    briefs = result.get("source_brief_details", [])
    if len(briefs) == 1 and isinstance(briefs[0], dict):
        # Multiple briefs can have complementary development; never collapse them.
        for key in ("point", "development"):
            if key in briefs[0] and briefs[0][key] == result.get(key):
                del briefs[0][key]
    return result


def _owner(owner):
    result = deepcopy(owner)
    aliases = result.get("owner_field_values")
    if isinstance(aliases, Mapping):
        canonical = [value for key, value in result.items() if key != "owner_field_values"]
        result["owner_field_values"] = {key: value for key, value in aliases.items()
                                        if value not in canonical}
        if not result["owner_field_values"]:
            del result["owner_field_values"]
    return result


def _outline(book):
    result = []
    for chapter in book.get("chapters", []):
        frame = deepcopy(chapter.get("chapter_frame", {}))
        for key, authority in (("research_question", book.get("research_question")),
                               ("review_argument", book.get("review_argument")),
                               ("shared_scope", book.get("review_scope"))):
            if key in frame and frame[key] == authority:
                del frame[key]
        for alias in ("chapter_thesis", "chapter_argument", "reader_objective"):
            if alias in frame and frame[alias] in [frame.get("chapter_scope"), frame.get("chapter_purpose")]:
                del frame[alias]
        units = []
        for unit in chapter.get("units", []):
            # This association may be unit-specific even when some inventories
            # happen to repeat. A global identity directory cannot replace it.
            row = deepcopy(unit)
            for key in ("paragraph_tasks", "table_tasks"):
                if key in unit:
                    row[key] = [_task(task) for task in unit[key]]
            if "owner_unit_context" in unit:
                row["owner_unit_context"] = _owner(unit["owner_unit_context"])
            units.append(row)
        result.append({"chapter_id": chapter["chapter_id"], "chapter_frame": frame, "units": units})
    return result


def _add_values(target, key, value):
    values = value if isinstance(value, list) else [value]
    for item in values:
        if item not in (None, "", [], {}) and item not in target.setdefault(key, []):
            target[key].append(deepcopy(item))


def build_post_body_context(book: Mapping[str, Any], *, context: Mapping[str, Any] | None = None):
    """Return (context, receipt); explicit fields win, identities must agree.

    Limits are enforced by the unchanged serial normalizer. Empty background
    selection is reported, never filled with invented or metadata-only science.
    """
    if book.get("schema_version") != "optomind.fullbody.input.v1":
        raise SerialPartsError("post_body_context_requires_full_body_input")
    explicit = deepcopy(dict(context or {}))
    identities = normalize_source_identity_map(book.get("source_identities", {}))
    aliases = book.get("source_aliases", {})
    selected, selection_uses = [], []
    for chapter in book.get("chapters", []):
        for unit in chapter.get("units", []):
            for task in [*unit.get("paragraph_tasks", []), *unit.get("table_tasks", [])]:
                for use in _uses(task):
                    if _BACKGROUND.search(str(use.get("role") or "")):
                        handle = use.get("source_handle")
                        handle = aliases.get(handle, handle)
                        if handle not in identities:
                            raise SerialPartsError(f"post_body_background_identity_missing:{handle}")
                        if handle not in selected:
                            selected.append(handle)
                        selection_uses.append({"chapter_id": chapter["chapter_id"],
                            "unit_id": unit.get("unit_id"),
                            "task_id": task.get("paragraph_id") or task.get("table_id"),
                            "canonical_source_handle": handle, "source_use": deepcopy(use)})
    rows = book.get("sources", [])
    if not isinstance(rows, list):
        raise SerialPartsError("post_body_sources_must_be_array")
    materials, unavailable = [], []
    for handle in selected:
        material = {"source_handle": handle}
        for source in rows:
            source_handle = aliases.get(source.get("source_handle"), source.get("source_handle"))
            if source_handle != handle:
                continue
            # Validate identity before combining any scientific values.
            merge_source_identity_maps(identities, {handle: {**source, "source_handle": handle}})
            for section, fields in (
                ("study_summary_A", ("key_findings", "research_scope")),
                ("review_planning_B", ("scope_interpretation_cautions", "broader_review_uses")),
            ):
                data = source.get(section) or {}
                for key in fields:
                    if data.get(key) not in (None, "", [], {}):
                        _add_values(material.setdefault(section, {}), key, data[key])
            # Precision and tool results can complement A/B for the same source.
            # Carry the fulfilled semantic records, not wire data/full-page dumps.
            deep = source.get("deep_read_material") or {}
            for key in ("question_material", "current_question_material", "prior_question_material"):
                _add_values(material, "precision_reading_material", deep.get(key))
            for tool in source.get("tool_supplement_materials", []):
                projected = deepcopy(tool)
                usable = projected.get("usable_content") if isinstance(projected, dict) else None
                if isinstance(usable, str):
                    try:
                        decoded = json.loads(usable)
                    except (ValueError, TypeError):
                        pass
                    else:
                        if isinstance(decoded, Mapping):
                            projected["usable_content"] = {key: value for key, value in decoded.items()
                                if value not in (None, "", [], {})}
                if not isinstance(projected, Mapping) or any(projected.get(key) not in (None, "", [], {})
                        for key in ("usable_content", "text", "content", "conditions", "limits", "examples")):
                    _add_values(material, "tool_supplement_materials", projected)
            for key in ("usable_content", "text", "content", "supplement_material"):
                _add_values(material, key, source.get(key))
        material = {key: value for key, value in material.items() if value not in ([], {}, "", None)}
        if len(material) > 1:
            materials.append(material)
        else:
            unavailable.append(handle)
    derived = {"research_question": book.get("research_question", ""),
               "shared_scope": deepcopy(book.get("review_scope", {})),
               "review_argument": deepcopy(book.get("review_argument", "")),
               "final_outline": _outline(book), "material_records": materials}
    if explicit.get("research_question") and explicit["research_question"] != derived["research_question"]:
        raise SerialPartsError("post_body_research_question_conflict")
    supplied = normalize_source_identity_map(merge_source_identity_maps(
        identities, explicit.get("source_identity_map", {})), explicit.get("material_records", []))
    derived.update(explicit)
    derived["source_identity_map"] = merge_source_identity_maps(identities, supplied)
    receipt = {"schema_version": "optomind.post_body_context.projection.v1",
        "body_input_sha256": body_input_sha256(book), "explicit_fields": sorted(explicit),
        "background_role_handles": selected, "background_material_unavailable": unavailable,
        "background_selection_uses": selection_uses,
        "background_boundary": "BODY remains primary evidence; no synthetic background when roles/content are absent",
        "chars_are_unicode_not_tokens": True,
        "component_chars": {key: len(_dump(value)) for key, value in derived.items()},
        "local_context_chars": len(_dump(derived)),
        "identity_count": len(derived["source_identity_map"]),
        "material_records": len(derived["material_records"])}
    return derived, receipt


def load_body_context_input(report: Mapping[str, Any], *, body_input_path=None):
    """Resolve one explicit run, never search/glob or use a latest pointer."""
    output_root = report.get("output_root")
    if not isinstance(output_root, str) or not output_root.strip():
        raise SerialPartsError("post_body_report_output_root_required")
    root = Path(output_root)
    if not root.is_absolute():
        raise SerialPartsError("post_body_report_output_root_must_be_absolute")
    root = root.resolve()
    recorded = report.get("body_input_path")
    path = Path(body_input_path or recorded or root / "FULL_BODY_INPUT.json").resolve()
    if path.parent != root:
        raise SerialPartsError("post_body_input_not_same_run_output_root")
    if recorded and Path(recorded).resolve() != path:
        raise SerialPartsError("post_body_input_report_path_conflict")
    book = json.loads(path.read_text(encoding="utf-8"))
    identity_path = root / "RUN_IDENTITY.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    actual = body_input_sha256(book)
    if not identity.get("book_sha256") or identity["book_sha256"] != actual:
        raise SerialPartsError("post_body_input_run_identity_mismatch")
    if report.get("body_input_sha256") and report["body_input_sha256"] != actual:
        raise SerialPartsError("post_body_input_report_hash_conflict")
    selected = report.get("selected_body") or {}
    for key in ("handles_draft", "references_path"):
        if selected.get(key) and not Path(selected[key]).resolve().is_relative_to(root):
            raise SerialPartsError(f"post_body_selected_artifact_not_same_run:{key}")
    return book, {"body_input_path": str(path), "body_input_sha256": actual,
                  "run_identity_path": str(identity_path)}


def normalize_built_context(context, receipt, *, research_question, body_text, chapter_roles=()):
    """Keep native validation and attach actionable size diagnostics on overflow."""
    from .serial_manuscript_parts import normalize_context
    try:
        return normalize_context(context, research_question=research_question,
                                 body_text=body_text, chapter_roles=chapter_roles)
    except SerialPartsError as exc:
        if str(exc) in {"material_records_too_long", "context_too_long"}:
            raise SerialPartsError(str(exc) + ":unicode_chars:" + _dump(receipt)) from exc
        raise
