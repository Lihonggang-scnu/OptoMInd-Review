"""Lossless task/material handoff and conservative candidate-output contracts.

These additive contracts do not change the existing arranger or unit writer.
Coverage means a model-declared, structurally usable mapping, never acceptance
of the science or prose. All views are copies; projection does not mutate the
accepted chapter or silently shorten any task or source text.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from . import review_unit_writer as _writer
from .chapter_arrangement import normalize_doi

INPUT_SCHEMA = "optomind.writer_candidates.input.v1"
PAYLOAD_SCHEMA = "optomind.writer_candidates.payload.v1"
RESULT_SCHEMA = "optomind.writer_candidates.result.v1"


class CandidateError(ValueError):
    """Invalid identity, scope, or unusable candidate input."""


def _signature(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _distinct(rows: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for row in rows:
        key = _signature(row)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(row))
    return result


def _handles(value: Any) -> list[str]:
    """Read explicitly typed references, including nested condition/case records.

    Only accepted reference fields are active. Original/rejected handles stay
    as provenance: they must not resurrect a removed identity, borrow material
    from an unrelated paper, or become valid citations. A resolved source_handle
    remains authoritative even when the upstream row retains an older conflict
    status; an absent source_handle cannot be replaced by original_source_handle.
    Incidental prose, DOI, task IDs, titles and numeric citations are not IDs.
    """
    found: list[str] = []
    singular = {"source_handle", "review_source_handle"}
    plural = {"source_handles", "review_source_handles"}
    provenance_fields = {
        "original_source_handle", "original_source_handles", "rejected_source_handle", "rejected_source_handles",
        "original_source", "original_sources", "rejected_source", "rejected_sources",
        "historical_source_handles", "prior_source_handles", "candidate_source_handles",
    }

    def add(item: Any) -> None:
        if isinstance(item, str) and item.strip() and item not in found:
            found.append(item)

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                if key in provenance_fields:
                    continue
                if key in singular:
                    add(item)
                elif key in plural:
                    if isinstance(item, str):
                        add(item)
                    elif isinstance(item, (list, tuple)):
                        for entry in item:
                            if isinstance(entry, Mapping):
                                add(entry.get("source_handle"))
                            else:
                                add(entry)
                visit(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)

    visit(value)
    return found


def _unit_rows(chapter: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    units = chapter.get("units")
    if not isinstance(units, list) or not units:
        raise CandidateError("chapter_has_no_units")
    seen: set[str] = set()
    for unit in units:
        if not isinstance(unit, Mapping):
            raise CandidateError("unit_not_object")
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise CandidateError("unit_id_missing_or_invalid")
        if unit_id in seen:
            raise CandidateError("unit_id_duplicated:" + unit_id)
        seen.add(unit_id)
    return units


def task_catalog(chapter: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return complete original tasks indexed by chapter-local stable identity.

    Legacy tasks without IDs are named only in this index, not rewritten. The
    deterministic synthetic identity and its origin are explicit in each row.
    A unit's paragraph/table IDs share a namespace, so collisions fail closed.
    """
    result: dict[str, dict[str, Any]] = {}
    for unit in _unit_rows(chapter):
        unit_id = unit["unit_id"]
        explicit: set[str] = set()
        saved_keys = unit.get("task_keys", {})
        if not isinstance(saved_keys, Mapping):
            raise CandidateError("task_keys_not_object:" + unit_id)
        for field, id_field in (("paragraph_tasks", "paragraph_id"), ("table_tasks", "table_id")):
            rows = unit.get(field, [])
            if not isinstance(rows, list):
                raise CandidateError("tasks_not_list:" + unit_id + ":" + field)
            for task in rows:
                if not isinstance(task, Mapping):
                    raise CandidateError("task_not_object:" + unit_id)
                original_id = task.get(id_field)
                if original_id is None or original_id == "":
                    continue
                if not isinstance(original_id, str) or not original_id.strip():
                    raise CandidateError("task_id_invalid:" + unit_id)
                if original_id in explicit:
                    raise CandidateError("task_id_duplicated:" + unit_id + "::" + original_id)
                explicit.add(original_id)
        for field, id_field, kind in (("paragraph_tasks", "paragraph_id", "paragraph"),
                                      ("table_tasks", "table_id", "table")):
            preserved = saved_keys.get(field)
            if preserved is not None and (not isinstance(preserved, list) or len(preserved) != len(unit.get(field, []))):
                raise CandidateError("task_keys_invalid:" + unit_id + ":" + field)
            for index, task in enumerate(unit.get(field, []), 1):
                original_id = task.get(id_field)
                generated = original_id is None or original_id == ""
                task_id = original_id
                if preserved is not None:
                    key = preserved[index - 1]
                    prefix = unit_id + "::"
                    if not isinstance(key, str) or not key.startswith(prefix) or len(key) <= len(prefix):
                        raise CandidateError("task_key_unit_mismatch:" + str(key))
                    task_id = key[len(prefix):]
                    if not generated and task_id != original_id:
                        raise CandidateError("task_key_identity_mismatch:" + key)
                    if generated and task_id in explicit:
                        raise CandidateError("task_id_duplicated:" + key)
                    explicit.add(task_id)
                elif generated:
                    task_id = f"__legacy_{kind}_{index:04d}"
                    # Do not overwrite an explicit identity, even one using our
                    # reserved-looking prefix. Naming stays deterministic.
                    while task_id in explicit:
                        task_id = "_" + task_id
                    explicit.add(task_id)
                key = unit_id + "::" + task_id
                if key in result:
                    raise CandidateError("task_key_duplicated:" + key)
                result[key] = {"unit_id": unit_id, "kind": kind, "task": deepcopy(dict(task))}
                if generated:
                    result[key]["identity_origin"] = "generated_legacy_missing_id"
                    result[key]["generated_task_id"] = task_id
                    result[key]["task_position"] = index
    if not result:
        raise CandidateError("chapter_has_no_tasks")
    return result


def _source_index(sources: Any) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if not isinstance(sources, list):
        raise CandidateError("sources_not_list")
    catalog: dict[str, dict[str, Any]] = {}
    for raw in sources:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("source_handle"), str) or not raw["source_handle"]:
            raise CandidateError("source_identity_missing")
        handle = raw["source_handle"]
        aliases_value = raw.get("aliases") or []
        if not isinstance(aliases_value, list) or any(not isinstance(alias, str) or not alias or alias != alias.strip() for alias in aliases_value):
            raise CandidateError("source_alias_invalid:" + handle)
        if handle not in catalog:
            catalog[handle] = deepcopy(dict(raw))
            continue
        prior = catalog[handle]
        left_doi, right_doi = normalize_doi(prior.get("doi")), normalize_doi(raw.get("doi"))
        left_paper = prior.get("canonical_paper_id") or prior.get("paper_id")
        right_paper = raw.get("canonical_paper_id") or raw.get("paper_id")
        if ((left_doi and right_doi and left_doi != right_doi)
                or (left_paper and right_paper and left_paper != right_paper
                    and not (left_doi and left_doi == right_doi))):
            raise CandidateError("source_identity_conflict:" + handle)
        # An identical source is shared once. Any complementary view is kept
        # verbatim; choosing one conflicting value would be a science edit.
        prior_base = {key: value for key, value in prior.items() if key != "material_record_variants"}
        if _signature(prior_base) != _signature(raw):
            variants = prior.setdefault("material_record_variants", [])
            if _signature(raw) not in {_signature(item) for item in variants}:
                variants.append(deepcopy(dict(raw)))
            prior["aliases"] = list(dict.fromkeys([*(prior.get("aliases") or []), *(raw.get("aliases") or [])]))
    try:
        aliases = _writer._explicit_source_aliases(catalog)
    except _writer.UnitWritingError as exc:
        raise CandidateError(str(exc)) from exc
    return catalog, aliases


def _relevant_tools(chapter: Mapping[str, Any], unit_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = set(unit_ids)
    chapter_id = str(chapter.get("chapter_id") or "")
    tools: list[dict[str, Any]] = []
    for item in chapter.get("chapter_tool_materials") or []:
        if not isinstance(item, Mapping):
            raise CandidateError("chapter_tool_material_not_object")
        target = str(item.get("unit_key") or "").strip()
        if target and not any(target in {unit, chapter_id + ":" + unit} for unit in ids):
            continue
        tools.append(dict(item))
    return _distinct(tools)


def build_chapter_input(
    arrangement_path: str | Path, *, view_path: str | Path | None = None,
    packet_root: str | Path | None = None, language: str = "zh",
) -> dict[str, Any]:
    """Read the accepted arrangement through the existing real material builder."""
    try:
        arrangement = _writer.load_arrangement(arrangement_path)
        _unit_rows(arrangement)
        catalog = arrangement.get("source_catalog")
        if not isinstance(catalog, Mapping) or not catalog:
            raise CandidateError("arrangement_without_source_catalog")
        for handle, entry in catalog.items():
            if not isinstance(entry, Mapping):
                raise CandidateError("source_catalog_entry_not_object:" + str(handle))
            if entry.get("source_handle") not in (None, "", handle):
                raise CandidateError("source_catalog_identity_conflict:" + str(handle))
        aliases = _writer._explicit_source_aliases(catalog)
        units: list[dict[str, Any]] = []
        materials: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        material_notes: list[dict[str, Any]] = []
        views = []
        for original in arrangement["units"]:
            view = _writer.build_unit_view(
                arrangement_path, original["unit_id"], view_path=view_path,
                packet_root=packet_root, max_material_chars_per_source=0,
            )
            views.append(view)
            unit = {"unit_id": view.unit_id, "focus": view.focus,
                    "paragraph_tasks": deepcopy(view.paragraph_tasks),
                    "table_tasks": deepcopy(view.table_tasks),
                    "owner_unit_context": deepcopy(view.owner_unit_context),
                    "unit_notes": view.unit_notes}
            related = _relevant_tools(arrangement, [view.unit_id])
            handles = list(dict.fromkeys([*view.sources, *_handles(unit), *_handles(related)]))
            local = deepcopy(view.materials)
            present = {item["source_handle"] for item in local}
            # The old unit_handles intentionally has a narrow grammar. Retain
            # additional explicit dependencies in cases/conditions/owner tasks
            # and source lineage. The finite canonical catalog bounds closure.
            pending_handles = list(dict.fromkeys([*handles, *_handles(local)]))
            checked_handles: set[str] = set()
            while pending_handles:
                handle = pending_handles.pop(0)
                canonical = aliases.get(handle, handle)
                if canonical in checked_handles:
                    continue
                checked_handles.add(canonical)
                if handle not in handles:
                    handles.append(handle)
                if canonical not in present:
                    entry, note = _writer._material_entry(
                        canonical, catalog, packet_materials=None, max_chars_per_source=0)
                    local.append(entry)
                    material_notes.append({"unit_id": view.unit_id, **note})
                    present.add(canonical)
                    pending_handles.extend(_handles(entry))
            unit["source_handles"] = handles
            units.append(unit)
            materials.extend(local)
            warnings.extend({"unit_id": view.unit_id, **deepcopy(item)} for item in view.warnings)
            material_notes.extend({"unit_id": view.unit_id, **deepcopy(item)} for item in view.material_notes)
        source_index, _ = _source_index(materials)
        resolved_view = Path(views[0].view_path) if views[0].view_path else None
        view_exists = resolved_view is not None and resolved_view.is_file()
        source_files = {}
        if view_exists:
            source_files["view_sha256"] = hashlib.sha256(resolved_view.read_bytes()).hexdigest()
        elif packet_root:
            packet_file = Path(packet_root) / "writer_packets" / (views[0].chapter_id + ".json")
            if packet_file.is_file():
                source_files["packet_path"] = str(packet_file.resolve())
                source_files["packet_sha256"] = hashlib.sha256(packet_file.read_bytes()).hexdigest()
        chapter: dict[str, Any] = {
            "schema_version": INPUT_SCHEMA, "chapter_id": views[0].chapter_id,
            "chapter_frame": deepcopy(views[0].chapter_frame),
            "other_chapters": deepcopy(views[0].other_chapters), "language": language,
            "units": units, "sources": list(source_index.values()),
            "chapter_tool_materials": _distinct(arrangement.get("chapter_tool_materials") or []),
            "provenance": {"arrangement_path": str(Path(arrangement_path)),
                           "view_path": str(resolved_view.resolve()) if view_exists else "",
                           "packet_root": str(packet_root or ""),
                           **source_files,
                           "arrangement_sha256": hashlib.sha256(Path(arrangement_path).read_bytes()).hexdigest(),
                           "material_builder": "review_unit_writer.build_unit_view",
                           "material_notes": _distinct(material_notes)},
            "warnings": _distinct(warnings),
        }
        tasks = task_catalog(chapter)
        chapter["provenance"]["generated_task_ids"] = [
            {"task_key": key, **{name: value for name, value in item.items() if name != "task"}}
            for key, item in tasks.items() if "generated_task_id" in item]
        for note in material_notes:
            if note.get("issues"):
                chapter["warnings"].append({"code": "material_read_note", **deepcopy(note)})
        # Detect input identity ambiguity before any model request can be made.
        project_chapter(chapter)
        return chapter
    except _writer.UnitWritingError as exc:
        raise CandidateError(str(exc)) from exc


def _selected_ids(catalog: Mapping[str, Any], task_ids: Sequence[str] | None) -> list[str]:
    if task_ids is None:
        return list(catalog)
    if isinstance(task_ids, (str, bytes)) or not isinstance(task_ids, Sequence):
        raise CandidateError("task_ids_not_list")
    requested = list(task_ids)
    if not requested:
        raise CandidateError("task_ids_empty")
    if any(not isinstance(key, str) for key in requested):
        raise CandidateError("task_id_invalid")
    if len(set(requested)) != len(requested):
        raise CandidateError("task_ids_duplicated")
    unknown = [key for key in requested if key not in catalog]
    if unknown:
        raise CandidateError("task_ids_unknown:" + ",".join(unknown))
    # Chapter order is stable even if the caller supplied an arbitrary order.
    return [key for key in catalog if key in set(requested)]


def project_chapter(chapter: Mapping[str, Any], task_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Select whole tasks and their full evidence, with all-chapter intent.

    Compact means no neighboring source text, not clipped task instructions.
    Shared roles keep the original complete intent, conditions and relationships
    so an adjacent unit can avoid contradicting another unit's responsibility.
    """
    tasks = task_catalog(chapter)
    selected = _selected_ids(tasks, task_ids)
    selected_set = set(selected)
    selected_units: list[dict[str, Any]] = []
    handles: list[str] = []
    for unit in _unit_rows(chapter):
        keys = [key for key in selected if tasks[key]["unit_id"] == unit["unit_id"]]
        if not keys:
            continue
        projected = deepcopy(dict(unit))
        projected["paragraph_tasks"] = [deepcopy(tasks[key]["task"]) for key in keys if tasks[key]["kind"] == "paragraph"]
        projected["table_tasks"] = [deepcopy(tasks[key]["task"]) for key in keys if tasks[key]["kind"] == "table"]
        projected["editable_task_ids"] = keys
        projected["task_keys"] = {
            "paragraph_tasks": [key for key in keys if tasks[key]["kind"] == "paragraph"],
            "table_tasks": [key for key in keys if tasks[key]["kind"] == "table"],
        }
        # A unit-level source index includes sibling task materials. Derive the
        # task projection anew instead of treating that broad index as evidence.
        needed = _handles([projected["paragraph_tasks"], projected["table_tasks"],
                           projected.get("owner_unit_context", {})])
        projected["source_handles"] = needed
        handles.extend(needed)
        selected_units.append(projected)
    tools = _relevant_tools(chapter, [unit["unit_id"] for unit in selected_units])
    handles.extend(_handles(tools))
    source_index, aliases = _source_index(chapter.get("sources"))
    canonical: list[str] = []
    pending = list(dict.fromkeys(handles))
    while pending:
        handle = pending.pop(0)
        key = aliases.get(handle, handle)
        if key in canonical:
            continue
        if key not in source_index:
            raise CandidateError("task_source_missing:" + handle)
        canonical.append(key)
        # Explicit cross-source lineage remains available. Bibliographies and
        # incidental prose are not interpreted as dependencies.
        pending.extend(h for h in _handles(source_index[key]) if aliases.get(h, h) not in canonical)
    roles = []
    for key, item in tasks.items():
        # Editable intent is already present verbatim in units, indexed by
        # unit.task_keys. Neighbor intent remains complete and read-only.
        # This is reference deduplication, never summarization or clipping.
        roles.append({"task_id": key, "unit_id": item["unit_id"], "kind": item["kind"],
                      "editable": key in selected_set,
                      **({"editable_task_reference": key} if key in selected_set
                         else {"task": deepcopy(item["task"])})})
    editable_units = {unit["unit_id"] for unit in selected_units}
    return {
        "schema_version": PAYLOAD_SCHEMA, "chapter_id": chapter.get("chapter_id"),
        "language": chapter.get("language", "zh"), "chapter_frame": deepcopy(chapter.get("chapter_frame") or {}),
        "editable_task_ids": selected, "units": selected_units,
        "sources": [deepcopy(source_index[key]) for key in source_index if key in canonical],
        "chapter_tool_materials": tools,
        "shared_chapter_context": {
            "read_only": True, "other_chapters": deepcopy(chapter.get("other_chapters") or []),
            "task_roles": roles,
            "unit_contexts": [
                {"unit_id": unit["unit_id"],
                 **({"editable_unit_reference": unit["unit_id"]} if unit["unit_id"] in editable_units
                    else {"focus": deepcopy(unit.get("focus", "")),
                          "owner_unit_context": deepcopy(unit.get("owner_unit_context") or {})})}
                for unit in _unit_rows(chapter)],
        },
    }


def render_blocks(blocks: Sequence[Mapping[str, Any]]) -> str:
    """Join model prose in its supplied order, without manufacturing headings."""
    return "\n\n".join(block["body_markdown"].strip() for block in blocks
                       if isinstance(block, Mapping) and isinstance(block.get("body_markdown"), str)
                       and block["body_markdown"].strip())


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    match = re.fullmatch(r"(`{3,}|~{3,})(?:json)?\s*\n([\s\S]*?)\n\1\s*", value, re.IGNORECASE)
    return match.group(2).strip() if match else value


def _string_prefix(text: str, start: int) -> tuple[str, bool]:
    """Recover a JSON string prefix without synthesizing missing prose."""
    output: list[str] = []
    index = start + 1
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(output), True
        if char == "\\":
            if index + 1 >= len(text):
                break
            nxt = text[index + 1]
            if nxt == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", text[index + 2:index + 6]):
                codepoint = int(text[index + 2:index + 6], 16)
                if 0xD800 <= codepoint <= 0xDBFF and re.fullmatch(r"\\u[0-9A-Fa-f]{4}", text[index + 6:index + 12]):
                    low = int(text[index + 8:index + 12], 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        output.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
                        index += 12
                        continue
                # An unfinished UTF-16 pair remains literal, not an invalid
                # Unicode surrogate that would prevent saving partial progress.
                output.append(text[index:index + 6] if 0xD800 <= codepoint <= 0xDFFF else chr(codepoint))
                index += 6
                continue
            output.append(escapes.get(nxt, "\\" + nxt))
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output), False


def _partial_blocks(text: str) -> list[dict[str, Any]]:
    """Recover complete blocks and at most one unfinished final body string."""
    marker = re.search(r'"blocks"\s*:\s*\[', text)
    if marker is None:
        return []
    decoder = json.JSONDecoder(strict=False)
    index = marker.end()
    blocks: list[dict[str, Any]] = []
    while index < len(text):
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= len(text) or text[index] != "{":
            break
        try:
            block, end = decoder.raw_decode(text, index)
        except ValueError:
            fragment = text[index:]
            body_match = re.search(r'"body_markdown"\s*:\s*"', fragment)
            if body_match:
                body, _ = _string_prefix(fragment, body_match.end() - 1)
                block = {"body_markdown": body, "task_ids": [], "partial": True}
                for field in ("block_id", "task_ids"):
                    match = re.search(r'"' + field + r'"\s*:\s*', fragment[:body_match.start()])
                    if match:
                        try:
                            block[field] = decoder.raw_decode(fragment, match.end())[0]
                        except ValueError:
                            pass
                blocks.append(block)
            break
        if not isinstance(block, dict):
            break
        blocks.append(block)
        index = end
    return blocks


def _envelope(response: Any) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Unwrap common provider records; never guess coverage for bare prose."""
    if isinstance(response, Mapping):
        if "blocks" in response:
            return deepcopy(dict(response)), [], True
        for key in ("content", "response", "message"):
            if isinstance(response.get(key), (Mapping, str)):
                return _envelope(response[key])
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            return _envelope(choices[0])
        if isinstance(response.get("body_markdown"), str):
            return {"blocks": [{"block_id": "unassigned-0001", "task_ids": [],
                                "body_markdown": response["body_markdown"]}],
                    "issues": deepcopy(response.get("issues", []))}, [{"code": "candidate_blocks_missing"}], False
        return {"blocks": []}, [{"code": "candidate_response_unreadable"}], False
    if isinstance(response, str):
        text = _strip_json_fence(response)
        try:
            decoded = json.loads(text, strict=False)
        except ValueError:
            recovered = _partial_blocks(text)
            if recovered:
                return {"blocks": recovered}, [{"code": "candidate_json_incomplete_or_invalid"}], False
            # Preserve genuine bare prose separately from unreadable JSON syntax.
            body = text if not text.startswith(("{", "[", "```json", "~~~json")) else ""
            return {"blocks": [{"block_id": "unassigned-0001", "task_ids": [], "body_markdown": body}] if body else []}, [{"code": "candidate_json_missing_or_invalid"}], False
        if isinstance(decoded, Mapping):
            return _envelope(decoded)
    return {"blocks": []}, [{"code": "candidate_response_unreadable"}], False


def _transport_incomplete(response: Any) -> bool:
    if not isinstance(response, Mapping):
        return False
    if response.get("complete") is False or response.get("finish_reason") in ("length", "max_tokens", "content_filter", "error", "cancelled") or response.get("error"):
        return True
    choices = response.get("choices")
    return any(_transport_incomplete(response.get(key)) for key in ("response", "message")) or (
        isinstance(choices, list) and any(_transport_incomplete(choice) for choice in choices))


def parse_candidate_response(
    response: Any, chapter: Mapping[str, Any], task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Keep useful prose while conservatively reporting structural coverage.

    Claimed IDs cannot prove scientific fidelity. A table claim additionally
    needs an actual finished Markdown table in the claiming block. Citation
    diagnostics reuse the existing writer, with no numeric-citation repair.
    """
    catalog = task_catalog(chapter)
    selected = _selected_ids(catalog, task_ids)
    allowed = set(selected)
    envelope, problems, valid_envelope = _envelope(response)
    raw_issues = envelope.get("issues", [])
    issues = deepcopy(raw_issues) if isinstance(raw_issues, list) else [{"code": "candidate_issues_not_list", "reported_issues": deepcopy(raw_issues)}]
    if not isinstance(raw_issues, list):
        valid_envelope = False
    issues.extend(problems)
    raw_blocks = envelope.get("blocks")
    if not isinstance(raw_blocks, list):
        raw_blocks = []
        issues.append({"code": "candidate_blocks_not_list"})
        valid_envelope = False
    blocks: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    covered: set[str] = set()
    table_checks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks, 1):
        if not isinstance(raw, Mapping):
            issues.append({"code": "candidate_block_not_object", "position": index})
            valid_envelope = False
            continue
        block = deepcopy(dict(raw))
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip() or block_id in block_ids:
            issues.append({"code": "candidate_block_id_missing_or_duplicated", "position": index,
                           "reported_block_id": block_id})
            block_id = f"recovered-{index:04d}"
            while block_id in block_ids:
                block_id = "_" + block_id
            valid_envelope = False
        block["block_id"] = block_id
        block_ids.add(block_id)
        body = block.get("body_markdown")
        if not isinstance(body, str) or not body.strip():
            issues.append({"code": "candidate_block_body_missing", "block_id": block_id})
            valid_envelope = False
            block["body_markdown"] = body if isinstance(body, str) else ""
        refs = block.get("task_ids")
        if not isinstance(refs, list) or any(not isinstance(key, str) for key in refs):
            issues.append({"code": "candidate_block_task_ids_invalid", "block_id": block_id})
            block["reported_task_ids"] = deepcopy(refs)
            refs = []
            valid_envelope = False
        unknown = [key for key in refs if key not in catalog]
        out_of_scope = [key for key in refs if key in catalog and key not in allowed]
        if unknown or out_of_scope:
            issues.append({"code": "candidate_task_reference_invalid", "block_id": block_id,
                           "unknown_task_ids": unknown, "out_of_scope_task_ids": out_of_scope})
            block["reported_task_ids"] = deepcopy(refs)
            valid_envelope = False
        refs = list(dict.fromkeys(key for key in refs if key in allowed))
        block["task_ids"] = refs
        if not refs:
            issues.append({"code": "candidate_block_unassigned", "block_id": block_id})
            valid_envelope = False
        if body and isinstance(body, str) and body.strip() and not block.get("partial"):
            for key in refs:
                if catalog[key]["kind"] == "table":
                    check = _writer._markdown_table_check(body)
                    table_checks.append({"task_id": key, "block_id": block_id, **check})
                    if not check["valid"]:
                        issues.append({"code": "markdown_table_missing_or_invalid", "task_id": key, "block_id": block_id})
                        continue
                covered.add(key)
        blocks.append(block)
    incomplete = _transport_incomplete(response)
    if incomplete:
        issues.append({"code": "candidate_transport_incomplete"})
    pending = [key for key in selected if key not in covered]
    if pending:
        issues.append({"code": "candidate_tasks_pending", "task_ids": pending})
    body = render_blocks(blocks)
    payload = project_chapter(chapter, selected)
    try:
        known = _writer._material_known_handles(payload["sources"])
    except _writer.UnitWritingError as exc:
        raise CandidateError(str(exc)) from exc
    diagnostics = _writer._output_diagnostics(body, known, _writer._known_tool_identifiers(payload))
    diagnostics.update({"table_checks": table_checks, "coverage_kind": "model_declared_structural_only",
                        "declared_covered_task_ids": [key for key in selected if key in covered],
                        "semantic_quality_unreviewed": True, "transport_incomplete": incomplete,
                        "valid_response_envelope": valid_envelope, "numeric_citation_repairs": []})
    result = {"schema_version": RESULT_SCHEMA, "blocks": blocks, "body_markdown": body,
              "issues": issues, "complete": bool(body and valid_envelope and not pending and not incomplete),
              "pending_task_ids": pending, "diagnostics": diagnostics,
              "semantic_quality_unreviewed": True}
    if not valid_envelope or incomplete:
        result["raw_response"] = deepcopy(response)
    return result
