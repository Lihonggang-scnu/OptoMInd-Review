"""Outline-to-guide contracts. Structural checks, never scientific judging.

The initial model view contains intent and navigation, not the science pool.
On-demand reading sends whole source packets, with all conditions and variants.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Mapping, Sequence

from .guided_body_contracts import (validate_guide, compile_guided_materials,
    _canonical_source, _is_scientific_atom, _scientific_tool, _strings)
from .writing_evidence import _digest
from .writer_candidates_contracts import CandidateError

SCHEMA = "optomind.guide_maker_input.v1"
PAYLOAD_SCHEMA = "optomind.guide_maker_payload.v1"
# These are material/archive wrappers, not outline requirements. All other
# intent fields, including unknown fields, tasks and tables, remain untouched.
_ARCHIVE_FIELDS = {"sources", "source_identities", "source_aliases", "source_catalog",
    "chapter_tool_materials", "tool_materials", "input_manifest", "provenance",
    "book_sha256", "chapter_sha256"}
_CLUES = {"content_clue", "content_clues", "short_description", "keywords", "topic", "topics"}
_TYPES = {"source_type", "study_type", "publication_type", "evidence_type"}


def _intent(book: Mapping[str, Any]) -> dict[str, Any]:
    # Only strip recognized archive fields at their actual book/chapter level.
    # A task may legitimately discuss "sources", "provenance" or "tool_materials";
    # recursive key deletion would erase that writing requirement.
    result = {k: deepcopy(v) for k, v in book.items() if k not in _ARCHIVE_FIELDS}
    result["chapters"] = [{k: deepcopy(v) for k, v in chapter.items()
                           if k not in _ARCHIVE_FIELDS} for chapter in book["chapters"]]
    return result


def _identity(pack: Mapping[str, Any], handle: str) -> dict[str, Any]:
    identity = {k: deepcopy(v) for k, v in pack["source_identities"][handle].items()
                if k not in {"record_ids", "chapter_ids"}}
    records = [r for r in pack["source_records"].values()
               if _canonical_source(pack, r["source_handle"]) == handle]
    for field in ("title", "doi", "year", "paper_id", "canonical_paper_id"):
        if field not in identity:
            values = [r[field] for r in records if r.get(field) is not None]
            identity[field] = deepcopy(values[0]) if values else None
            if any(v != values[0] for v in values[1:]):
                identity.setdefault("identity_metadata_variants", {})[field] = deepcopy(values)
    return identity


def compile_guide_input(book: Mapping[str, Any], feedback: Any = None) -> dict[str, Any]:
    pack = compile_guided_materials(book)
    outline = _intent(book)
    # This normalized index is an exact duplicate of chapter task wrappers.
    # Retain nonidentical caller-provided indexes rather than silently losing data.
    if book.get("task_catalog") == pack["tasks"]:
        outline.pop("task_catalog", None)
    catalog = []
    for handle in pack["source_identities"]:
        records = [r for r in pack["source_records"].values()
                   if _canonical_source(pack, r["source_handle"]) == handle]
        clues, types = [], []
        for record in records:
            # Only dedicated existing navigation fields. Never promote a full
            # finding list, A/B record, role or abstract into this index.
            for key in sorted(_CLUES | _TYPES):
                if key in record:
                    item = {"field": key, "value": deepcopy(record[key])}
                    destination = types if key in _TYPES else clues
                    if item not in destination:
                        destination.append(item)
            summary = record.get("study_summary_A")
            if isinstance(summary, Mapping):
                for key in ("paper_kind", "work_summary", "research_scope"):
                    if key in summary:
                        item = {"field": "study_summary_A." + key, "value": deepcopy(summary[key])}
                        destination = types if key == "paper_kind" else clues
                        if item not in destination:
                            destination.append(item)
        tasks = [tid for tid, hs in pack["task_source_handles"].items() if handle in hs]
        if any(c["field"] == "study_summary_A.work_summary" for c in clues):
            clues = [c for c in clues if c["field"] == "study_summary_A.work_summary"]
        elif any(c["field"] == "study_summary_A.research_scope" for c in clues):
            clues = [c for c in clues if c["field"] == "study_summary_A.research_scope"]
        for tid in tasks if not clues else []:
            for use in pack["tasks"][tid]["task"].get("source_uses", []):
                if isinstance(use, Mapping) and use.get("source_handle") and _canonical_source(pack, use["source_handle"]) == handle and "use" in use:
                    item = {"field": "source_uses.use", "value": deepcopy(use["use"])}
                    if item not in clues:
                        clues.append(item)
        units = list(dict.fromkeys((pack["tasks"][t]["chapter_id"], pack["tasks"][t]["unit_id"]) for t in tasks))
        catalog.append({"source_handle": handle, "identity": _identity(pack, handle),
            "source_types": types or None, "content_clues": clues or None,
            "chapter_ids": deepcopy(pack["source_identities"][handle]["chapter_ids"]),
            "unit_links": [{"chapter_id": c, "unit_id": u} for c, u in units],
            "review_source_handles": deepcopy(pack["review_dependencies"][handle])})
    tool_records = {}
    for chapter in book["chapters"]:
        for raw in chapter.get("chapter_tool_materials", []):
            science = _scientific_tool(raw)
            key = _digest(science)
            entry = tool_records.setdefault(key, {"science": science, "chapter_ids": []})
            if chapter["chapter_id"] not in entry["chapter_ids"]:
                entry["chapter_ids"].append(chapter["chapter_id"])
    tool_archive, tool_catalog = {}, []
    for index, (_, entry) in enumerate(sorted(tool_records.items()), 1):
        handle = "tool_" + str(index).zfill(3)
        science = entry["science"]
        tool_archive[handle] = science
        sources = science.get("sources", [])
        statuses = list(dict.fromkeys(source.get("identity_status") for source in sources
            if isinstance(source, Mapping))) if isinstance(sources, list) else []
        tool_catalog.append({"tool_handle": handle, "title": science.get("title"),
            "question": science.get("question"), "chapter_ids": entry["chapter_ids"],
            "source_identity_statuses": statuses or None})
    result = {"schema_version": SCHEMA, "full_outline": outline, "source_catalog": catalog,
              "tool_catalog": tool_catalog, "tool_archive": tool_archive,
              "science_archive": pack, "input_sha256": _digest(book)}
    if feedback is not None:
        result["feedback"] = deepcopy(feedback)
    return result


def build_maker_payload(bundle: Mapping[str, Any], prior_guide: Any = None,
                        needs: Sequence[Any] = (), materials: Sequence[Any] = (),
                        read_history: Sequence[Any] = (), feedback: Any = None) -> dict[str, Any]:
    result = {"schema_version": PAYLOAD_SCHEMA,
        "full_outline": deepcopy(bundle["full_outline"]),
        "source_catalog": deepcopy(bundle["source_catalog"]),
        "tool_catalog": deepcopy(bundle.get("tool_catalog", [])),
        "prior_guide": deepcopy(prior_guide), "reading_needs": deepcopy(list(needs)),
        "materials": deepcopy(list(materials)), "read_history": deepcopy(list(read_history))}
    feedback = bundle.get("feedback") if feedback is None else feedback
    if feedback is not None:
        result["feedback"] = deepcopy(feedback)
    return result


def _needs(bundle: Mapping[str, Any], needs: Any) -> list[dict[str, Any]]:
    if not isinstance(needs, list):
        raise CandidateError("maker_reading_needs_not_list")
    pack, result, seen = bundle["science_archive"], [], set()
    for need in needs:
        if not isinstance(need, Mapping) or set(need) - {"need_id", "question", "source_handles", "atom_ids", "reread_reason", "tool_handles"}:
            raise CandidateError("maker_need_invalid_keys")
        nid = need.get("need_id")
        if not isinstance(nid, str) or not re.fullmatch(r"N[1-9][0-9]{0,5}", nid) or nid in seen:
            raise CandidateError("maker_need_id_invalid_or_duplicated")
        seen.add(nid)
        if not isinstance(need.get("question"), str) or not need["question"].strip():
            raise CandidateError("maker_need_question_missing")
        if "reread_reason" in need and (not isinstance(need["reread_reason"], str) or not need["reread_reason"].strip()):
            raise CandidateError("maker_reread_reason_invalid")
        handles = _strings(need.get("source_handles", []), "maker_need_source_handles")
        canonical = [_canonical_source(pack, h) for h in handles]
        if len(set(canonical)) != len(canonical):
            raise CandidateError("maker_need_sources_duplicated")
        atoms = _strings(need.get("atom_ids", []), "maker_need_atom_ids")
        tools = _strings(need.get("tool_handles", []), "maker_need_tool_handles")
        if len(set(tools)) != len(tools):
            raise CandidateError("maker_need_tools_duplicated")
        for tool in tools:
            if tool not in bundle.get("tool_archive", {}):
                raise CandidateError("maker_tool_unknown:" + tool)
        if not handles and not atoms and not tools:
            raise CandidateError("maker_need_sources_empty")
        if len(set(atoms)) != len(atoms):
            raise CandidateError("maker_need_atoms_duplicated")
        for aid in atoms:
            if aid not in pack["atoms"]:
                raise CandidateError("maker_atom_unknown:" + aid)
            atom = pack["atoms"][aid]
            if not _is_scientific_atom(atom):
                raise CandidateError("maker_atom_not_scientific:" + aid)
            if canonical and atom["source_handle"] not in canonical:
                raise CandidateError("maker_atom_source_mismatch:" + aid)
        result.append(deepcopy(dict(need)))
    return result


def parse_maker_response(response: Any, book: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    value, transport = response, True
    while isinstance(value, Mapping) and "guide" not in value:
        if value.get("finish_reason") in {"length", "max_tokens", "max_output_tokens", "content_filter", "error", "cancelled"} or value.get("error") or value.get("complete") is False:
            transport = False
        if isinstance(value.get("choices"), list) and value["choices"]:
            value = value["choices"][0]
        else:
            nested = next((value[k] for k in ("content", "response", "message") if isinstance(value.get(k), (str, Mapping))), None)
            if nested is None:
                break
            value = nested
    if isinstance(value, str):
        text = re.sub(r"^```(?:json)?\s*\n(.*?)\n```\s*$", r"\1", value.strip(), flags=re.S)
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise CandidateError("maker_response_invalid_json") from exc
    if not isinstance(value, Mapping) or set(value) != {"guide", "reading_needs", "complete", "changes"}:
        raise CandidateError("maker_response_invalid_keys")
    guide = validate_guide(value["guide"], book)
    for chapter in guide["chapters"]:
        for handle in chapter.get("source_handles", []):
            _canonical_source(bundle["science_archive"], handle)
    errors = []
    try:
        needs = _needs(bundle, value["reading_needs"])
    except CandidateError as exc:
        # Keep the valid provisional guide, but never execute any member of an
        # invalid request set. The runtime stops for correction, not auto-repair.
        needs = []
        errors.append(str(exc))
    if not isinstance(value["complete"], bool):
        raise CandidateError("maker_complete_not_boolean")
    if value["complete"] and needs:
        raise CandidateError("maker_complete_has_pending_needs")
    changes = _strings(value["changes"], "maker_changes")
    return {"guide": guide, "reading_needs": needs, "complete": value["complete"] and transport and not errors,
        "changes": changes, "transport_complete": transport,
        "errors": errors + ([] if transport else ["maker_transport_incomplete"])}


def resolve_material_requests(bundle: Mapping[str, Any], needs: Sequence[Any]) -> list[dict[str, Any]]:
    validated = _needs(bundle, list(needs))
    pack, selected, selected_tools = bundle["science_archive"], {}, {}
    for need in validated:
        for handle in need.get("tool_handles", []):
            selected_tools.setdefault(handle, []).append(need)
        pending = [_canonical_source(pack, h) for h in need.get("source_handles", [])]
        pending.extend(pack["atoms"][aid]["source_handle"] for aid in need.get("atom_ids", []))
        visited = set()
        while pending:
            handle = pending.pop(0)
            if handle in visited:
                continue
            visited.add(handle)
            selected.setdefault(handle, []).append(need)
            pending.extend(pack["review_dependencies"][handle])
    result = []
    for handle, requested in selected.items():
        atoms = [{k: (_scientific_tool(a[k], tuple(a["field_path"])) if k == "value" else deepcopy(a[k]))
                  for k in ("atom_id", "source_handle", "field_path", "value", "role", "origins")}
                 for aid in pack["source_atom_ids"][handle] if _is_scientific_atom(a := pack["atoms"][aid])]
        # Per-snapshot science retains semantic objects and all complementary
        # variants. Paths are original addresses, never a sentence extraction.
        records = []
        for rid, record in pack["source_records"].items():
            if _canonical_source(pack, record["source_handle"]) != handle:
                continue
            atom_ids = [a["atom_id"] for a in atoms if any(o["record_id"] == rid for o in a["origins"])]
            records.append({"record_id": rid, "source_handle": handle, "atom_ids": atom_ids})
        packet = {"packet_kind": "source", "source_handle": handle, "source_identity": _identity(pack, handle),
            "evidence_atoms": atoms, "scientific_records": records,
            "review_source_handles": deepcopy(pack["review_dependencies"][handle])}
        packet["packet_sha256"] = _digest(packet)
        packet["need_ids"] = [n["need_id"] for n in requested]
        packet["questions"] = [n["question"] for n in requested]
        packet["reread_reasons"] = [n.get("reread_reason") for n in requested]
        result.append(packet)
    for handle, requested in selected_tools.items():
        packet = {"packet_kind": "tool", "source_handle": handle,
            "scientific_tool": deepcopy(bundle["tool_archive"][handle])}
        packet["packet_sha256"] = _digest(packet)
        packet["need_ids"] = [n["need_id"] for n in requested]
        packet["questions"] = [n["question"] for n in requested]
        packet["reread_reasons"] = [n.get("reread_reason") for n in requested]
        result.append(packet)
    return result
