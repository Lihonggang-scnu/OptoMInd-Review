"""Deterministic unified full-manuscript handoff package and metadata repair.

This module builds a metadata-only handoff envelope for the eight enhanced
chapter assets.  It reads an explicit manifest (project root + sections with
enhanced asset dirs, authoritative input packets, and optional old drafts),
validates the required enhanced files, repairs metadata (title recovery,
section-id reconciliation, REF-marker diagnostics) without rewriting chapter
prose or evidence, and emits:

- ``UNIFIED_MANUSCRIPT_HANDOFF.json``
- ``HANDOFF_METADATA_REPAIR_REPORT.json``

Paths inside ``project_root`` are stored project-relative; the input
fingerprint is computed from file contents, schema, section order, repair
notes, and aggregate counts, so the same content under another root produces
the same fingerprint.  The authoritative input packet stays core evidence and
the explanatory citation ledger stays ``background_explanation_only``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "optomind.full_manuscript_handoff.v2"
UNIFIED_HANDOFF_JSON = "UNIFIED_MANUSCRIPT_HANDOFF.json"
REPAIR_REPORT_JSON = "HANDOFF_METADATA_REPAIR_REPORT.json"

REQUIRED_ENHANCED_FILES = (
    "ENHANCED_CHAPTER.md",
    "CHAPTER_ARGUMENT_PLAN.json",
    "CLAIM_TO_PARAGRAPH_MAP.json",
    "EXPLANATION_BLOCKS.json",
    "EXPLANATORY_CITATION_LEDGER.json",
    "ENHANCEMENT_REPORT.json",
)
OPTIONAL_ENHANCED_FILES = (
    "BLOCK_SCIENTIFIC_REVIEW.json",
    "LEGACY_GAP_AUDIT.json",
)

REF_MARKER_PATTERN = re.compile(r"\[REF:([^\]]+)\]")


class HandoffBuildError(RuntimeError):
    """Raised when the handoff package cannot be built safely."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_relative(path: Path, project_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(project_root.resolve())
        return relative.as_posix()
    except ValueError:
        return str(path.resolve())


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HandoffBuildError(
            f"{label}: cannot read/parse {path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise HandoffBuildError(
            f"{label}: expected a JSON object, got {type(payload).__name__}: "
            f"{path}"
        )
    return dict(payload)


def _resolve(spec_path: str, project_root: Path) -> Path:
    path = Path(spec_path)
    if not path.is_absolute():
        path = project_root / path
    return path


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load and validate the handoff manifest."""

    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise HandoffBuildError(f"missing manifest: {manifest_path}")
    manifest = _read_json(manifest_path, "manifest")
    if manifest.get("schema_version") != "optomind.full_manuscript_handoff.manifest.v2":
        raise HandoffBuildError(
            "manifest schema is incompatible; rebuild the full planned-section manifest"
        )
    raw_project_root = str(manifest.get("project_root") or "")
    project_root_path = Path(raw_project_root)
    if not project_root_path.is_absolute():
        project_root_path = manifest_path.parent / project_root_path
    project_root = project_root_path.resolve()
    if not project_root.is_dir():
        raise HandoffBuildError(
            f"manifest project_root is not a directory: {project_root}"
        )
    raw_sections = manifest.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise HandoffBuildError("manifest sections must be a non-empty list")
    sections: list[dict[str, Any]] = []
    seen_section_ids: set[str] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            raise HandoffBuildError(
                f"manifest section {index} must be an object"
            )
        section_id = str(raw.get("section_id") or "").strip()
        content_status = str(raw.get("content_status") or "enhanced").strip()
        enhanced_asset_dir = str(raw.get("enhanced_asset_dir") or "").strip()
        authoritative_input_packet = str(
            raw.get("authoritative_input_packet") or ""
        ).strip()
        if not section_id:
            raise HandoffBuildError(
                f"manifest section {index} requires section_id"
            )
        if content_status == "enhanced" and (
            not enhanced_asset_dir or not authoritative_input_packet
        ):
            raise HandoffBuildError(
                f"manifest enhanced section {index} requires enhanced_asset_dir "
                "and authoritative_input_packet"
            )
        if section_id in seen_section_ids:
            raise HandoffBuildError(
                f"duplicate section_id in manifest: {section_id}"
            )
        seen_section_ids.add(section_id)
        source_old_draft = str(raw.get("source_old_draft") or "").strip()
        sections.append(
            {
                "section_id": section_id,
                "content_status": content_status,
                "enhanced_asset_dir": _resolve(
                    enhanced_asset_dir, project_root
                ),
                "authoritative_input_packet": _resolve(
                    authoritative_input_packet, project_root
                ),
                "source_old_draft": (
                    _resolve(source_old_draft, project_root)
                    if source_old_draft
                    else None
                ),
                "provided_title": str(raw.get("title") or "").strip(),
                "failure": dict(raw.get("failure") or {}),
            }
        )
    return {
        "manifest_path": manifest_path,
        "project_root": project_root,
        "sections": sections,
    }


def _file_record(path: Path, project_root: Path) -> dict[str, Any]:
    return {
        "path": _project_relative(path, project_root),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _section_id_of(data: Mapping[str, Any]) -> str | None:
    value = data.get("section_id")
    return str(value).strip() if value else None


def _recover_title(
    *,
    provided_title: str,
    argument_plan: Mapping[str, Any],
    enhanced_text: str,
) -> tuple[str, dict[str, Any] | None]:
    plan = argument_plan.get("plan") if isinstance(
        argument_plan.get("plan"), Mapping
    ) else {}
    if provided_title:
        return provided_title, None
    for candidate in (
        str(plan.get("title") or "").strip(),
        str(argument_plan.get("section_title") or "").strip(),
    ):
        if candidate:
            return candidate, {
                "source": "chapter_argument_plan",
                "title": candidate,
            }
    match = re.match(r"^#\s+(.+?)\s*$", enhanced_text, re.M)
    if match:
        title = match.group(1).strip()
        return title, {
            "source": "enhanced_chapter_heading",
            "title": title,
        }
    return "", {
        "source": "unrecoverable",
        "title": "",
        "note": "no title found in manifest, argument plan, or chapter heading",
    }


def _word_count(text: str) -> int:
    return len(str(text or "").split())


def _ref_markers(text: str) -> list[str]:
    return REF_MARKER_PATTERN.findall(str(text or ""))


def _core_paper_identities(packet: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()
    for entry in packet.get("evidence_packets") or []:
        if isinstance(entry, Mapping) and entry.get("paper_id"):
            identities.add(str(entry["paper_id"]))
    coverage = packet.get("literature_coverage") or {}
    if isinstance(coverage, Mapping):
        for source in coverage.get("sources") or []:
            if isinstance(source, Mapping) and source.get("paper_id"):
                identities.add(str(source["paper_id"]))
    provenance = packet.get("manuscript_context") or {}
    if isinstance(provenance, Mapping):
        for value in provenance.get("evidence_provenance") or {}.values():
            if isinstance(value, Mapping) and value.get("paper_id"):
                identities.add(str(value["paper_id"]))
    return identities


def _ledger_identities(ledger: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()

    # Ledger records may carry the citation marker on a nested application or
    # provenance object rather than on the record itself.  Walk only
    # structured values and collect identity-bearing fields; do not treat
    # arbitrary explanatory text as an identity.
    identity_keys = (
        "marker_id",
        "marker",
        "ref",
        "citation",
        "citation_id",
        "id",
        "handle",
        "paper_id",
        "doi",
    )

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in identity_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    identities.add(candidate.strip())
            metadata = value.get("metadata") or {}
            if isinstance(metadata, Mapping):
                for key in ("paper_id", "doi"):
                    candidate = metadata.get(key)
                    if candidate:
                        identities.add(str(candidate).strip())
            for child in value.values():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)

    visit(ledger.get("records") or [])
    return identities


def _ref_identity_candidates(marker: str) -> set[str]:
    candidates = {marker}
    if ":" in marker:
        head, tail = marker.rsplit(":", 1)
        if re.fullmatch(r"[A-Za-z]\d[\w.-]*|S\d+-C[\w.-]*|c\d+[\w.-]*", tail):
            candidates.add(head)
    return candidates


def _diagnose_ref_markers(
    markers: Sequence[str],
    core_identities: set[str],
    ledger_identities: set[str],
) -> list[str]:
    allowed = core_identities | ledger_identities
    hard_defects: list[str] = []
    for marker in markers:
        if not (_ref_identity_candidates(marker) & allowed):
            hard_defects.append(f"unknown_ref_marker:{marker}")
    return hard_defects


def _build_section_envelope(
    section: Mapping[str, Any],
    *,
    project_root: Path,
    repair_notes: list[str],
    hard_defects: list[str],
) -> dict[str, Any]:
    section_id = str(section["section_id"])
    content_status = str(section.get("content_status") or "enhanced")
    if content_status != "enhanced":
        raw_draft = section.get("source_old_draft")
        draft_path = Path(raw_draft) if raw_draft else None
        draft_text = (
            draft_path.read_text(encoding="utf-8")
            if draft_path is not None and draft_path.is_file()
            else ""
        )
        if content_status == "raw_fallback":
            if draft_path is None or not draft_text.strip():
                raise HandoffBuildError(
                    f"section {section_id}: raw fallback draft is missing or empty"
                )
            packet_path = Path(section["authoritative_input_packet"])
            if not packet_path.is_file() or packet_path.stat().st_size == 0:
                raise HandoffBuildError(
                    f"section {section_id}: raw fallback requires a non-empty "
                    "authoritative input packet"
                )
            return {
                "section_id": section_id,
                "section_title": str(section.get("provided_title") or ""),
                "content_status": content_status,
                "chapter_status": content_status,
                "word_count": _word_count(draft_text),
                # The original draft is the read-only source text for every
                # downstream consumer.  Its field name stays compatible with
                # the enhanced path contract; provenance records the fallback.
                "enhanced_chapter": _file_record(draft_path, project_root),
                "authoritative_input_packet": _file_record(
                    packet_path, project_root
                ),
                "optional_file_status": {},
                "provenance": {
                    "source_old_draft_path": _project_relative(
                        draft_path, project_root
                    ),
                    "failure": dict(section.get("failure") or {}),
                    "fallback_warning": (
                        "enhancement failed; original section draft retained"
                    ),
                },
                "hard_defects": [],
            }
        return {
            "section_id": section_id,
            "section_title": str(section.get("provided_title") or ""),
            "content_status": content_status,
            "chapter_status": content_status,
            "word_count": _word_count(draft_text),
            "enhanced_chapter": {},
            "authoritative_input_packet": {},
            "optional_file_status": {},
            "provenance": {
                "source_old_draft_path": (
                    _project_relative(draft_path, project_root)
                    if draft_path is not None and draft_path.is_file()
                    else None
                ),
                "failure": dict(section.get("failure") or {}),
            },
            "hard_defects": ["section_content_unavailable"],
        }
    asset_dir = Path(section["enhanced_asset_dir"])
    if not asset_dir.is_dir():
        raise HandoffBuildError(
            f"section {section_id}: enhanced_asset_dir missing: {asset_dir}"
        )
    required_paths = {
        name: asset_dir / name for name in REQUIRED_ENHANCED_FILES
    }
    for name, path in required_paths.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise HandoffBuildError(
                f"section {section_id}: required {name} missing or empty: "
                f"{path}"
            )
    packet_path = Path(section["authoritative_input_packet"])
    if not packet_path.is_file() or packet_path.stat().st_size == 0:
        raise HandoffBuildError(
            f"section {section_id}: authoritative input packet missing or "
            f"empty: {packet_path}"
        )

    enhanced_text = required_paths["ENHANCED_CHAPTER.md"].read_text(
        encoding="utf-8"
    )
    argument_plan = _read_json(
        required_paths["CHAPTER_ARGUMENT_PLAN.json"],
        f"section {section_id} CHAPTER_ARGUMENT_PLAN",
    )
    claim_to_paragraph = _read_json(
        required_paths["CLAIM_TO_PARAGRAPH_MAP.json"],
        f"section {section_id} CLAIM_TO_PARAGRAPH_MAP",
    )
    explanation_blocks = _read_json(
        required_paths["EXPLANATION_BLOCKS.json"],
        f"section {section_id} EXPLANATION_BLOCKS",
    )
    ledger = _read_json(
        required_paths["EXPLANATORY_CITATION_LEDGER.json"],
        f"section {section_id} EXPLANATORY_CITATION_LEDGER",
    )
    enhancement_report = _read_json(
        required_paths["ENHANCEMENT_REPORT.json"],
        f"section {section_id} ENHANCEMENT_REPORT",
    )
    packet_data = _read_json(
        packet_path, f"section {section_id} authoritative input packet"
    )

    for label, data in (
        ("CHAPTER_ARGUMENT_PLAN", argument_plan),
        ("CLAIM_TO_PARAGRAPH_MAP", claim_to_paragraph),
        ("EXPLANATION_BLOCKS", explanation_blocks),
        ("EXPLANATORY_CITATION_LEDGER", ledger),
        ("ENHANCEMENT_REPORT", enhancement_report),
        ("authoritative input packet", packet_data),
    ):
        file_section_id = _section_id_of(data)
        if file_section_id and file_section_id != section_id:
            raise HandoffBuildError(
                f"section_id mismatch: manifest {section_id} vs "
                f"{label}.section_id={file_section_id} (no guessing)"
            )

    title, title_repair = _recover_title(
        provided_title=str(section.get("provided_title") or ""),
        argument_plan=argument_plan,
        enhanced_text=enhanced_text,
    )
    if title_repair:
        repair_notes.append(
            f"{section_id}: title recovered from "
            f"{title_repair.get('source')}"
        )
    report_word_count = (
        enhancement_report.get("word_counts") or {}
    ).get("enhanced")
    word_count = (
        int(report_word_count)
        if isinstance(report_word_count, int) and report_word_count > 0
        else _word_count(enhanced_text)
    )
    chapter_status = str(enhancement_report.get("status") or "unknown")

    core_identities = _core_paper_identities(packet_data)
    ledger_identities = _ledger_identities(ledger)
    marker_defects = _diagnose_ref_markers(
        _ref_markers(enhanced_text), core_identities, ledger_identities
    )
    hard_defects.extend(
        f"{section_id}:{defect}" for defect in marker_defects
    )

    reviewer_notes: dict[str, Any] | None = None
    optional_status: dict[str, str] = {}
    for name in OPTIONAL_ENHANCED_FILES:
        path = asset_dir / name
        if path.is_file() and path.stat().st_size > 0:
            optional_status[name] = "present"
            if name == "BLOCK_SCIENTIFIC_REVIEW.json":
                reviewer_notes = {
                    **_file_record(path, project_root),
                    "status": "present",
                }
        else:
            optional_status[name] = "missing_fail_open"
            repair_notes.append(
                f"{section_id}: optional {name} missing (fail-open)"
            )

    return {
        "section_id": section_id,
        "content_status": "enhanced",
        "section_title": title,
        "chapter_status": chapter_status,
        "word_count": word_count,
        "enhanced_chapter": _file_record(
            required_paths["ENHANCED_CHAPTER.md"], project_root
        ),
        "argument_plan": _file_record(
            required_paths["CHAPTER_ARGUMENT_PLAN.json"], project_root
        ),
        "claim_to_paragraph_map": _file_record(
            required_paths["CLAIM_TO_PARAGRAPH_MAP.json"], project_root
        ),
        "explanation_blocks": _file_record(
            required_paths["EXPLANATION_BLOCKS.json"], project_root
        ),
        "explanatory_citation_ledger": {
            **_file_record(
                required_paths["EXPLANATORY_CITATION_LEDGER.json"],
                project_root,
            ),
            "trust_boundary": "background_explanation_only",
        },
        "enhancement_report": _file_record(
            required_paths["ENHANCEMENT_REPORT.json"], project_root
        ),
        "authoritative_input_packet": {
            **_file_record(packet_path, project_root),
            "role": "core_evidence",
        },
        "reviewer_notes": reviewer_notes,
        "legacy_gap_audit": (
            _file_record(asset_dir / "LEGACY_GAP_AUDIT.json", project_root)
            if optional_status.get("LEGACY_GAP_AUDIT.json") == "present"
            else None
        ),
        "optional_file_status": optional_status,
        "provenance": {
            "source_enhanced_asset_dir": _project_relative(
                asset_dir, project_root
            ),
            "source_old_draft_path": (
                _project_relative(
                    Path(section["source_old_draft"]), project_root
                )
                if section.get("source_old_draft")
                else None
            ),
            "title_repair": title_repair,
            "core_evidence_role": "authoritative_input_packet",
            "explanatory_trust_boundary": "background_explanation_only",
        },
        "hard_defects": marker_defects,
    }


def build_full_manuscript_handoff(
    *,
    manifest_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Build the unified handoff package and metadata repair report."""

    manifest = load_manifest(manifest_path)
    project_root = manifest["project_root"]
    sections = manifest["sections"]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    repair_notes: list[str] = []
    hard_defects: list[str] = []
    envelopes: dict[str, dict[str, Any]] = {}
    for section in sections:
        envelopes[str(section["section_id"])] = _build_section_envelope(
            section,
            project_root=project_root,
            repair_notes=repair_notes,
            hard_defects=hard_defects,
        )

    aggregate_counts = {
        "section_count": len(sections),
        "total_word_count": sum(
            envelope["word_count"] for envelope in envelopes.values()
        ),
        "hard_defect_count": len(hard_defects),
        "optional_missing_count": sum(
            1
            for envelope in envelopes.values()
            for status in envelope["optional_file_status"].values()
            if status == "missing_fail_open"
        ),
        "title_repair_count": sum(
            1
            for envelope in envelopes.values()
            if envelope["provenance"].get("title_repair")
        ),
    }
    section_order = [str(section["section_id"]) for section in sections]
    input_fingerprint = fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "section_order": section_order,
            "sections": {
                section_id: {
                    "title": envelope["section_title"],
                    "chapter_status": envelope["chapter_status"],
                    "content_status": envelope.get("content_status"),
                    "word_count": envelope["word_count"],
                    "files": sorted(
                        (key, record["sha256"])
                        for key, record in envelope.items()
                        if isinstance(record, Mapping)
                        and "sha256" in record
                    ),
                }
                for section_id, envelope in sorted(envelopes.items())
            },
            "repair_notes": sorted(repair_notes),
            "hard_defects": sorted(hard_defects),
            "aggregate_counts": aggregate_counts,
        }
    )

    package = {
        "schema_version": SCHEMA_VERSION,
        "input_manifest": _project_relative(
            manifest["manifest_path"], project_root
        ),
        "input_fingerprint": input_fingerprint,
        "section_order": section_order,
        "sections": envelopes,
        "aggregate_counts": aggregate_counts,
        "repair_notes": sorted(repair_notes),
        "hard_defects": sorted(hard_defects),
    }
    repair_report = {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "sections": {
            section_id: {
                "repair_notes": [
                    note
                    for note in repair_notes
                    if note.startswith(f"{section_id}:")
                ],
                "hard_defects": envelope["hard_defects"],
                "title_repair": envelope["provenance"].get("title_repair"),
            }
            for section_id, envelope in sorted(envelopes.items())
        },
        "aggregate_counts": aggregate_counts,
    }

    unified_path = output_dir / UNIFIED_HANDOFF_JSON
    repair_path = output_dir / REPAIR_REPORT_JSON
    reused = False
    if unified_path.is_file():
        try:
            existing = _read_json(unified_path, "existing unified handoff")
            reused = (
                existing.get("input_fingerprint") == input_fingerprint
                and existing.get("schema_version") == SCHEMA_VERSION
            )
        except HandoffBuildError:
            reused = False
    if reused:
        if not repair_path.is_file():
            repair_path.write_text(
                json.dumps(repair_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    else:
        unified_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        existing_repair = (
            _read_json(repair_path, "existing repair report")
            if repair_path.is_file()
            else {}
        )
        if (
            not repair_path.is_file()
            or existing_repair.get("input_fingerprint") != input_fingerprint
        ):
            repair_path.write_text(
                json.dumps(repair_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "input_manifest": _project_relative(
            manifest["manifest_path"], project_root
        ),
        "input_fingerprint": input_fingerprint,
        "reused": reused,
        "output_paths": {
            "unified_handoff": str(unified_path),
            "metadata_repair_report": str(repair_path),
        },
        "section_order": section_order,
        "aggregate_counts": aggregate_counts,
        "hard_defects": sorted(hard_defects),
    }


__all__ = [
    "SCHEMA_VERSION",
    "UNIFIED_HANDOFF_JSON",
    "REPAIR_REPORT_JSON",
    "REQUIRED_ENHANCED_FILES",
    "OPTIONAL_ENHANCED_FILES",
    "HandoffBuildError",
    "canonical_json",
    "fingerprint",
    "load_manifest",
    "build_full_manuscript_handoff",
]
