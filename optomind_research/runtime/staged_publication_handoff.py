"""Deterministic staged-publication handoff builder.

This module combines the accepted reviewed body with newer staged conclusion,
introduction, and abstract artifacts, the publication metadata catalog, the
commander's structural work order, and the final visual package into the
existing ``research_harness.content_package.v1`` contract consumed by
``latex_publication_renderer.py``.

The implementation is deliberately model-free and network-free.  Upstream
scientific prose is immutable: only the three front-matter sections are
replaced, and the only body presentation cleanup is removing a duplicate
consecutive visible chapter heading (``## Title`` immediately followed by
``# Title``).  Citation identity mismatches in front matter fail closed.
Missing or rejected visual assets fail open with an audit, while unsafe path
escapes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "research_harness.content_package.v1"
CONTENT_PACKAGE_FILENAME = "REVIEW_CONTENT_PACKAGE.json"
FINAL_REVIEW_FILENAME = "FINAL_REVIEW_EN.md"
FINAL_VISUAL_PACKAGE_FILENAME = "FINAL_VISUAL_PACKAGE.json"
VISUAL_ASSETS_DIRNAME = "visual_assets"
METADATA_CATALOG_FILENAME = "PUBLICATION_METADATA_CATALOG.json"
METADATA_AUDIT_FILENAME = "PUBLICATION_METADATA_AUDIT.json"
PUBLICATION_METADATA_FILENAME = "publication_metadata.json"
REVIEW_BLUEPRINT_FILENAME = "REVIEW_BLUEPRINT.json"
SECTION_SOURCE_LEDGER_RELPATH = (
    "section_coverage/sections/ALL/SECTION_SOURCE_LEDGER.json"
)

EXPECTED_SCIENTIFIC_IDS = tuple(f"S{index:02d}" for index in range(1, 9))
FRONT_SECTION_TITLES = ("Abstract", "Introduction")
CONCLUSION_TITLE_PREFIXES = ("conclusion",)

REF_MARKER_PATTERN = re.compile(r"\[REF:([^\]]+)\]")
HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
H1_TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s*$")
SAFE_FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}

ACCEPTED_REVIEW_DECISIONS = {
    "human_approved",
    "timeout_accepted_for_draft",
    "system_approved_test_mode",
    "system_approved_test_mode_with_warnings",
}

PLACEHOLDER_VALUES = {
    "",
    "1900",
    "unknown",
    "metadata pending",
    "authors not recovered",
    "authors not recovered in chapter snapshot",
    "publication metadata pending",
}


class StagedPublicationHandoffError(RuntimeError):
    """Raised when the handoff cannot be produced safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _clean_newlines(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _clean_phrase(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_title(value: Any) -> str:
    return _clean_phrase(value).casefold()


def _posix_relative(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _read_json_any(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StagedPublicationHandoffError(
            f"{label}: cannot read/parse JSON: {path}: {exc}"
        ) from exc


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_json_any(path, label)
    if not isinstance(value, Mapping):
        raise StagedPublicationHandoffError(
            f"{label}: expected a JSON object, got {type(value).__name__}: {path}"
        )
    return dict(value)


def _resolve_input_path(raw: str | Path, project_root: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _require_input_file(
    raw: str | Path | None,
    *,
    project_root: Path,
    label: str,
) -> Path:
    if raw is None or str(raw).strip() == "":
        raise StagedPublicationHandoffError(f"{label}: path is required")
    path = _resolve_input_path(str(raw), project_root)
    if not path.is_file():
        raise StagedPublicationHandoffError(f"{label}: file not found: {path}")
    return path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _is_placeholder(value: Any) -> bool:
    text = " ".join(str(value or "").split()).strip().casefold()
    return text in PLACEHOLDER_VALUES or text.startswith("evidence record ")


def _clean_metadata_scalar(value: Any) -> str:
    text = _clean_phrase(value)
    return "" if _is_placeholder(text) else text


def _clean_authors(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_authors = [value]
    elif isinstance(value, list):
        raw_authors = []
        for item in value:
            if isinstance(item, str):
                raw_authors.append(item)
            elif isinstance(item, Mapping):
                name = item.get("name") or item.get("author") or ""
                if name:
                    raw_authors.append(str(name))
            else:
                raw_authors.append(str(item))
    else:
        raw_authors = []
    cleaned: list[str] = []
    for author in raw_authors:
        name = _clean_metadata_scalar(author)
        if name:
            cleaned.append(name)
    return cleaned


def _clean_year(value: Any) -> str:
    text = _clean_phrase(value)
    if not text or text == "1900":
        return ""
    return text


def _sanitize_record_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(record)
    cleaned["title"] = _clean_metadata_scalar(record.get("title"))
    cleaned["authors"] = _clean_authors(record.get("authors"))
    cleaned["year"] = _clean_year(record.get("year"))
    cleaned["venue"] = _clean_metadata_scalar(record.get("venue"))
    return cleaned


def _extract_ref_markers(text: str) -> list[str]:
    return [
        match.strip()
        for match in REF_MARKER_PATTERN.findall(_clean_newlines(text))
        if match.strip()
    ]


def _parse_staged_draft(path: Path, label: str) -> str:
    artifact = _read_json_object(path, label)
    payload = artifact.get("payload")
    payload = payload if isinstance(payload, Mapping) else artifact
    draft = payload.get("draft")
    draft = draft if isinstance(draft, Mapping) else payload
    text = draft.get("text") if isinstance(draft, Mapping) else payload.get("text")
    if text is None:
        text = artifact.get("text")
    cleaned = _clean_newlines(text).strip()
    if not cleaned:
        raise StagedPublicationHandoffError(
            f"{label}: payload.draft.text is missing or empty: {path}"
        )
    return cleaned


def _parse_reviewed_manuscript(path: Path) -> dict[str, Any]:
    text = _clean_newlines(path.read_text(encoding="utf-8"))
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_title is not None:
            sections.append((current_title, current_lines))

    for line in lines:
        match = HEADING_PATTERN.match(line)
        if match:
            flush()
            current_title = match.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()

    abstract: list[tuple[str, str]] = []
    introduction: list[tuple[str, str]] = []
    conclusion: list[tuple[str, str]] = []
    scientific: list[tuple[str, str]] = []
    for title, content_lines in sections:
        content = "\n".join(content_lines).strip()
        normalized = _normalize_title(title)
        if normalized == "abstract":
            abstract.append((title, content))
        elif normalized == "introduction":
            introduction.append((title, content))
        elif any(normalized.startswith(prefix) for prefix in CONCLUSION_TITLE_PREFIXES):
            conclusion.append((title, content))
        else:
            scientific.append((title, content))

    if len(abstract) != 1 or len(introduction) != 1 or len(conclusion) != 1:
        raise StagedPublicationHandoffError(
            "reviewed manuscript must contain exactly one Abstract, "
            "one Introduction, and one Conclusion heading"
        )
    if len(scientific) != len(EXPECTED_SCIENTIFIC_IDS):
        raise StagedPublicationHandoffError(
            "reviewed manuscript must contain exactly "
            f"{len(EXPECTED_SCIENTIFIC_IDS)} scientific sections, "
            f"found {len(scientific)}"
        )

    return {
        "text": text,
        "abstract_title": abstract[0][0],
        "introduction_title": introduction[0][0],
        "conclusion_title": conclusion[0][0],
        "scientific": scientific,
    }


def _dedupe_scientific_content(content: str, title: str) -> str:
    lines = _clean_newlines(content).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        match = H1_TITLE_PATTERN.match(lines[0].strip())
        if match and _normalize_title(match.group(1)) == _normalize_title(title):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _section_items_from_raw(raw: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if isinstance(raw, str):
        value = raw.strip()
        if value:
            items.append({"section_id": value, "title": ""})
        return items
    if isinstance(raw, Mapping):
        raw = raw.get("sections", raw)
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, Mapping):
                section_id = str(
                    value.get("section_id") or value.get("id") or key
                ).strip()
                title = str(value.get("title") or value.get("section_title") or "").strip()
            else:
                section_id = str(key).strip()
                title = str(value or "").strip()
            if section_id:
                items.append({"section_id": section_id, "title": title})
        return items
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if isinstance(entry, str):
            section_id = entry.strip()
            if section_id:
                items.append({"section_id": section_id, "title": ""})
        elif isinstance(entry, Mapping):
            section_id = str(
                entry.get("section_id") or entry.get("id") or ""
            ).strip()
            title = str(
                entry.get("title")
                or entry.get("section_title")
                or entry.get("section")
                or ""
            ).strip()
            if section_id:
                items.append({"section_id": section_id, "title": title})
    return items


def _payload_of(data: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = data.get("payload")
    if isinstance(payload, Mapping):
        return payload
    structure = data.get("structure")
    if isinstance(structure, Mapping):
        return structure
    return data


def _load_section_order(
    commander_path: Path | None,
    section_source_path: Path | None,
) -> tuple[list[str], dict[str, str], str]:
    order_items: list[dict[str, str]] = []
    title_by_id: dict[str, str] = {}
    thesis = ""

    if section_source_path is not None:
        source = _read_json_any(section_source_path, "structured section source")
        order_items = _section_items_from_raw(source)
        for item in order_items:
            if item["title"]:
                title_by_id[item["section_id"]] = item["title"]

    if commander_path is not None:
        commander = _read_json_object(commander_path, "commander work order")
        payload = _payload_of(commander)
        raw_order = (
            payload.get("section_order")
            or payload.get("proposed_section_order")
            or payload.get("section_ids")
            or commander.get("section_order")
            or commander.get("proposed_section_order")
            or commander.get("section_ids")
        )
        if raw_order is not None:
            order_items = _section_items_from_raw(raw_order)
        for key in ("central_thesis", "review_thesis", "thesis"):
            for source in (payload, commander):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    thesis = _clean_phrase(value)
                    break
            if thesis:
                break

    if not order_items:
        raise StagedPublicationHandoffError(
            "commander work order or structured section source must provide section order"
        )

    order: list[str] = []
    seen: set[str] = set()
    for item in order_items:
        section_id = item["section_id"]
        if section_id in seen:
            raise StagedPublicationHandoffError(
                f"duplicate section_id in commander/section source: {section_id}"
            )
        seen.add(section_id)
        order.append(section_id)
        if item["title"] and section_id not in title_by_id:
            title_by_id[section_id] = item["title"]

    missing = [sid for sid in EXPECTED_SCIENTIFIC_IDS if sid not in seen]
    if len(order) != len(EXPECTED_SCIENTIFIC_IDS):
        if missing:
            raise StagedPublicationHandoffError(
                "missing scientific section IDs: " + ", ".join(missing)
            )
        raise StagedPublicationHandoffError(
            "expected exactly "
            f"{len(EXPECTED_SCIENTIFIC_IDS)} scientific section IDs, "
            f"got {len(order)}"
        )

    return order, title_by_id, thesis


def _assign_scientific_sections(
    scientific: Sequence[tuple[str, str]],
    order: Sequence[str],
    title_by_id: Mapping[str, str],
) -> dict[str, Any]:
    title_to_ids: dict[str, list[str]] = {}
    for section_id in order:
        title = str(title_by_id.get(section_id) or "").strip()
        if title:
            title_to_ids.setdefault(_normalize_title(title), []).append(section_id)

    assigned: dict[str, tuple[str, str]] = {}
    used_ids: set[str] = set()
    assigned_indices: set[int] = set()
    for index, (title, content) in enumerate(scientific):
        matches = title_to_ids.get(_normalize_title(title), [])
        for candidate in matches:
            if candidate not in used_ids:
                assigned[candidate] = (title, content)
                used_ids.add(candidate)
                assigned_indices.add(index)
                break

    remaining_indices = [
        index for index in range(len(scientific)) if index not in assigned_indices
    ]
    remaining_ids = [sid for sid in order if sid not in used_ids]
    if len(remaining_indices) != len(remaining_ids):
        if set(order) == set(EXPECTED_SCIENTIFIC_IDS):
            remaining_ids = [
                sid
                for sid in EXPECTED_SCIENTIFIC_IDS
                if sid not in used_ids
            ]
        if len(remaining_indices) != len(remaining_ids):
            raise StagedPublicationHandoffError(
                "could not map reviewed scientific sections to commander section IDs; "
                "provide a structured section source with explicit titles"
            )
    for index, section_id in zip(remaining_indices, remaining_ids):
        assigned[section_id] = scientific[index]

    if len(assigned) != len(order) or set(assigned) != set(order):
        raise StagedPublicationHandoffError(
            "failed to assign exactly one reviewed scientific section to each "
            "commander section ID"
        )
    return {
        "content_by_id": {
            section_id: _dedupe_scientific_content(content, title)
            for section_id, (title, content) in assigned.items()
        },
        "title_by_id": {
            section_id: (
                title_by_id.get(section_id)
                or assigned[section_id][0]
            )
            for section_id in order
        },
    }


def _catalog_identity_lookup(catalog: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()
    for key in ("records", "renderer_records"):
        value = catalog.get(key)
        if isinstance(value, Mapping):
            identities.update(str(item) for item in value.keys() if str(item).strip())
    entries = catalog.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            for key in (
                "identity",
                "canonical_identity",
                "aliases",
                "markers",
            ):
                value = entry.get(key)
                if isinstance(value, str):
                    identities.add(value.strip())
                elif isinstance(value, list):
                    identities.update(
                        str(item).strip()
                        for item in value
                        if str(item).strip()
                    )
    return {value for value in identities if value}


def _validate_front_matter_markers(
    *,
    abstract: str,
    introduction: str,
    conclusion: str,
    catalog: Mapping[str, Any],
) -> dict[str, list[str]]:
    lookup = _catalog_identity_lookup(catalog)
    marker_map = {
        "abstract": _extract_ref_markers(abstract),
        "introduction": _extract_ref_markers(introduction),
        "conclusion": _extract_ref_markers(conclusion),
    }
    for section, markers in marker_map.items():
        unknown = sorted({marker for marker in markers if marker not in lookup})
        if unknown:
            raise StagedPublicationHandoffError(
                f"front-matter REF identity not present in metadata catalog "
                f"({section}): {', '.join(unknown)}"
            )
    return marker_map


def _catalog_entries(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = catalog.get("entries")
    if isinstance(entries, list):
        return [
            dict(entry)
            for entry in entries
            if isinstance(entry, Mapping)
        ]
    for key in ("renderer_records", "records"):
        value = catalog.get(key)
        if isinstance(value, Mapping):
            return [
                _sanitize_record_fields(record)
                for record in value.values()
                if isinstance(record, Mapping)
            ]
    return []


def _entry_aliases(entry: Mapping[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("canonical_identity", "identity"):
        value = str(entry.get(key) or "").strip()
        if value and value not in aliases:
            aliases.append(value)
    raw_aliases = entry.get("aliases")
    if isinstance(raw_aliases, list):
        for value in raw_aliases:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in aliases:
                aliases.append(cleaned)
    if not aliases:
        paper_id = str(entry.get("paper_id") or "").strip()
        if paper_id:
            aliases.append(paper_id)
    return aliases


def _metadata_source(entry: Mapping[str, Any]) -> str:
    direct = str(entry.get("metadata_source") or "").strip()
    if direct:
        return direct
    provenance = entry.get("provenance")
    if isinstance(provenance, Mapping):
        title_provenance = provenance.get("title")
        if isinstance(title_provenance, Mapping):
            source = str(
                title_provenance.get("base_source")
                or title_provenance.get("source")
                or title_provenance.get("status")
                or ""
            ).strip()
            if source:
                return source
    return "unresolved"


def _ledger_row(entry: Mapping[str, Any], paper_id: str) -> dict[str, Any]:
    canonical_identity = str(
        entry.get("canonical_identity") or entry.get("identity") or paper_id
    ).strip()
    title = _clean_metadata_scalar(entry.get("title"))
    authors = _clean_authors(entry.get("authors"))
    year = _clean_year(entry.get("year"))
    venue = _clean_metadata_scalar(entry.get("venue"))
    doi = _clean_metadata_scalar(entry.get("doi"))
    url = _clean_metadata_scalar(entry.get("url"))
    s2_paper_id = _clean_metadata_scalar(entry.get("s2_id") or entry.get("s2_paper_id"))
    resolution_status = _clean_metadata_scalar(
        entry.get("resolution_status") or "unresolved"
    )
    missing_fields = entry.get("missing_fields") or []
    markers = entry.get("markers") or []
    marker_count = entry.get("marker_count")
    if marker_count is None:
        marker_count = len(markers) if isinstance(markers, list) else 0
    return {
        "paper_id": paper_id,
        "canonical_paper_id": canonical_identity,
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi,
        "url": url,
        "s2_paper_id": s2_paper_id,
        "reference_kind": str(
            entry.get("reference_kind")
            or ("article" if venue else "misc")
        ),
        "metadata_source": _metadata_source(entry),
        "resolution_status": resolution_status,
        "missing_fields": list(missing_fields),
        "markers": list(markers),
        "marker_count": int(marker_count),
    }


def _build_source_rows(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows_by_paper_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        aliases = _entry_aliases(entry)
        canonical_identity = str(
            entry.get("canonical_identity")
            or entry.get("identity")
            or (aliases[0] if aliases else "")
        ).strip()
        if not canonical_identity:
            continue
        ordered_aliases: list[str] = []
        if canonical_identity not in ordered_aliases:
            ordered_aliases.append(canonical_identity)
        for alias in sorted(aliases):
            if alias and alias not in ordered_aliases:
                ordered_aliases.append(alias)
        for alias in ordered_aliases:
            rows_by_paper_id[alias] = _ledger_row(entry, alias)
    return sorted(
        rows_by_paper_id.values(),
        key=lambda row: (
            str(row.get("canonical_paper_id") or ""),
            str(row.get("paper_id") or ""),
        ),
    )


def _sanitize_catalog_copy(catalog: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(dict(catalog), ensure_ascii=False))
    entries = cleaned.get("entries")
    if isinstance(entries, list):
        cleaned["entries"] = [
            _sanitize_record_fields(entry)
            if isinstance(entry, dict)
            else entry
            for entry in entries
        ]
    for key in ("records", "renderer_records"):
        value = cleaned.get(key)
        if isinstance(value, dict):
            cleaned[key] = {
                str(paper_id): _sanitize_record_fields(record)
                if isinstance(record, dict)
                else record
                for paper_id, record in value.items()
            }
    return cleaned


def _safe_visual_source(
    raw: Any,
    *,
    visual_package_dir: Path,
    allowed_roots: Sequence[Path],
) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise StagedPublicationHandoffError("visual asset path is empty")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = visual_package_dir / candidate
    candidate = candidate.resolve()
    resolved_roots = [Path(root).resolve() for root in allowed_roots]
    for root in resolved_roots:
        try:
            candidate.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise StagedPublicationHandoffError(
            f"unsafe visual asset path escapes allowed roots: {candidate}"
        )
    return candidate


def _safe_figure_name(figure_id: Any, index: int) -> str:
    raw = str(figure_id or "").strip()
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_.")
    return stem or f"figure_{index:03d}"


def _copy_visual_package(
    visual_package_path: Path,
    *,
    project_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    package = _read_json_object(visual_package_path, "final visual package")
    raw_figures = package.get("figures")
    if not isinstance(raw_figures, list):
        raise StagedPublicationHandoffError(
            "final visual package figures must be a list: "
            f"{visual_package_path}"
        )

    visual_package_dir = visual_package_path.parent
    visual_assets_dir = output_dir / VISUAL_ASSETS_DIRNAME
    visual_assets_dir.mkdir(parents=True, exist_ok=True)
    allowed_roots = [project_root, visual_package_dir]
    figures: list[dict[str, Any]] = []
    copied_count = 0
    accepted_count = 0
    missing_or_rejected: list[dict[str, str]] = []
    used_names: set[str] = set()

    for index, raw_item in enumerate(raw_figures, start=1):
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        figure_id = str(item.get("figure_id") or f"figure_{index:03d}")
        review_decision = str(item.get("review_decision") or "")
        render_status = str(item.get("render_status") or "")
        accepted = (
            review_decision in ACCEPTED_REVIEW_DECISIONS
            and render_status == "ready"
        )
        if accepted:
            accepted_count += 1
        source_raw = (
            item.get("local_path")
            or item.get("local_image_path")
            or ""
        )
        copy_status = "not_reviewed"
        copy_error = ""
        if not accepted:
            copy_status = "rejected_not_accepted_render_ready"
        elif not source_raw:
            copy_status = "missing_source_path_fail_open"
        else:
            try:
                source = _safe_visual_source(
                    source_raw,
                    visual_package_dir=visual_package_dir,
                    allowed_roots=allowed_roots,
                )
                suffix = source.suffix.lower()
                if suffix not in SAFE_FIGURE_SUFFIXES:
                    copy_status = "unsupported_figure_format"
                elif not source.is_file():
                    copy_status = "missing_source_file_fail_open"
                else:
                    base_name = _safe_figure_name(figure_id, index)
                    candidate_name = f"{base_name}{suffix}"
                    if candidate_name in used_names:
                        candidate_name = f"{base_name}_{index:03d}{suffix}"
                    used_names.add(candidate_name)
                    destination = visual_assets_dir / candidate_name
                    shutil.copyfile(source, destination)
                    digest = _sha256_file(destination)
                    portable_ref = _posix_relative(destination, output_dir)
                    item["local_path"] = portable_ref
                    item["local_image_path"] = portable_ref
                    item["source_sha256"] = digest
                    item["image_sha256"] = digest
                    panel_manifest = item.get("panel_manifest")
                    if isinstance(panel_manifest, list):
                        for panel in panel_manifest:
                            if not isinstance(panel, Mapping):
                                continue
                            panel_source = panel.get("source_local_path")
                            if str(panel_source or "").strip() in {
                                str(source_raw).strip(),
                                str(source).strip(),
                            }:
                                panel["source_local_path"] = portable_ref
                            panel["image_sha256"] = digest
                    copy_status = "copied_ready"
                    copied_count += 1
            except StagedPublicationHandoffError:
                raise

        if copy_status != "copied_ready":
            missing_or_rejected.append(
                {
                    "figure_id": figure_id,
                    "reason": copy_status,
                    "review_decision": review_decision,
                    "render_status": render_status,
                }
            )
            item["copy_status"] = copy_status
            item["copy_error"] = copy_error
            item["render_status"] = (
                "rejected_missing_source"
                if copy_status.startswith("missing_source")
                else "rejected_for_portable_handoff"
            )
            item["local_path"] = ""
            item["local_image_path"] = ""
        else:
            item["copy_status"] = copy_status
            item["copy_error"] = ""
            item["render_status"] = "ready"

        item["publication_eligible"] = bool(
            item.get("publication_eligible", False)
        )
        item["publication_eligible_reason"] = str(
            item.get("publication_eligible_reason")
            or "Source-derived or internal-study visual; not cleared for publication."
        )
        item["internal_study"] = True
        figures.append(item)

    if "publication_policy" not in package or not isinstance(
        package.get("publication_policy"), Mapping
    ):
        package["publication_policy"] = {
            "publication_eligible": False,
            "reason": (
                "Source-derived images are retained for internal study and "
                "are not cleared for publication."
            ),
        }
    package["figures"] = figures
    package["internal_study_audit"] = {
        "publication_eligible": False,
        "accepted_render_ready_count": accepted_count,
        "copied_asset_count": copied_count,
        "missing_or_rejected": missing_or_rejected,
    }
    return package


def _build_publication_metadata(
    publication_metadata_path: Path,
    *,
    abstract_text: str,
) -> tuple[dict[str, Any], list[str]]:
    supplied = _read_json_object(publication_metadata_path, "publication metadata")
    title = _clean_phrase(supplied.get("title"))
    if not title:
        raise StagedPublicationHandoffError(
            "publication metadata title is missing or empty"
        )
    raw_authors = supplied.get("authors")
    authors = _clean_authors(raw_authors)
    if not authors:
        raise StagedPublicationHandoffError(
            "publication metadata authors are missing or empty; "
            "no fallback author identity will be invented"
        )

    raw_keywords = supplied.get("keywords") or []
    if isinstance(raw_keywords, str):
        keywords = [
            keyword.strip()
            for keyword in re.split(r"[,;]", raw_keywords)
            if keyword.strip()
        ]
    elif isinstance(raw_keywords, list):
        keywords = [
            _clean_phrase(keyword)
            for keyword in raw_keywords
            if _clean_phrase(keyword)
        ]
    else:
        keywords = []
    date = _clean_phrase(supplied.get("date"))

    ai_statement = (
        "This manuscript was generated and assembled with OptoMind AI "
        "assistance for personal and internal study. AI assistance was used "
        "for literature organization, section drafting, visual selection, "
        "evidence traceability, and LaTeX composition."
    )
    internal_study_statement = (
        "This is an internal study draft. It has not been submitted to any "
        "journal, repository, or other publication venue, and it is not "
        "represented as peer-reviewed or publication-cleared."
    )
    figure_rights_statement = (
        "Figures are retained for internal study and traceability. They are "
        "not cleared for publication; any future submission must replace them "
        "with original figures or obtain the required permissions from rights "
        "holders."
    )

    incomplete_fields = [
        field
        for field, value in {
            "keywords": keywords,
            "date": date,
        }.items()
        if not value
    ]
    metadata = {
        "title": title,
        "authors": authors,
        "abstract": abstract_text,
        "keywords": keywords,
        "date": date,
        "draft_only": True,
        "publication_eligible": False,
        "ai_generation_statement": ai_statement,
        "internal_study_statement": internal_study_statement,
        "figure_rights_statement": figure_rights_statement,
        "acknowledgements": (
            ai_statement + " " + internal_study_statement + " " + figure_rights_statement
        ),
    }
    return metadata, incomplete_fields


def _build_review_blueprint(
    *,
    section_order: Sequence[str],
    title_by_id: Mapping[str, str],
    thesis: str,
) -> dict[str, Any]:
    return {
        "schema_version": "research_harness.review_blueprint.v1",
        "section_order": list(section_order),
        "sections": [
            {
                "section_id": section_id,
                "title": str(title_by_id.get(section_id) or section_id),
            }
            for section_id in section_order
        ],
        "review_thesis": thesis,
        "central_thesis": thesis,
    }


def _build_final_review(
    *,
    abstract_text: str,
    introduction_text: str,
    conclusion_text: str,
    section_order: Sequence[str],
    title_by_id: Mapping[str, str],
    content_by_id: Mapping[str, str],
) -> str:
    parts: list[str] = [
        "## Abstract",
        "",
        abstract_text,
        "",
        "## Introduction",
        "",
        introduction_text,
        "",
    ]
    for section_id in section_order:
        title = str(title_by_id.get(section_id) or section_id)
        parts.extend(
            [
                f"## {title}",
                "",
                content_by_id[section_id],
                "",
            ]
        )
    parts.extend(
        [
            "## Conclusion",
            "",
            conclusion_text,
        ]
    )
    return "\n".join(parts).strip() + "\n"


def _audit_metadata(catalog: Mapping[str, Any]) -> dict[str, Any]:
    entries = _catalog_entries(catalog)
    unresolved = sum(
        1 for entry in entries if entry.get("resolution_status") == "unresolved"
    )
    partial = sum(
        1 for entry in entries if entry.get("resolution_status") == "partial"
    )
    with_empty_authors = sum(1 for entry in entries if not entry.get("authors"))
    with_1900_years = sum(1 for entry in entries if str(entry.get("year")) == "1900")
    return {
        "catalog_entry_count": len(entries),
        "resolved_count": sum(
            1 for entry in entries if entry.get("resolution_status") == "resolved"
        ),
        "partial_count": partial,
        "unresolved_count": unresolved,
        "empty_authors_count": with_empty_authors,
        "placeholder_year_1900_count": with_1900_years,
        "missing_field_counts": {
            field: sum(
                1
                for entry in entries
                if field in (entry.get("missing_fields") or [])
            )
            for field in ("title", "authors", "year", "venue", "doi", "url")
        },
    }


def build_staged_publication_handoff(
    *,
    reviewed_manuscript_path: str | Path,
    conclusion_artifact_path: str | Path,
    introduction_artifact_path: str | Path,
    abstract_artifact_path: str | Path,
    metadata_catalog_path: str | Path,
    visual_package_path: str | Path,
    publication_metadata_path: str | Path,
    commander_path: str | Path | None = None,
    section_source_path: str | Path | None = None,
    metadata_audit_path: str | Path | None = None,
    output_dir: str | Path,
    project_root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the portable staged-publication handoff package.

    Parameters are explicit input paths.  ``project_root`` defaults to the
    repository root.  Relative input paths are resolved from ``project_root``.
    """

    project_root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    if not project_root.is_dir():
        raise StagedPublicationHandoffError(
            f"project_root is not a directory: {project_root}"
        )
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    reviewed_path = _require_input_file(
        reviewed_manuscript_path,
        project_root=project_root,
        label="reviewed manuscript",
    )
    conclusion_path = _require_input_file(
        conclusion_artifact_path,
        project_root=project_root,
        label="conclusion artifact",
    )
    introduction_path = _require_input_file(
        introduction_artifact_path,
        project_root=project_root,
        label="introduction artifact",
    )
    abstract_path = _require_input_file(
        abstract_artifact_path,
        project_root=project_root,
        label="abstract artifact",
    )
    catalog_path = _require_input_file(
        metadata_catalog_path,
        project_root=project_root,
        label="metadata catalog",
    )
    visual_path = _require_input_file(
        visual_package_path,
        project_root=project_root,
        label="final visual package",
    )
    publication_metadata_path = _require_input_file(
        publication_metadata_path,
        project_root=project_root,
        label="publication metadata",
    )

    commander_file: Path | None = None
    if commander_path is not None and str(commander_path).strip():
        commander_file = _require_input_file(
            commander_path,
            project_root=project_root,
            label="commander work order",
        )
    section_source_file: Path | None = None
    if section_source_path is not None and str(section_source_path).strip():
        section_source_file = _require_input_file(
            section_source_path,
            project_root=project_root,
            label="structured section source",
        )
    audit_file: Path | None = None
    if metadata_audit_path is not None and str(metadata_audit_path).strip():
        audit_file = _require_input_file(
            metadata_audit_path,
            project_root=project_root,
            label="metadata audit",
        )

    reviewed = _parse_reviewed_manuscript(reviewed_path)
    abstract_text = _parse_staged_draft(abstract_path, "abstract artifact")
    introduction_text = _parse_staged_draft(introduction_path, "introduction artifact")
    conclusion_text = _parse_staged_draft(conclusion_path, "conclusion artifact")
    catalog = _read_json_object(catalog_path, "metadata catalog")

    front_markers = _validate_front_matter_markers(
        abstract=abstract_text,
        introduction=introduction_text,
        conclusion=conclusion_text,
        catalog=catalog,
    )

    section_order, source_title_by_id, thesis = _load_section_order(
        commander_file,
        section_source_file,
    )
    assignment = _assign_scientific_sections(
        reviewed["scientific"],
        section_order,
        source_title_by_id,
    )
    content_by_id = assignment["content_by_id"]
    title_by_id = assignment["title_by_id"]

    body_text = "\n\n".join(
        _dedupe_scientific_content(content, title)
        for title, content in reviewed["scientific"]
    )
    body_ref_markers = _extract_ref_markers(body_text)
    body_sha256 = _sha256_text(body_text)

    publication_metadata, publication_incomplete_fields = _build_publication_metadata(
        publication_metadata_path,
        abstract_text=abstract_text,
    )
    review_blueprint = _build_review_blueprint(
        section_order=section_order,
        title_by_id=title_by_id,
        thesis=thesis,
    )
    final_review_text = _build_final_review(
        abstract_text=abstract_text,
        introduction_text=introduction_text,
        conclusion_text=conclusion_text,
        section_order=section_order,
        title_by_id=title_by_id,
        content_by_id=content_by_id,
    )

    final_review_path = output_dir / FINAL_REVIEW_FILENAME
    _write_text(final_review_path, final_review_text)

    visual_package = _copy_visual_package(
        visual_path,
        project_root=project_root,
        output_dir=output_dir,
    )
    visual_package_path = output_dir / FINAL_VISUAL_PACKAGE_FILENAME
    _write_json(visual_package_path, visual_package)

    clean_catalog = _sanitize_catalog_copy(catalog)
    catalog_output_path = output_dir / METADATA_CATALOG_FILENAME
    _write_json(catalog_output_path, clean_catalog)

    if audit_file is not None:
        audit_value = _read_json_any(audit_file, "metadata audit")
    else:
        audit_value = {
            "schema_version": str(catalog.get("schema_version") or ""),
            "input_fingerprint": str(catalog.get("input_fingerprint") or ""),
            "catalog_fingerprint": str(catalog.get("catalog_fingerprint") or ""),
            "audit": catalog.get("audit", {}),
        }
    audit_output_path = output_dir / METADATA_AUDIT_FILENAME
    _write_json(audit_output_path, audit_value)

    entries = _catalog_entries(catalog)
    source_rows = _build_source_rows(entries)
    ledger_path = output_dir / SECTION_SOURCE_LEDGER_RELPATH
    _write_json(
        ledger_path,
        {
            "schema_version": "research_harness.section_source_ledger.v1",
            "section_id": "ALL",
            "sources": source_rows,
        },
    )

    publication_metadata_path_out = output_dir / PUBLICATION_METADATA_FILENAME
    _write_json(publication_metadata_path_out, publication_metadata)

    blueprint_path = output_dir / REVIEW_BLUEPRINT_FILENAME
    _write_json(blueprint_path, review_blueprint)

    content_fingerprints = {
        "reviewed_manuscript_sha256": _sha256_file(reviewed_path),
        "reviewed_scientific_body_sha256": body_sha256,
        "final_review_sha256": _sha256_file(final_review_path),
        "final_visual_package_sha256": _sha256_file(visual_package_path),
        "metadata_catalog_sha256": _sha256_file(catalog_output_path),
        "metadata_audit_sha256": _sha256_file(audit_output_path),
        "publication_metadata_sha256": _sha256_file(publication_metadata_path_out),
        "review_blueprint_sha256": _sha256_file(blueprint_path),
        "section_source_ledger_sha256": _sha256_file(ledger_path),
    }
    metadata_audit = _audit_metadata(catalog)
    visual_audit = visual_package.get("internal_study_audit", {})
    package = {
        "schema_version": SCHEMA_VERSION,
        "run_id": (
            _clean_phrase(run_id)
            if run_id
            else (output_dir.name.strip() or "staged_publication_handoff")
        ),
        "status": "internal_study_draft",
        "completed_stage": "staged_publication_handoff",
        "source_run_dir": ".",
        "final_review_path": FINAL_REVIEW_FILENAME,
        "final_visual_package_path": FINAL_VISUAL_PACKAGE_FILENAME,
        "publication_metadata_path": PUBLICATION_METADATA_FILENAME,
        "base_kb_sqlite": "",
        "publication_eligible": False,
        "artifacts": {
            "review_blueprint": REVIEW_BLUEPRINT_FILENAME,
            "publication_metadata": PUBLICATION_METADATA_FILENAME,
            "metadata_catalog": METADATA_CATALOG_FILENAME,
            "metadata_audit": METADATA_AUDIT_FILENAME,
            "section_source_ledger": SECTION_SOURCE_LEDGER_RELPATH,
        },
        "publication_policy": visual_package.get("publication_policy", {}),
        "scope_note": (
            "Internal AI-generated study draft; not publication-ready. "
            "Missing or rejected visual assets and incomplete bibliography "
            "metadata are audited and fail open."
        ),
        "content_fingerprints": content_fingerprints,
        "audit": {
            "front_matter_ref_markers": front_markers,
            "reviewed_body_ref_marker_sequence": body_ref_markers,
            "reviewed_scientific_section_count": len(section_order),
            "publication_metadata_incomplete_fields": publication_incomplete_fields,
            "metadata_catalog_audit": metadata_audit,
            "visual_asset_audit": visual_audit,
        },
    }
    package_path = output_dir / CONTENT_PACKAGE_FILENAME
    _write_json(package_path, package)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": package["run_id"],
        "section_order": section_order,
        "front_matter_ref_markers": front_markers,
        "reviewed_body_ref_marker_sequence": body_ref_markers,
        "reviewed_scientific_body_sha256": body_sha256,
        "visual_asset_audit": visual_audit,
        "metadata_catalog_audit": metadata_audit,
        "output_paths": {
            "content_package": str(package_path),
            "final_review": str(final_review_path),
            "final_visual_package": str(visual_package_path),
            "publication_metadata": str(publication_metadata_path_out),
            "review_blueprint": str(blueprint_path),
            "metadata_catalog": str(catalog_output_path),
            "metadata_audit": str(audit_output_path),
            "section_source_ledger": str(ledger_path),
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "CONTENT_PACKAGE_FILENAME",
    "FINAL_REVIEW_FILENAME",
    "FINAL_VISUAL_PACKAGE_FILENAME",
    "METADATA_CATALOG_FILENAME",
    "METADATA_AUDIT_FILENAME",
    "PUBLICATION_METADATA_FILENAME",
    "REVIEW_BLUEPRINT_FILENAME",
    "SECTION_SOURCE_LEDGER_RELPATH",
    "StagedPublicationHandoffError",
    "build_staged_publication_handoff",
]
