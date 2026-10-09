"""Build a complete offline navigation pack for supplied-but-uncited sources.

The source list comes from FINAL_CITATION_AUDIT.json.  Every matching atom in
the actual author MESSAGES.json payloads is retained, including repeated
chapter material and study_summary_A/review_planning_B variant fields.  This
script never edits LIVE or contacts a model/network service.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "records"
LIVE = ROOT / "LIVE"
AUDIT_PATH = RECORDS / "FINAL_CITATION_AUDIT.json"
INPUT_PATH = next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json"))


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_json(value: Any) -> str:
    return sha_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    book = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    aliases = book.get("source_aliases") or {}
    canonical = lambda handle: aliases.get(handle, handle)
    omitted = sorted(audit.get("supplied_not_cited") or [])

    atoms_by_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    navigation_by_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chapter_by_handle: dict[str, set[str]] = defaultdict(set)
    semantic_chapter_by_handle: dict[str, set[str]] = defaultdict(set)
    navigation_chapter_by_handle: dict[str, set[str]] = defaultdict(set)
    message_paths_by_handle: dict[str, set[str]] = defaultdict(set)

    for index in range(1, 8):
        stage = LIVE / "stages" / f"author_{index:03d}"
        message_path = next(stage.glob("*/attempt_001/MESSAGES.json"))
        messages = json.loads(message_path.read_text(encoding="utf-8"))
        payload = json.loads(messages[1]["content"])
        chapter_id = f"Ch{index}"
        materials = payload["materials"]

        # Preserve every evidence atom exactly, while attaching the author
        # message location needed for later inspection.
        for atom in materials.get("evidence_atoms", []):
            raw_handle = str(atom.get("source_handle") or "")
            handle = canonical(raw_handle)
            if handle not in omitted:
                continue
            row = dict(atom)
            row["author_chapter_id"] = chapter_id
            row["author_messages_path"] = str(message_path.resolve())
            row["raw_source_handle"] = raw_handle
            atoms_by_handle[handle].append(row)
            chapter_by_handle[handle].add(chapter_id)
            first_field = str(row.get("field_path", [""])[0])
            if first_field.startswith(("study_summary_A", "review_planning_B")):
                semantic_chapter_by_handle[handle].add(chapter_id)
            message_paths_by_handle[handle].add(str(message_path.resolve()))

        # source_navigation is the complete local navigation pool sent to
        # the author.  Retain matching entries with their full fields.
        for item in materials.get("source_navigation", []):
            raw_handle = str(item.get("source_handle") or "")
            handle = canonical(raw_handle)
            if handle not in omitted:
                continue
            row = dict(item)
            row["author_chapter_id"] = chapter_id
            row["author_messages_path"] = str(message_path.resolve())
            row["raw_source_handle"] = raw_handle
            navigation_by_handle[handle].append(row)
            chapter_by_handle[handle].add(chapter_id)
            navigation_chapter_by_handle[handle].add(chapter_id)
            message_paths_by_handle[handle].add(str(message_path.resolve()))

    entries: list[dict[str, Any]] = []
    missing_atoms: list[str] = []
    missing_navigation: list[str] = []
    source_identities = book.get("source_identities") or {}
    for handle in omitted:
        identity = dict(source_identities.get(handle) or {})
        atoms = atoms_by_handle.get(handle, [])
        navigation = navigation_by_handle.get(handle, [])
        if not atoms:
            missing_atoms.append(handle)
        if not navigation:
            missing_navigation.append(handle)

        planning_atoms = [
            atom for atom in atoms
            if str(atom.get("field_path", [""])[0]).startswith("review_planning_B")
        ]
        finding_atoms = [
            atom for atom in atoms
            if str(atom.get("field_path", [""])[0]).startswith("study_summary_A")
            and str(atom.get("field_path", [""])[-1]) == "key_findings"
        ]
        semantic_atoms = [
            atom for atom in atoms
            if str(atom.get("field_path", [""])[0]).startswith(("study_summary_A", "review_planning_B"))
        ]
        entry = {
            "source_handle": handle,
            "source_id": identity.get("source_id", f"source::{handle}"),
            "title": identity.get("title", ""),
            "doi": identity.get("doi", ""),
            "paper_id": identity.get("paper_id", ""),
            "aliases": identity.get("aliases", []),
            "canonical_source_identity": canonical(handle),
            "all_evidence_atom_chapter_ids": sorted(chapter_by_handle.get(handle, set())),
            "semantic_chapter_ids": sorted(semantic_chapter_by_handle.get(handle, set())),
            "navigation_chapter_ids": sorted(navigation_chapter_by_handle.get(handle, set())),
            "author_messages_paths": sorted(message_paths_by_handle.get(handle, set())),
            "source_navigation_occurrences": len(navigation),
            "evidence_atom_occurrences": len(atoms),
            "planning_summary_atom_occurrences": len(planning_atoms),
            "key_findings_atom_occurrences": len(finding_atoms),
            "semantic_field_paths": sorted({".".join(map(str, atom.get("field_path", []))) for atom in semantic_atoms}),
            "planning_summary": [
                {"field_path": atom.get("field_path", []), "value": atom.get("value"), "atom_id": atom.get("atom_id"), "chapter_id": atom.get("author_chapter_id")}
                for atom in planning_atoms
            ],
            "key_findings": [
                {"field_path": atom.get("field_path", []), "value": atom.get("value"), "atom_id": atom.get("atom_id"), "chapter_id": atom.get("author_chapter_id")}
                for atom in finding_atoms
            ],
            "source_navigation": navigation,
            "evidence_atoms": atoms,
        }
        entry["entry_sha256"] = sha_json(entry)
        entries.append(entry)

    output = {
        "schema_version": "guided_body_compact_auto_metadata.unused_source_navigation.v1",
        "citation_audit_path": str(AUDIT_PATH.resolve()),
        "citation_audit_body_sha256": audit.get("body_bytes_sha256"),
        "input_path": str(INPUT_PATH.resolve()),
        "input_sha256": sha_bytes(INPUT_PATH.read_bytes()),
        "selection_rule": "FINAL_CITATION_AUDIT.supplied_not_cited; canonicalized through input source_aliases",
        "supplied_not_cited_count": len(omitted),
        "missing_from_author_evidence_atoms": missing_atoms,
        "missing_from_author_source_navigation": missing_navigation,
        "entry_count": len(entries),
        "total_evidence_atom_occurrences": sum(len(entry["evidence_atoms"]) for entry in entries),
        "total_source_navigation_occurrences": sum(len(entry["source_navigation"]) for entry in entries),
        "entries": entries,
    }
    out_path = RECORDS / "UNUSED_SOURCE_NAVIGATION.json"
    write_json(out_path, output)

    lines = [
        "# Supplied-but-uncited source navigation",
        "",
        "This is an offline navigation index from FINAL_CITATION_AUDIT.supplied_not_cited.",
        "Full planning_summary/key_findings values and every matching author evidence atom are in UNUSED_SOURCE_NAVIGATION.json.",
        "The index does not judge scientific quality and does not imply that an omitted source should be added.",
        "",
        f"- Sources: {len(entries)}",
        f"- Evidence atom occurrences retained: {output['total_evidence_atom_occurrences']}",
        f"- Source-navigation occurrences retained: {output['total_source_navigation_occurrences']}",
        f"- Missing evidence atoms: {len(missing_atoms)}; missing navigation entries: {len(missing_navigation)}",
        f"- Input/body audit SHA: {audit.get('body_bytes_sha256')}",
        "",
    ]
    for entry in entries:
        lines.extend([
            f"## {entry['source_handle']} — {entry['title']}",
            f"- source_id: {entry['source_id']} | semantic chapters: {', '.join(entry['semantic_chapter_ids']) or 'none'}",
            f"- navigation chapters: {', '.join(entry['navigation_chapter_ids']) or 'none'}",
            f"- DOI: {entry['doi'] or 'missing'} | paper_id: {entry['paper_id'] or 'missing'}",
            f"- evidence atoms: {entry['evidence_atom_occurrences']} | source navigation: {entry['source_navigation_occurrences']}",
            f"- planning_summary atoms: {entry['planning_summary_atom_occurrences']} | key_findings atoms: {entry['key_findings_atom_occurrences']}",
            f"- semantic fields: {', '.join(entry['semantic_field_paths']) or 'none'}",
            f"- entry_sha256: {entry['entry_sha256']}",
            "- Full values: UNUSED_SOURCE_NAVIGATION.json",
            "",
        ])
    (RECORDS / "UNUSED_SOURCE_NAVIGATION.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "entry_count": len(entries),
        "total_evidence_atom_occurrences": output["total_evidence_atom_occurrences"],
        "total_source_navigation_occurrences": output["total_source_navigation_occurrences"],
        "missing_atoms": missing_atoms,
        "missing_navigation": missing_navigation,
        "json_path": str(out_path.resolve()),
        "markdown_path": str((RECORDS / "UNUSED_SOURCE_NAVIGATION.md").resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
