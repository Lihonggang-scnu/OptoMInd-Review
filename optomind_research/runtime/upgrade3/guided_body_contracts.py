"""Guide-first author contracts; legacy planning stays in the backend archive.

This is structural validation and scientific-material projection, not scientific
review or a guide-to-outline coverage validator. No provider calls occur here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Mapping, Sequence

from . import writing_evidence
from .fullbody_contracts import _chapter_rows, _transport_status
from .writer_candidates_contracts import CandidateError, _relevant_tools, _string_prefix

GUIDE_SCHEMA = "optomind.guided_body_guide.v1"
MATERIALS_SCHEMA = "optomind.guided_body_materials.v1"
# Exact orchestration fields only. Unknown scientific fields remain intact.
_TOOL_ORCHESTRATION = {"need_id", "unit_key", "unit_id", "chapter_ids", "chapter_id",
    "task_id", "task_ids", "paragraph_id", "table_id", "intended_use", "decision",
    "task_hash", "source_hash", "required_outputs", "fulfilled", "model_calls",
    "downloads", "whole_paper_rereads", "_progressive_task_requirements",
    "_progressive_task_signature", "tasks", "task", "unit_contexts", "chapter_frames",
    "paragraph_tasks", "table_tasks", "review_argument", "planning_summary", "topic_handles",
    "outline_action"}


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise CandidateError(label + "_must_be_string_list")
    return deepcopy(value)


def validate_guide(guide: Mapping[str, Any], book: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(guide, Mapping):
        raise CandidateError("guide_must_be_object")
    if set(guide) - {"schema_version", "manuscript_guide", "chapters"}:
        raise CandidateError("guide_unknown_keys")
    if guide.get("schema_version", GUIDE_SCHEMA) != GUIDE_SCHEMA:
        raise CandidateError("guide_schema_invalid")
    if not isinstance(guide.get("manuscript_guide"), str) or not guide["manuscript_guide"].strip():
        raise CandidateError("guide_manuscript_guide_missing")
    chapters = guide.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise CandidateError("guide_chapters_missing")
    normalized = []
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise CandidateError("guide_chapter_not_object")
        if set(chapter) - {"chapter_id", "title", "writing_arrangement", "required_content", "source_handles"}:
            raise CandidateError("guide_chapter_unknown_keys")
        for key in ("chapter_id", "title", "writing_arrangement"):
            if not isinstance(chapter.get(key), str) or not chapter[key].strip():
                raise CandidateError("guide_chapter_" + key + "_missing")
        row = deepcopy(dict(chapter))
        for key in ("required_content", "source_handles"):
            if key in row:
                row[key] = _strings(row[key], "guide_" + key)
        normalized.append(row)
    ids = [row["chapter_id"] for row in normalized]
    if len(set(ids)) != len(ids):
        raise CandidateError("guide_chapter_duplicated")
    if ids != [row["chapter_id"] for row in _chapter_rows(book)]:
        raise CandidateError("guide_chapter_order_or_identity_mismatch")
    return {"schema_version": GUIDE_SCHEMA, "manuscript_guide": guide["manuscript_guide"], "chapters": normalized}


def _scientific_tool(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        return {key: _scientific_tool(item, (*path, key)) for key, item in value.items()
                if key not in _TOOL_ORCHESTRATION
                and not ("review_planning_B" in path and key in {"possible_uses", "purpose", "connection_to_review"})
                and not (
                    key == "provenance" and isinstance(item, Mapping)
                    and set(item) <= {"downloads", "model_calls", "source", "whole_paper_rereads"})}
    if isinstance(value, list):
        return [_scientific_tool(item, path) for item in value]
    return deepcopy(value)


def compile_guided_materials(book: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the existing lossless compiler once, without projecting its tasks."""
    pack = writing_evidence.compile_evidence(book)
    chapter_sources = {cid: [] for cid in pack["chapter_order"]}
    # Union existing source links by chapter, without inventing guide-to-task links.
    for tid, row in pack["tasks"].items():
        for handle in pack["task_source_handles"][tid]:
            if handle not in chapter_sources[row["chapter_id"]]:
                chapter_sources[row["chapter_id"]].append(handle)
    chapter_tools = {}
    for ch in _chapter_rows(book):
        chapter_tools[ch["chapter_id"]] = [_scientific_tool(tool) for tool in
            _relevant_tools(ch, [unit["unit_id"] for unit in ch["units"]])]
    pack["chapter_source_handles"] = chapter_sources
    pack["chapter_tool_materials"] = chapter_tools
    return pack


def _canonical_source(pack: Mapping[str, Any], handle: str) -> str:
    canonical = pack["source_aliases"].get(handle, handle)
    if canonical not in pack["source_identities"]:
        raise CandidateError("guided_source_unknown:" + handle)
    return canonical


def _is_scientific_atom(atom: Mapping[str, Any]) -> bool:
    return atom["role"] == "evidence" and not any(part in _TOOL_ORCHESTRATION for part in atom["field_path"])


def _science_ids(pack: Mapping[str, Any], handles: Sequence[str]) -> list[str]:
    pending, chosen = list(handles), []
    while pending:
        handle = _canonical_source(pack, pending.pop(0))
        if handle in chosen:
            continue
        chosen.append(handle)
        pending.extend(pack["review_dependencies"].get(handle, []))
    return [aid for h in chosen for aid in pack["source_atom_ids"][h] if _is_scientific_atom(pack["atoms"][aid])]


def resolve_read_request(pack: Mapping[str, Any], request: Mapping[str, Any]) -> list[str]:
    atoms = _strings(request.get("read_atom_ids", []), "guided_read_atom_ids")
    handles = _strings(request.get("read_source_handles", []), "guided_read_source_handles")
    if not (atoms or handles):
        raise CandidateError("guided_read_empty")
    if len(set(atoms)) != len(atoms) or len(set(handles)) != len(handles):
        raise CandidateError("guided_read_duplicated")
    for aid in atoms:
        if aid not in pack["atoms"]:
            raise CandidateError("guided_atom_unknown:" + aid)
        if not _is_scientific_atom(pack["atoms"][aid]):
            raise CandidateError("guided_atom_not_scientific_evidence:" + aid)
    # A reread includes source context and review mediation, not a naked finding.
    handles += [pack["atoms"][aid]["source_handle"] for aid in atoms]
    return list(dict.fromkeys([*atoms, *_science_ids(pack, handles)]))


def render_manuscript_guide(guide: Mapping[str, Any]) -> str:
    """Expose every chapter arrangement without rewriting the approved guide.

    Exact containment only avoids repeating already embedded manual arrangements;
    no semantic similarity, heading guesses, or deletion from the original text.
    """
    original = guide["manuscript_guide"]
    additions = [f"{row['chapter_id']} — {row['title']}\n{row['writing_arrangement']}"
                 for row in guide["chapters"] if row["writing_arrangement"] not in original]
    return original + ("\n\n全文章节写作安排：\n\n" + "\n\n".join(additions) if additions else "")


def normalize_citation_handles(body: str, known_handles: Sequence[str]) -> str:
    """Only format exact known P#### identities; never guess or renumber them.

    Preserve code, URLs and Markdown links/images. This is presentation repair,
    not evidence validation: even a known handle can support the wrong claim.
    """
    known = {h for h in known_handles if re.fullmatch(r"P[0-9]{4}", h)}
    protected = r"(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`|!?\[[^]\n]*\]\([^\n)]*\)|https?://[^\s<>]+|\[[^\]\n]*\])"
    token = re.compile(r"(?<![A-Za-z0-9_:\[/\-])(?:\[(P[0-9]{4})\]|[（(](P[0-9]{4})[）)]|【(P[0-9]{4})】|［(P[0-9]{4})］|(P[0-9]{4}))(?![A-Za-z0-9_:\]/\-])")
    def replace(match):
        handle = next(group for group in match.groups() if group is not None)
        return "[" + handle + "]" if handle in known else match.group(0)
    pieces = re.split(protected, body)
    return "".join(piece if index % 2 else token.sub(replace, piece) for index, piece in enumerate(pieces))


def build_author_payload(pack: Mapping[str, Any], guide: Mapping[str, Any],
                         chapter: str | Mapping[str, Any], accepted_body_markdown: str,
                         reread_atoms: Sequence[str] = ()) -> dict[str, Any]:
    cid = chapter if isinstance(chapter, str) else chapter["chapter_id"]
    assignment = next((row for row in guide["chapters"] if row["chapter_id"] == cid), None)
    if assignment is None or cid not in pack["chapter_source_handles"]:
        raise CandidateError("guided_chapter_unknown:" + str(cid))
    if not isinstance(accepted_body_markdown, str):
        raise CandidateError("guided_prefix_not_string")
    chosen = _science_ids(pack, [*pack["chapter_source_handles"][cid], *assignment.get("source_handles", [])])
    if reread_atoms:
        chosen += resolve_read_request(pack, {"read_atom_ids": list(reread_atoms)})
    chosen = list(dict.fromkeys(chosen))
    handles = list(dict.fromkeys(pack["atoms"][aid]["source_handle"] for aid in chosen))
    # Preserve which immutable snapshots supplied an object, without exposing
    # backend paths or planning records. Complementary variants remain distinct.
    atoms = [{**{key: (_scientific_tool(pack["atoms"][aid][key], tuple(pack["atoms"][aid]["field_path"]))
                    if key == "value" else deepcopy(pack["atoms"][aid][key])) for key in
              ("atom_id", "source_handle", "field_path", "value", "role")},
              "snapshot_ids": list(dict.fromkeys("snapshot::" + origin["record_id"].split("::", 1)[1][:24]
                  for origin in pack["atoms"][aid]["origins"]))} for aid in chosen]
    materials = {"schema_version": MATERIALS_SCHEMA, "evidence_atoms": atoms,
        "source_identities": {h: {k: deepcopy(v) for k, v in pack["source_identities"][h].items()
            if k not in {"record_ids", "chapter_ids"}} for h in handles},
        "source_aliases": {a: h for a, h in pack["source_aliases"].items() if h in handles},
        "tool_materials": deepcopy(pack["chapter_tool_materials"][cid]),
        "source_navigation": [{"source_handle": h, "title": identity.get("title", ""),
            "doi": identity.get("doi", ""), "year": identity.get("year", ""),
            "atom_count": sum(_is_scientific_atom(pack["atoms"][aid]) for aid in pack["source_atom_ids"][h])}
            for h, identity in pack["source_identities"].items()],
        "read_protocol": {"request": {"read_source_handles": ["exact source_handle"], "read_atom_ids": ["exact atom_id"]},
            "separate_from_prose": True, "unknown_addresses": "error; no fuzzy matching",
            "scientific_fidelity_verified": False}}
    return {"manuscript_guide": render_manuscript_guide(guide), "chapter_assignment": deepcopy(assignment),
        "materials": materials, "accepted_body_markdown": accepted_body_markdown}


def _unwrap(response: Any) -> Any:
    if isinstance(response, Mapping):
        if any(key in response for key in ("body_markdown", "insertions", "read_atom_ids", "read_source_handles")):
            return dict(response)
        for key in ("content", "response", "message"):
            if isinstance(response.get(key), (str, Mapping)):
                return _unwrap(response[key])
        if isinstance(response.get("choices"), list) and response["choices"]:
            return _unwrap(response["choices"][0])
    return response


def _decode(response: Any) -> tuple[dict[str, Any], list[str]]:
    value = _unwrap(response)
    if isinstance(value, Mapping):
        return deepcopy(dict(value)), []
    if not isinstance(value, str):
        return {}, ["guided_response_unreadable"]
    text = re.sub(r"^```(?:json)?\s*\n(.*?)\n```\s*$", r"\1", value.strip(), flags=re.S)
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, Mapping):
        return dict(obj), []
    marker = re.search(r"(?:^|\n)(`{3,}|~{3,})guide_writer_metadata[ \t]*\r?\n", value)
    if marker:
        body, rest = value[:marker.start()], value[marker.end():]
        closing = re.search(r"\r?\n" + re.escape(marker.group(1)) + r"[ \t]*\s*$", rest)
        try:
            meta = json.loads(rest[:closing.start()] if closing else rest)
        except ValueError:
            meta = None
        if closing and isinstance(meta, Mapping):
            return {**meta, "body_markdown": body}, []
        return {"body_markdown": body}, ["guided_metadata_incomplete_or_invalid"]
    if text.startswith("{"):
        match = re.search(r'"body_markdown"\s*:\s*"', text)
        body = _string_prefix(text, match.end() - 1)[0] if match else ""
        return {"body_markdown": body}, ["guided_json_incomplete_or_invalid"]
    return {"body_markdown": value}, ["guided_metadata_missing"]


def parse_guided_response(response: Any) -> dict[str, Any]:
    obj, errors = _decode(response)
    transport = not _transport_status(response)[1]
    if "read_atom_ids" in obj or "read_source_handles" in obj:
        if obj.get("body_markdown") or obj.get("insertions"):
            raise CandidateError("guided_read_mixed_with_prose")
        atoms = _strings(obj.get("read_atom_ids", []), "guided_read_atom_ids")
        sources = _strings(obj.get("read_source_handles", []), "guided_read_source_handles")
        if not (atoms or sources) or len(set(atoms)) != len(atoms) or len(set(sources)) != len(sources):
            raise CandidateError("guided_read_empty_or_duplicated")
        return {"kind": "reread_request", "read_atom_ids": atoms, "read_source_handles": sources,
                "complete": transport and not errors, "transport_complete": transport, "errors": errors}
    body = obj.get("body_markdown", "")
    if not isinstance(body, str) or not body.strip():
        errors.append("guided_body_missing_or_invalid")
        body = body if isinstance(body, str) else ""
    remaining = obj.get("remaining_content", [])
    try:
        remaining = _strings(remaining, "guided_remaining_content")
    except CandidateError:
        errors.append("guided_remaining_content_invalid")
        remaining = []
    if not isinstance(obj.get("complete"), bool):
        errors.append("guided_complete_missing_or_invalid")
    if obj.get("complete") is True and remaining:
        errors.append("guided_complete_with_remaining_content")
    return {"kind": "author", "body_markdown": body, "complete": obj.get("complete") is True and not errors and transport,
            "remaining_content": remaining, "errors": errors, "issues": [{"code": e} for e in errors],
            "transport_complete": transport}


def apply_insertions(original: str, insertions: Any) -> str:
    if not isinstance(insertions, list) or not insertions:
        raise CandidateError("guided_insertions_missing")
    points = []
    for row in insertions:
        if not isinstance(row, Mapping):
            raise CandidateError("guided_insertion_not_object")
        anchor, text = row.get("after_anchor", ""), row.get("text")
        if not isinstance(anchor, str) or not isinstance(text, str) or not text.strip():
            raise CandidateError("guided_insertion_invalid")
        if anchor and original.count(anchor) != 1:
            raise CandidateError("guided_insertion_anchor_must_match_once")
        points.append((original.index(anchor) + len(anchor) if anchor else len(original), text))
    if len({point for point, _ in points}) != len(points):
        raise CandidateError("guided_insertion_duplicate_point")
    result = original
    for point, text in sorted(points, reverse=True):
        result = result[:point] + text + result[point:]
    return result


def parse_completion_response(response: Any, original: str) -> dict[str, Any]:
    obj, errors = _decode(response)
    if errors:
        raise CandidateError(errors[0])
    body = apply_insertions(original, obj.get("insertions"))
    parsed = parse_guided_response({"body_markdown": body, "complete": obj.get("complete"),
        "remaining_content": obj.get("remaining_content", [])})
    # A direct insertion envelope's complete flag describes the manuscript,
    # just as a direct body envelope does; it is not a transport failure.
    status_input = {**response, "body_markdown": body} if isinstance(response, Mapping) and "insertions" in response else response
    transport = not _transport_status(status_input)[1]
    return {**parsed, "kind": "completion", "insertions": deepcopy(obj["insertions"]),
        "complete": parsed["complete"] and transport, "transport_complete": transport, "untouched_prose_preserved": True}
