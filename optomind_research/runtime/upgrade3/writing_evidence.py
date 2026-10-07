"""Deterministic writing evidence, distinct from planning intent.

The archive is lossless; the default model view is an explicit field selection,
not a lossless substitute. Semantic objects (findings with conditions, limits,
etc.) are indivisible. No ranking model, character cutoff, or fact rewriting is
used. Unknown fields default to evidence, so schema evolution fails expansive.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping, Sequence

from .fullbody_contracts import _chapter_rows, _source_records, fullbody_task_catalog
from .writer_candidates_contracts import CandidateError, _handles, _selected_ids, _signature, _relevant_tools

SCHEMA = "optomind.writing_evidence.v1"
PAYLOAD_SCHEMA = "optomind.writing_evidence.payload.v1"
# These are exact, known planning/transport fields, never substring heuristics.
_SOURCE_METADATA = {"source_handle", "title", "year", "doi", "paper_id", "canonical_paper_id",
    "aliases", "locator", "material_read_from_disk"}
_PLANNING_B = {"topic_handles"}
_PLANNING_CHILDREN = {"possible_uses", "purpose", "connection_to_review"}
_TRANSPORT_DEEP = {"_progressive_task_requirements", "_progressive_task_signature",
    "required_outputs", "source_hash", "task_hash", "fulfilled", "reading_mode", "paper_identity"}


def _digest(value: Any) -> str:
    return hashlib.sha256(_signature(value).encode("utf-8")).hexdigest()


def _review_handles(value: Any) -> list[str]:
    """Only typed review mediation creates transitive source dependencies."""
    result: list[str] = []
    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, val in node.items():
                if key in {"review_source_handle", "review_source_handles"}:
                    for handle in _handles({key: val}):
                        if handle not in result:
                            result.append(handle)
                # Provenance never creates evidence authority.
                if key not in {"original_source", "original_sources", "rejected_source", "rejected_sources",
                    "original_source_handle", "original_source_handles", "rejected_source_handle", "rejected_source_handles",
                    "historical_source_handles", "prior_source_handles", "candidate_source_handles"}:
                    visit(val)
        elif isinstance(node, list):
            for item in node:
                visit(item)
    visit(value)
    return result


def _pieces(record: Mapping[str, Any]):
    """Yield field path, intact semantic value, visibility role.

    A known top-level list is split only between objects, never inside a finding.
    B's editorial instructions are separate atoms; their scientific sibling
    values, including boundaries and unknown keys, stay joined as one object.
    """
    for field, value in record.items():
        if field == "material_record_variants":
            continue  # Variants are visited independently with their own origin.
        if field in _SOURCE_METADATA:
            yield (field,), value, "metadata"
        elif field == "deep_read_material_trimmed" and isinstance(value, Mapping) and set(value) <= {"dropped_keys", "dropped_chars", "note"}:
            yield (field,), value, "metadata"
        elif field in {"study_summary_A", "review_planning_B", "deep_read_material"} and isinstance(value, Mapping):
            for key, item in value.items():
                role = "evidence"
                if field == "review_planning_B" and key in _PLANNING_B:
                    role = "planning"
                if field == "review_planning_B" and key == "planning_summary":
                    role = "planner_interpretation"
                if field == "deep_read_material" and key == "questions" and isinstance(item, list) and all(isinstance(q, str) for q in item):
                    role = "metadata"
                if field == "deep_read_material" and key in _TRANSPORT_DEEP:
                    role = "metadata"
                values = list(enumerate(item)) if isinstance(item, list) and item else [(None, item)]
                for index, row in values:
                    path = (field, key) + (() if index is None else (str(index),))
                    if field == "review_planning_B" and key in {"facet_contributions", "broader_review_uses"} and isinstance(row, Mapping):
                        scientific = {k: deepcopy(v) for k, v in row.items() if k not in _PLANNING_CHILDREN}
                        if scientific:
                            yield path, scientific, "evidence"
                        for child in row:
                            if child in _PLANNING_CHILDREN:
                                yield (*path, child), row[child], "planning"
                    else:
                        yield path, row, role
        else:
            # Unknown wrappers retain the complete object, including conditions.
            yield (field,), value, "evidence"


def compile_evidence(book: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the accepted book without mutating it or calling any provider."""
    chapters = _chapter_rows(book)
    tasks = fullbody_task_catalog(book)
    records, identities, aliases = _source_records(chapters)
    atoms: dict[str, Any] = {}
    source_atoms: dict[str, list[str]] = {handle: [] for handle in identities}
    dependencies: dict[str, list[str]] = {handle: [] for handle in identities}
    source_records: dict[str, Any] = {}
    snapshot_records: dict[str, str] = {}
    field_variants: dict[str, set[str]] = {}

    def canonical(handle: str) -> str:
        resolved = aliases.get(handle, handle)
        if resolved not in identities:
            raise CandidateError("evidence_source_missing:" + handle)
        return resolved

    def add_record(record: Mapping[str, Any], parent_id: str | None = None) -> None:
        handle = canonical(record["source_handle"])
        rid = "record::" + _digest(record)
        source_records.setdefault(rid, deepcopy(dict(record)))
        snapshot = "snapshot::" + rid.split("::", 1)[1][:24]
        if snapshot in snapshot_records and snapshot_records[snapshot] != rid:
            raise CandidateError("evidence_snapshot_hash_collision:" + snapshot)
        snapshot_records[snapshot] = rid
        for dependency in _review_handles(record):
            dependency = canonical(dependency)
            if dependency != handle and dependency not in dependencies[handle]:
                dependencies[handle].append(dependency)
        for path, value, role in _pieces(record):
            # List positions are provenance, not scientific identity. Field type
            # remains part of identity; equal prose in distinct roles is not merged.
            semantic_path = tuple(part for part in path if not part.isdecimal())
            aid = "atom::" + _digest([handle, semantic_path, role, value])[:24]
            origin = {"record_id": rid, "field_path": list(path)}
            if parent_id:
                origin["parent_record_id"] = parent_id
            if aid not in atoms:
                atoms[aid] = {"atom_id": aid, "source_handle": handle,
                    "field_path": list(semantic_path), "value": deepcopy(value), "role": role,
                    "origins": [], "variant_group": "field::" + _digest([handle, semantic_path])}
                source_atoms[handle].append(aid)
            elif (atoms[aid]["source_handle"] != handle or atoms[aid]["field_path"] != list(semantic_path)
                  or atoms[aid]["role"] != role or _signature(atoms[aid]["value"]) != _signature(value)):
                raise CandidateError("evidence_atom_hash_collision:" + aid)
            if origin not in atoms[aid]["origins"]:
                atoms[aid]["origins"].append(origin)
            field_variants.setdefault(atoms[aid]["variant_group"], set()).add(aid)
        for variant in record.get("material_record_variants", []):
            add_record(variant, rid)

    for record in records:
        add_record(record)
    for atom in atoms.values():
        atom["multiple_values_in_field"] = len(field_variants[atom["variant_group"]]) > 1
    units = {(ch["chapter_id"], unit["unit_id"]): unit for ch in chapters for unit in ch["units"]}
    tool_materials: dict[str, Any] = {}
    task_tools: dict[str, list[str]] = {}
    chapter_index = {ch["chapter_id"]: ch for ch in chapters}
    task_sources: dict[str, list[str]] = {}
    task_atoms: dict[str, list[str]] = {}
    for tid, task in tasks.items():
        unit = units[task["chapter_id"], task["unit_id"]]
        task_tools[tid] = []
        for tool in _relevant_tools(chapter_index[task["chapter_id"]], [task["unit_id"]]):
            tool_id = "tool::" + _digest(tool)
            tool_materials.setdefault(tool_id, deepcopy(tool))
            task_tools[tid].append(tool_id)
        owner = unit.get("owner_unit_context", {})
        # Supporting-study catalog is broad unit navigation, not a task assignment.
        # All other owner conditions/cases retain their explicit dependencies.
        owner_required = {k: v for k, v in owner.items() if k != "supporting_studies"} if isinstance(owner, Mapping) else owner
        primary = _handles(task["task"])
        if not primary:
            primary = _handles({"source_handles": unit.get("source_handles", [])})
        pending = list(dict.fromkeys(primary + _handles(owner_required)))
        selected: list[str] = []
        while pending:
            handle = canonical(pending.pop(0))
            if handle in selected:
                continue
            selected.append(handle)
            pending.extend(dependencies[handle])
        task_sources[tid] = selected
        task_atoms[tid] = [aid for h in selected for aid in source_atoms[h] if atoms[aid]["role"] in {"evidence", "planner_interpretation"}]
    return {"schema_version": SCHEMA, "book_sha256": book.get("book_sha256", ""),
        "canonical_content_sha256": _digest(book), "canonical_book": deepcopy(dict(book)),
        "tasks": deepcopy(tasks), "chapter_order": [ch["chapter_id"] for ch in chapters],
        "chapter_frames": {ch["chapter_id"]: deepcopy(ch.get("chapter_frame", {})) for ch in chapters},
        "unit_contexts": {ch["chapter_id"]: {u["unit_id"]: {k: deepcopy(v) for k, v in u.items()
            if k not in {"paragraph_tasks", "table_tasks", "source_handles"}} for u in ch["units"]} for ch in chapters},
        "atoms": atoms, "source_records": source_records, "snapshot_record_ids": snapshot_records, "source_identities": identities,
        "source_aliases": aliases, "source_atom_ids": source_atoms, "review_dependencies": dependencies,
        "task_source_handles": task_sources, "task_atom_ids": task_atoms,
        "tool_materials": tool_materials, "task_tool_ids": task_tools,
        "selection_policy": {"default": "task_sources_plus_explicit_review_mediation",
            "omitted_roles": ["planning", "metadata"], "unknown_fields": "preserved_as_evidence",
            "deduplication": "exact_value_same_canonical_source_and_semantic_field",
            "scientific_fidelity_verified": False}}


def evidence_payload(pack: Mapping[str, Any], task_ids: Sequence[str] | None = None,
                     atom_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Return full current intent and evidence; explicit atoms enable full reread.

    Explicit atom IDs may name any archived source (and planning/metadata atom),
    so a navigator can reread outside the initial task scope without fake citation
    authority. Invalid or repeated IDs fail before a model call.
    """
    if pack.get("schema_version") != SCHEMA:
        raise CandidateError("evidence_schema_invalid")
    selected = _selected_ids(pack["tasks"], task_ids)
    if atom_ids is None:
        chosen = list(dict.fromkeys(aid for tid in selected for aid in pack["task_atom_ids"][tid]))
    else:
        if isinstance(atom_ids, (str, bytes)) or not isinstance(atom_ids, Sequence):
            raise CandidateError("evidence_atom_ids_not_sequence")
        chosen = list(atom_ids)
        if any(not isinstance(aid, str) or aid not in pack["atoms"] for aid in chosen):
            raise CandidateError("evidence_atom_unknown")
        if len(set(chosen)) != len(chosen):
            raise CandidateError("evidence_atom_duplicated")
    handles = list(dict.fromkeys(pack["atoms"][aid]["source_handle"] for aid in chosen))
    chapters = list(dict.fromkeys(pack["tasks"][tid]["chapter_id"] for tid in selected))
    units: dict[str, Any] = {}
    for tid in selected:
        task = pack["tasks"][tid]
        cid, uid = task["chapter_id"], task["unit_id"]
        units.setdefault(cid, {})[uid] = deepcopy(pack["unit_contexts"][cid][uid])
    book = pack["canonical_book"]
    # Compact navigation labels are stable for this immutable book across all
    # task windows. The archive's content-addressed snapshot IDs remain intact.
    snapshot_labels = {snapshot: "s" + str(index + 1).zfill(3)
        for index, snapshot in enumerate(sorted(pack["snapshot_record_ids"]))}
    used_snapshots = list(dict.fromkeys("snapshot::" + origin["record_id"].split("::", 1)[1][:24]
        for aid in chosen for origin in pack["atoms"][aid]["origins"]))
    return {"schema_version": PAYLOAD_SCHEMA, "book_sha256": pack["book_sha256"],
        **{key: deepcopy(book.get(key, "")) for key in ("language", "research_question", "review_argument",
            "user_request", "target_reader", "review_scope", "missing_original_context_fields")},
        "editable_task_ids": selected, "tasks": {tid: deepcopy(pack["tasks"][tid]) for tid in selected},
        "chapter_frames": {cid: deepcopy(pack["chapter_frames"][cid]) for cid in chapters},
        "unit_contexts": units,
        "chapter_roles": [{"chapter_id": cid, **{k: deepcopy(v) for k, v in pack["chapter_frames"][cid].items()
            if k in {"chapter_title", "chapter_purpose", "chapter_scope", "chapter_thesis", "chapter_argument", "reader_objective", "title", "purpose", "scope", "thesis", "argument"}}}
            for cid in pack["chapter_order"]],
        "evidence_atoms": [{**{k: deepcopy(atom[k]) for k in ("atom_id", "source_handle", "field_path", "value", "role")},
            "snapshot_ids": list(dict.fromkeys(snapshot_labels["snapshot::" + o["record_id"].split("::", 1)[1][:24]] for o in atom["origins"]))}
            for atom in (pack["atoms"][aid] for aid in chosen)],
        "snapshot_index": {snapshot_labels[snapshot]: snapshot for snapshot in used_snapshots},
        "tool_materials": [{"tool_id": tool_id, "value": deepcopy(pack["tool_materials"][tool_id])}
            for tool_id in dict.fromkeys(tool_id for tid in selected for tool_id in pack["task_tool_ids"][tid])]
            if atom_ids is None or chosen else [],
        "source_identities": {h: {k: deepcopy(v) for k, v in pack["source_identities"][h].items()
            if k not in {"record_ids", "chapter_ids"}} for h in handles},
        "source_aliases": {a: h for a, h in pack["source_aliases"].items() if h in handles},
        "reread_source_ids": {h: pack["source_identities"][h]["source_id"] for h in pack["source_identities"]},
        "selection": {"atom_count": len(chosen), "task_source_handles": {tid: pack["task_source_handles"][tid] for tid in selected},
            "explicit_selection": atom_ids is not None, "archive_complete": True, "intent_is_not_scientific_evidence": True,
            "scientific_fidelity_verified": False}}


def protected_atom_ids(pack: Mapping[str, Any], chosen_atom_ids: Sequence[str]) -> list[str]:
    """Carry design, limits and review attribution with selected scientific facts.

    Protection is conservative across complementary snapshots: it never selects
    a favored version. Changed source context remains separately identified.
    """
    if isinstance(chosen_atom_ids, (str, bytes)) or not isinstance(chosen_atom_ids, Sequence):
        raise CandidateError("evidence_atom_ids_not_sequence")
    if any(not isinstance(aid, str) or aid not in pack["atoms"] for aid in chosen_atom_ids):
        raise CandidateError("evidence_atom_unknown")
    handles = list(dict.fromkeys(pack["atoms"][aid]["source_handle"] for aid in chosen_atom_ids))
    pending = list(handles)
    mediated: set[str] = set()
    while pending:
        handle = pending.pop(0)
        for dependency in pack["review_dependencies"].get(handle, []):
            if dependency not in handles:
                handles.append(dependency)
                mediated.add(dependency)
                pending.append(dependency)
    result: list[str] = []
    for handle in handles:
        for aid in pack["source_atom_ids"][handle]:
            atom = pack["atoms"][aid]
            path = atom["field_path"]
            protected = (handle in mediated and atom["role"] in {"evidence", "planner_interpretation"}) or (
                len(path) > 1 and path[0] == "study_summary_A" and path[1] not in
                {"key_findings", "work_summary", "problem_or_question"}) or (
                len(path) > 1 and path[0] == "review_planning_B" and path[1] == "scope_interpretation_cautions") or (
                path[0] in {"card_material", "material_depth", "material_status", "review_source_handle", "review_source_handles",
                            "evidence_origin", "evidence_level", "limitations", "conditions"})
            if protected and aid not in chosen_atom_ids:
                result.append(aid)
    return result


def select_atoms(pack: Mapping[str, Any], task_ids: Sequence[str],
                 chosen_atom_ids: Sequence[str]) -> dict[str, Any]:
    """Validate exact choices and explicitly add their inseparable context."""
    # Validate the unmodified request first, including duplicates and task IDs.
    payload = evidence_payload(pack, task_ids, chosen_atom_ids)
    protected = protected_atom_ids(pack, chosen_atom_ids)
    if protected:
        payload = evidence_payload(pack, task_ids, [*chosen_atom_ids, *protected])
    payload["selection"]["requested_atom_ids"] = list(chosen_atom_ids)
    payload["selection"]["protected_atom_ids"] = protected
    return payload
