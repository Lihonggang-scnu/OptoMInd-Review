"""Free audit of every guide writing-unit author input.

This is an evidence helper for the content-units acceptance run.  It uses the
same ``validate_guide`` -> ``compile_guided_materials`` ->
``build_author_payload`` path as ``guided_body_writer.py`` and deliberately
passes an explicit empty preceding BODY snapshot for every unit.  It does not
call a provider and makes no continuity claim.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


def _sha(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_handles(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_handle" and isinstance(item, str) and item:
                found.add(item)
            found.update(_source_handles(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_source_handles(item))
    return found


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worktree", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--guide", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--tokenizer", required=True)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    worktree = Path(args.worktree).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    guide_path = Path(args.guide).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    sys.path.insert(0, str(worktree))

    from scripts.upgrade3.evidence_body_writer import load_body_manifest
    from optomind_research.runtime.upgrade3.guided_body_contracts import (
        build_author_payload,
        compile_guided_materials,
        _scientific_tool,
        validate_guide,
    )
    from optomind_research.runtime.upgrade3.guided_content_units import (
        resolve_content_basis, resolve_content_contexts,
    )
    from optomind_research.runtime.upgrade3.guided_body_writer import _messages
    from scripts.upgrade3.writer_candidates import tokenizer_counter

    book, prepared = load_body_manifest(str(manifest_path))
    guide_raw = json.loads(guide_path.read_text(encoding="utf-8"))
    normalized = validate_guide(guide_raw, book)
    pack = compile_guided_materials(book)
    writer_prompt = (worktree / "prompts" / "guided_body_writer" / "writer.md").read_text(encoding="utf-8")
    counter, meter = tokenizer_counter(args.tokenizer)

    # The maker exposes short C0001-style addresses; the runtime normalizes
    # those to immutable canonical task ids before constructing author input.
    expected_task_ids = list(pack["tasks"])
    expected_task_set = set(expected_task_ids)
    assigned_task_ids: list[str] = []
    short_task_ids: list[str] = []
    raw_units = []
    for raw_chapter in guide_raw.get("chapters", []):
        raw_units.extend(raw_chapter.get("writing_units") or [])
        for raw_unit in raw_chapter.get("writing_units") or []:
            short_task_ids.extend(raw_unit.get("content_task_ids", []))
    recovered_unit_ids = [
        unit["unit_id"]
        for chapter in normalized["chapters"]
        for unit in chapter.get("writing_units", [])
        if str(unit.get("unit_id", "")).startswith("retained_")
    ]

    payload_root = output / "author_payloads"
    records = []
    errors = []
    unit_total = 0
    for chapter_index, chapter in enumerate(normalized["chapters"], 1):
        units = chapter.get("writing_units") or [chapter]
        for unit_index, unit in enumerate(units, 1):
            unit_total += 1
            assignment = deepcopy(unit)
            assignment["chapter_id"] = chapter["chapter_id"]
            suffix = f"{chapter_index:03d}"
            if chapter.get("writing_units"):
                suffix += f"_unit_{unit_index:03d}"
                assignment.update(
                    chapter_title=chapter["title"],
                    unit_position={"index": unit_index, "count": len(units)},
                    sibling_units=[
                        {"unit_id": sibling["unit_id"], "title": sibling["title"],
                         "writing_arrangement": sibling["writing_arrangement"]}
                        for sibling in units
                    ],
                )
            stage_id = f"author_{suffix}"
            snapshot = {
                "kind": "explicit_empty_preceding_body",
                "accepted_body_markdown": "",
                "continuity_claim": False,
            }
            try:
                payload = build_author_payload(pack, normalized, assignment, "")
                messages = _messages(writer_prompt, payload)
                basis_expected = resolve_content_basis(assignment, pack)
                contexts_expected = resolve_content_contexts(assignment, pack)
                basis_exact = payload["chapter_assignment"].get("content_basis", []) == basis_expected
                contexts_exact = payload["chapter_assignment"].get("content_unit_contexts", {}) == contexts_expected
                canonical_ids = list(assignment.get("content_task_ids", []))
                assigned_task_ids.extend(canonical_ids)
                # Every exact source-use/table/unknown scientific field is
                # compared through the full task wrapper, while source proof
                # is checked separately against the immutable evidence atoms.
                source_union = []
                missing_source_evidence = []
                missing_context_source_evidence = []
                source_identity_mismatches = []
                missing_scientific_atoms = []
                atom_value_mismatches = []
                supplied_atoms = {row.get("atom_id"): row for row in payload["materials"].get("evidence_atoms", [])}
                supplied_sources = set(payload["materials"].get("source_identities", {}))
                for task_id in canonical_ids:
                    required_sources = list(pack["task_source_handles"].get(task_id, []))
                    source_union.extend(required_sources)
                    absent_sources = [handle for handle in required_sources if handle not in supplied_sources]
                    if absent_sources:
                        missing_source_evidence.append({"task_id": task_id, "source_handles": absent_sources})
                    for handle in required_sources:
                        expected_identity = {
                            key: deepcopy(value)
                            for key, value in pack["source_identities"].get(handle, {}).items()
                            if key not in {"record_ids", "chapter_ids"}
                        }
                        if payload["materials"].get("source_identities", {}).get(handle) != expected_identity:
                            source_identity_mismatches.append(handle)
                    for atom_id in pack["task_atom_ids"].get(task_id, []):
                        full_atom = pack["atoms"][atom_id]
                        if full_atom.get("role") != "evidence":
                            continue
                        supplied = supplied_atoms.get(atom_id)
                        if supplied is None:
                            missing_scientific_atoms.append(atom_id)
                            continue
                        expected_value = _scientific_tool(
                            full_atom.get("value"), tuple(full_atom.get("field_path", ())))
                        expected_atom = {**full_atom, "value": expected_value}
                        for key in ("source_handle", "field_path", "value", "role"):
                            if supplied.get(key) != expected_atom.get(key):
                                atom_value_mismatches.append({"atom_id": atom_id, "field": key})
                context_source_handles = sorted({
                    pack["source_aliases"].get(handle, handle)
                    for handle in _source_handles(contexts_expected)
                })
                missing_context_source_evidence = [handle for handle in context_source_handles if handle not in supplied_sources]
                stage_dir = payload_root / stage_id
                _dump(stage_dir / "PAYLOAD.json", payload)
                _dump(stage_dir / "MESSAGES.json", messages)
                expected_tasks = list(assignment.get("content_task_ids", []))
                basis_tasks = [row.get("task", {}).get("task_id") for row in payload["chapter_assignment"].get("content_basis", [])]
                context_ids = sorted(payload["chapter_assignment"].get("content_unit_contexts", {}).keys())
                records.append({
                    "stage_id": stage_id,
                    "chapter_id": chapter["chapter_id"],
                    "chapter_index": chapter_index,
                    "unit_index": unit_index,
                    "unit_id": assignment.get("unit_id"),
                    "title": assignment.get("title"),
                    "content_task_ids": expected_tasks,
                    "canonical_content_task_ids": canonical_ids,
                    "content_basis_task_ids": [row.get("task_id") for row in payload["chapter_assignment"].get("content_basis", [])],
                    "content_basis_exact_full_wrappers": basis_exact,
                    "content_basis_count": len(payload["chapter_assignment"].get("content_basis", [])),
                    "content_unit_context_ids": context_ids,
                    "content_unit_contexts_exact": contexts_exact,
                    "source_union": sorted(set(source_union)),
                    "missing_source_evidence_by_task": missing_source_evidence,
                    "context_source_union": context_source_handles,
                    "missing_source_evidence_by_context": missing_context_source_evidence,
                    "source_identity_mismatches": sorted(set(source_identity_mismatches)),
                    "missing_scientific_atom_ids": missing_scientific_atoms,
                    "scientific_atom_value_mismatches": atom_value_mismatches,
                    "materials": {
                        "evidence_atoms": len(payload["materials"].get("evidence_atoms", [])),
                        "source_identities": len(payload["materials"].get("source_identities", {})),
                        "source_aliases": len(payload["materials"].get("source_aliases", {})),
                        "tool_materials": len(payload["materials"].get("tool_materials", [])),
                        "source_navigation": len(payload["materials"].get("source_navigation", [])),
                    },
                    "accepted_body_snapshot": snapshot,
                    "author_payload_sha256": _sha(payload),
                    "author_messages_sha256": _sha(messages),
                    "payload_utf8_bytes": len(messages[1]["content"].encode("utf-8")),
                    "messages_utf8_bytes": len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                    "tokenizer_user_tokens": counter(b"", [messages[1]]),
                    "tokenizer_messages_tokens": counter(b"", messages),
                    "payload_path": str(stage_dir / "PAYLOAD.json"),
                    "messages_path": str(stage_dir / "MESSAGES.json"),
                    "provider_calls": 0,
                })
            except Exception as exc:  # preserve all unit failures in the report
                errors.append({"stage_id": stage_id, "chapter_id": chapter["chapter_id"],
                               "unit_id": assignment.get("unit_id"),
                               "error": type(exc).__name__ + ": " + str(exc)})

    report = {
        "schema_version": "optomind.guide_content_units_author_supply_check.v1",
        "status": "pending",
        "provider_calls": 0,
        "continuity_claim": False,
        "preceding_body_policy": "explicit_empty_for_every_unit",
        "constructor": {
            "module": "optomind_research.runtime.upgrade3.guided_body_contracts",
            "function": "build_author_payload",
            "signature": "build_author_payload(pack, guide, chapter_or_unit, accepted_body_markdown, reread_atoms=())",
            "supporting_constructors": ["validate_guide", "compile_guided_materials"],
            "message_builder": "optomind_research.runtime.upgrade3.guided_body_writer._messages",
        },
        "worktree": str(worktree),
        "manifest": str(manifest_path),
        "prepared_manifest_sha256": _sha_file(manifest_path),
        "prepared_input_hash": _sha(prepared),
        "guide": str(guide_path),
        "guide_sha256": _sha_file(guide_path),
        "tokenizer": meter,
        "chapters": len(normalized["chapters"]),
        "writing_units_expected": unit_total,
        "writing_units_constructed": len(records),
        "recovered_unit_count": len(recovered_unit_ids),
        "recovered_unit_ids": recovered_unit_ids,
        "raw_writing_unit_count": len(raw_units),
        "short_content_task_id_count": len(short_task_ids),
        "short_to_canonical_id_count": len(assigned_task_ids),
        "short_to_canonical_ids_exactly_once": len(assigned_task_ids) == len(expected_task_ids) and set(assigned_task_ids) == expected_task_set and len(assigned_task_ids) == len(set(assigned_task_ids)),
        "canonical_content_task_ids_missing": [tid for tid in expected_task_ids if tid not in assigned_task_ids],
        "canonical_content_task_ids_duplicate": sorted({tid for tid in assigned_task_ids if assigned_task_ids.count(tid) > 1}),
        "all_full_task_wrappers_exact": all(row.get("content_basis_exact_full_wrappers") for row in records),
        "all_original_contexts_exact": all(row.get("content_unit_contexts_exact") for row in records),
        "all_source_evidence_present": all(not row.get("missing_source_evidence_by_task") for row in records),
        "all_context_source_evidence_present": all(not row.get("missing_source_evidence_by_context") for row in records),
        "all_source_identities_exact": all(not row.get("source_identity_mismatches") for row in records),
        "all_scientific_atoms_present_and_exact": all(not row.get("missing_scientific_atom_ids") and not row.get("scientific_atom_value_mismatches") for row in records),
        "records": records,
        "errors": errors,
        "output_root": str(output),
    }
    report["status"] = "pass" if (
        not errors and len(records) == unit_total
        and report["short_to_canonical_ids_exactly_once"]
        and report["all_full_task_wrappers_exact"]
        and report["all_original_contexts_exact"]
        and report["all_source_evidence_present"]
        and report["all_context_source_evidence_present"]
        and report["all_source_identities_exact"]
        and report["all_scientific_atoms_present_and_exact"]
    ) else "blocked"
    _dump(output / "AUTHOR_SUPPLY_CHECK.json", report)
    print(json.dumps({k: report[k] for k in ("status", "provider_calls", "continuity_claim", "writing_units_expected", "writing_units_constructed", "errors")}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
