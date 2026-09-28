"""Assemble a review from its manifest and unit-writer results.

This module is deliberately a batch assembler, not another writing stage.  It
reads the chapter arrangement manifest and the result paths recorded by the
unit batch runner, preserves a handle-citation copy, and creates a numeric
citation copy after paper identities have been merged by aliases and exact
DOIs.  It never calls a model.

The command requires every manifest unit and writes the final draft.  Use
``--check-only --unit ...`` for a small offline mapping check while a batch is
still running.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "outputs/unit_writing/20260927_astra_repair/DELIVERY_MANIFEST.json"
DEFAULT_BATCH_ROOT = PROJECT_ROOT / "outputs/full_review_draft/20260927_run01"
DEFAULT_OUTPUT_ROOT = DEFAULT_BATCH_ROOT

HANDLE_RE = re.compile(r"\bP\d{3,}\b")
CITATION_BRACKET_RE = re.compile(r"\[([^\[\]]{1,240})\]")
TABLE_SEPARATOR_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
TABLE_CAPTION_RE = re.compile(r"(表|Table)\s*\d+(?:\s*[-–—]\s*\d+)?")


class AssemblyError(RuntimeError):
    """A result or arrangement cannot be assembled honestly."""


@dataclass(frozen=True)
class Job:
    chapter_id: str
    unit_id: str
    arrangement: Path
    output: Path
    reused_result: Path | None


@dataclass
class UnitDocument:
    job: Job
    result_path: Path
    body: str
    result: dict[str, Any]
    focus: str
    chapter_argument: str
    chapter_title: str
    table_purposes: list[str]
    status: str


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssemblyError(f"missing_json:{path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"invalid_json:{path}:{type(exc).__name__}") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _norm_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text.rstrip(" .;,\t\r\n")


def _chapter_title(arrangement_path: Path, arrangement: Mapping[str, Any]) -> str:
    """Read the human chapter frame without inventing a title from evidence."""

    markdown = arrangement_path.with_name("CHAPTER_ARRANGEMENT.md")
    if markdown.is_file():
        first = markdown.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in first:
            match = re.match(r"^#\s*CH\d+\s+章内编排：(.+?)\s*$", line)
            if match:
                return match.group(1).strip()
            match = re.match(r"^#\s*CH\d+\s*[:：]\s*(.+?)\s*$", line)
            if match:
                return match.group(1).strip()
    title = arrangement.get("title") or arrangement.get("chapter_title")
    return str(title).strip() if title else str(arrangement.get("chapter_id") or "章节").strip()


def load_manifest(path: Path) -> tuple[dict[str, Any], list[Job], dict[str, dict[str, Any]]]:
    manifest = _read_json(path)
    jobs: list[Job] = []
    arrangements: dict[str, dict[str, Any]] = {}
    for chapter in manifest.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "").strip()
        arrangement_path = Path(str(chapter.get("arrangement_path") or "")).resolve()
        if not chapter_id or not arrangement_path.is_file():
            raise AssemblyError(f"arrangement_missing:{chapter_id}:{arrangement_path}")
        arrangement = _read_json(arrangement_path)
        arrangements[chapter_id] = arrangement
        output_default = DEFAULT_BATCH_ROOT / "units"
        for unit in arrangement.get("units") or []:
            unit_id = str(unit.get("unit_id") or "").strip()
            if not unit_id:
                raise AssemblyError(f"unit_id_missing:{chapter_id}")
            # BATCH_JOBS.json is authoritative when present; this fallback is
            # useful for offline checks before the runner has created it.
            jobs.append(Job(chapter_id, unit_id, arrangement_path, output_default / unit_id, None))
    if not jobs:
        raise AssemblyError("manifest_has_no_units")
    return manifest, jobs, arrangements


def load_jobs(batch_root: Path, fallback: Iterable[Job]) -> list[Job]:
    jobs = list(fallback)
    batch_path = batch_root / "BATCH_JOBS.json"
    if not batch_path.is_file():
        return jobs
    raw = _read_json(batch_path)
    if not isinstance(raw, list):
        raise AssemblyError(f"batch_jobs_not_list:{batch_path}")
    by_key: dict[tuple[str, str], Job] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("chapter_id") or "").strip(), str(item.get("unit_id") or "").strip())
        if not all(key):
            continue
        arrangement = Path(str(item.get("arrangement") or "")).resolve()
        output = Path(str(item.get("output") or batch_root / "units" / key[1])).resolve()
        reused_text = str(item.get("reused_result") or "").strip()
        by_key[key] = Job(key[0], key[1], arrangement, output, Path(reused_text).resolve() if reused_text else None)
    if not by_key:
        raise AssemblyError(f"batch_jobs_empty:{batch_path}")
    # Preserve manifest chapter/unit order while using the runner's paths.
    return [by_key.get((job.chapter_id, job.unit_id), job) for job in jobs]


def _batch_run_paths(batch_root: Path) -> dict[tuple[str, str], Path]:
    path = batch_root / "BATCH_RUN.json"
    if not path.is_file():
        return {}
    raw = _read_json(path)
    rows = raw.get("jobs") if isinstance(raw, Mapping) else raw
    result: dict[tuple[str, str], Path] = {}
    for item in rows or []:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("chapter_id") or "").strip(), str(item.get("unit_id") or "").strip())
        value = str(item.get("result") or item.get("result_path") or "").strip()
        if all(key) and value:
            candidate = Path(value).resolve()
            if candidate.is_file():
                result[key] = candidate
    return result


def resolve_result(job: Job, batch_results: Mapping[tuple[str, str], Path]) -> Path | None:
    if job.reused_result and job.reused_result.is_file():
        return job.reused_result
    candidate = batch_results.get((job.chapter_id, job.unit_id))
    if candidate and candidate.is_file():
        return candidate
    # run_units.py places the result below output/CHxx_Uxx/CHxx_CHxx_Uxx/.
    direct = job.output / "UNIT_RESULT.json"
    if direct.is_file():
        return direct
    matches = sorted(job.output.rglob("UNIT_RESULT.json")) if job.output.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer the result whose declared identity matches the job.
        for match in matches:
            try:
                result = _read_json(match)
            except AssemblyError:
                continue
            if result.get("chapter_id") == job.chapter_id and result.get("unit_id") == job.unit_id:
                return match
    return None


def load_unit_document(job: Job, result_path: Path, arrangement: Mapping[str, Any]) -> UnitDocument:
    result = _read_json(result_path)
    if not isinstance(result, dict):
        raise AssemblyError(f"result_not_object:{result_path}")
    if result.get("chapter_id") and result.get("chapter_id") != job.chapter_id:
        raise AssemblyError(f"result_chapter_mismatch:{result_path}")
    if result.get("unit_id") and result.get("unit_id") != job.unit_id:
        raise AssemblyError(f"result_unit_mismatch:{result_path}")
    if result.get("simulated") is True or str(result.get("mode") or "").lower() == "fake":
        raise AssemblyError(f"simulated_result:{job.unit_id}:{result_path}")
    complete = result.get("complete", True)
    finish_reason = str(result.get("finish_reason") or "")
    if complete is False or finish_reason == "length":
        raise AssemblyError(f"partial_result:{job.unit_id}:{finish_reason or 'incomplete'}")
    body = ""
    body_path_value = str(result.get("body_path") or "").strip()
    if body_path_value:
        body_path = Path(body_path_value)
        if not body_path.is_absolute():
            body_path = result_path.parent / body_path
        if body_path.is_file():
            body = body_path.read_text(encoding="utf-8", errors="replace")
    if not body:
        body = str(result.get("body_markdown") or "")
    body = body.strip()
    if not body or body.lower() in {"length", "partial", "none"}:
        raise AssemblyError(f"body_missing_or_invalid:{result_path}")
    units = {str(item.get("unit_id")): item for item in arrangement.get("units") or []}
    unit = units.get(job.unit_id) or {}
    focus = str(unit.get("focus") or job.unit_id).strip()
    table_purposes = [
        str(item.get("purpose") or "").strip()
        for item in unit.get("table_tasks") or []
        if isinstance(item, Mapping) and str(item.get("purpose") or "").strip()
    ]
    return UnitDocument(
        job=job,
        result_path=result_path,
        body=body,
        result=result,
        focus=focus,
        chapter_argument=str(arrangement.get("chapter_argument") or "").strip(),
        chapter_title=_chapter_title(job.arrangement, arrangement),
        table_purposes=table_purposes,
        status="reused" if job.reused_result else "written",
    )


def collect_documents(
    jobs: list[Job],
    arrangements: Mapping[str, Mapping[str, Any]],
    batch_root: Path,
    selected: set[str] | None = None,
) -> tuple[list[UnitDocument], list[str], list[str]]:
    batch_results = _batch_run_paths(batch_root)
    documents: list[UnitDocument] = []
    missing: list[str] = []
    errors: list[str] = []
    for job in jobs:
        if selected is not None and job.unit_id not in selected:
            continue
        result_path = resolve_result(job, batch_results)
        if result_path is None:
            missing.append(job.unit_id)
            continue
        try:
            documents.append(load_unit_document(job, result_path, arrangements[job.chapter_id]))
        except AssemblyError as exc:
            errors.append(str(exc))
    return documents, missing, errors


class IdentityIndex:
    """Union paper handles by declared aliases and exact normalized DOI."""

    def __init__(self, catalogs: Iterable[Mapping[str, Any]]) -> None:
        self.entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for catalog in catalogs:
            for handle, entry in catalog.items():
                if handle not in self.entries:
                    self.entries[str(handle)] = dict(entry)
                else:
                    # Keep the first stable identity, but retain any fields a
                    # later chapter supplied if the first copy omitted them.
                    for key, value in dict(entry).items():
                        if self.entries[handle].get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                            self.entries[handle][key] = value
        self.order = {handle: index for index, handle in enumerate(self.entries)}
        self.parent = {handle: handle for handle in self.entries}
        self._build()

    def _find(self, value: str) -> str:
        parent = self.parent
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def _union(self, left: str, right: str) -> None:
        if left not in self.parent:
            self.parent[left] = left
        if right not in self.parent:
            self.parent[right] = right
        a, b = self._find(left), self._find(right)
        if a == b:
            return
        # The earliest manifest handle is the stable canonical handle.
        if self.order.get(a, 10**9) <= self.order.get(b, 10**9):
            self.parent[b] = a
        else:
            self.parent[a] = b

    def _build(self) -> None:
        for handle, entry in self.entries.items():
            for alias in entry.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    self._union(handle, alias)
        by_doi: dict[str, str] = {}
        for handle, entry in self.entries.items():
            doi = _norm_doi(entry.get("doi"))
            if doi:
                if doi in by_doi:
                    self._union(handle, by_doi[doi])
                else:
                    by_doi[doi] = handle
        self.canonical_for: dict[str, str] = {}
        for handle in self.entries:
            self.canonical_for[handle] = self._find(handle)
        # Aliases may point to a handle absent from this catalog (e.g. P0576).
        for handle, entry in self.entries.items():
            for alias in entry.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    self.canonical_for[alias] = self.canonical_for.get(alias, self.canonical_for[handle])

    def reference_key(self, handle: str) -> str | None:
        handle = str(handle).strip()
        return self.canonical_for.get(handle)

    def reference(self, canonical: str, number: int, used_handles: list[str]) -> dict[str, Any]:
        entry = dict(self.entries.get(canonical) or {})
        aliases = sorted({str(h) for h, c in self.canonical_for.items() if c == canonical})
        item: dict[str, Any] = {
            "reference_number": number,
            "canonical_handle": canonical,
            "handles": aliases,
            "title": entry.get("title") or None,
            "year": entry.get("year") or None,
            "doi": entry.get("doi") or None,
            "paper_id": entry.get("paper_id") or None,
            "identity_source": "source_catalog",
            "used_handles_in_text": used_handles,
        }
        # Some local cards may contain bibliographic fields.  Add them only
        # when explicitly present; never infer authors or a venue.
        card_path = Path(str(entry.get("card_path") or ""))
        if card_path.is_file():
            try:
                card = _read_json(card_path)
            except AssemblyError:
                card = {}
            identity = card.get("paper_identity") if isinstance(card, Mapping) else {}
            if isinstance(identity, Mapping):
                for field in ("authors", "journal", "venue", "publication", "publisher"):
                    value = identity.get(field)
                    if value not in (None, "", [], {}):
                        item[field] = value
        return item


def citation_handles(text: str) -> list[str]:
    found: list[str] = []
    for bracket in CITATION_BRACKET_RE.findall(text or ""):
        for handle in HANDLE_RE.findall(bracket):
            if handle not in found:
                found.append(handle)
    return found


def scan_citations(text: str, identity: IdentityIndex) -> tuple[list[str], list[str]]:
    """Return canonical first-appearance order and unknown handles."""

    canonical_order: list[str] = []
    unknown: list[str] = []
    for handle in citation_handles(text):
        canonical = identity.reference_key(handle)
        if canonical is None:
            if handle not in unknown:
                unknown.append(handle)
        elif canonical not in canonical_order:
            canonical_order.append(canonical)
    return canonical_order, unknown


def replace_citations_numbered(
    text: str, identity: IdentityIndex, number_by_canonical: Mapping[str, int]
) -> str:
    def replace_number(match: re.Match[str]) -> str:
        bracket = match.group(1)

        def one(handle_match: re.Match[str]) -> str:
            handle = handle_match.group(0)
            canonical = identity.reference_key(handle)
            if canonical is None:
                return handle
            return str(number_by_canonical[canonical])

        return "[" + HANDLE_RE.sub(one, bracket) + "]"

    return CITATION_BRACKET_RE.sub(replace_number, text)


def _is_table_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return len(cells) >= 1 and all(TABLE_SEPARATOR_RE.match(cell) for cell in cells)


def normalize_tables(
    text: str, table_number: int, focus: str, table_purposes: Iterable[str] = ()
) -> tuple[str, int]:
    lines = text.splitlines()
    out: list[str] = []
    index = table_number
    purpose_list = [str(item).strip() for item in table_purposes if str(item).strip()]
    purpose_index = 0
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and "|" in lines[i] and _is_table_separator(lines[i + 1]):
            caption_index = None
            for back in range(len(out) - 1, max(-1, len(out) - 4), -1):
                if out[back].strip() == "":
                    continue
                caption_index = back
                break
            if caption_index is not None and TABLE_CAPTION_RE.search(out[caption_index]):
                out[caption_index] = TABLE_CAPTION_RE.sub(f"表 {index}", out[caption_index], count=1)
            else:
                purpose = purpose_list[purpose_index] if purpose_index < len(purpose_list) else focus
                out.append(f"**表 {index}. {purpose}**")
            if out and out[-1].strip():
                out.append("")
            purpose_index += 1
            index += 1
            while i < len(lines) and ("|" in lines[i] or not lines[i].strip()):
                out.append(lines[i])
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip(), index


def clean_unit_body(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and re.match(r"^\s*#{1,6}\s+", lines[0]):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    cleaned: list[str] = []
    for line in lines:
        caption = re.match(
            r"^\s*#{1,6}\s+((?:表|Table)\s+\d+(?:\s*[-–—]\s*\d+)?(?:\s*[.:：]\s*.*|\s+.*)?)\s*$",
            line,
        )
        if caption:
            cleaned.append(f"**{caption.group(1).strip()}**")
            continue
        match = re.match(r"^(#{1,6})(\s+.*)$", line)
        if match:
            level = min(6, max(4, len(match.group(1)) + 2))
            line = "#" * level + match.group(2)
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def concise_unit_title(focus: str, override: str = "") -> str:
    """Keep long arrangement assertions out of the document heading."""

    if str(override or "").strip():
        return re.sub(r"\s+", " ", str(override).strip())
    value = re.sub(r"\s+", " ", str(focus or "").strip())
    if len(value) <= 48:
        return value
    clauses = [part.strip(" ，,；;。：:") for part in re.split(r"[；;。！？!?:：]", value) if part.strip()]
    if clauses and len(clauses[0]) >= 10:
        return clauses[0][:48].rstrip(" ，,；;") + ("…" if len(clauses[0]) > 48 else "")
    comma_parts = [part.strip(" ，,") for part in re.split(r"[，,]", value) if part.strip()]
    if comma_parts:
        candidate = comma_parts[0]
        if len(candidate) >= 10:
            return candidate[:48].rstrip(" ，,") + ("…" if len(candidate) > 48 else "")
    return value[:47].rstrip() + "…"


def editorial_cleanup(text: str, repairs: Mapping[str, str] | None = None) -> tuple[str, list[str]]:
    """Apply explicit manuscript-specific edits supplied in the manifest."""
    applied: list[str] = []
    for old, new in (repairs or {}).items():
        if old and old in text:
            text = text.replace(old, new)
            applied.append(f"{old} -> {new}")
    return text, applied


def assemble_documents(
    documents: list[UnitDocument],
    arrangements: Mapping[str, Mapping[str, Any]],
    identity: IdentityIndex,
    *,
    front_matter: str = "",
    back_matter: str = "",
    title_overrides: Mapping[str, str] | None = None,
    editorial_repairs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    by_chapter: OrderedDict[str, list[UnitDocument]] = OrderedDict()
    for document in documents:
        by_chapter.setdefault(document.job.chapter_id, []).append(document)
    handle_chapters: OrderedDict[str, str] = OrderedDict()
    numeric_chapters: OrderedDict[str, str] = OrderedDict()
    references_order: list[str] = []
    unknown_citations: list[str] = []
    table_number = 1
    unit_rows: list[dict[str, Any]] = []
    prepared: list[tuple[str, UnitDocument, str, int]] = []
    # First build the raw handle chapters and establish one citation order for
    # the complete manuscript.  Numbering per unit would restart at [1].
    for chapter_index, (chapter_id, chapter_docs) in enumerate(by_chapter.items(), start=1):
        chapter_title = chapter_docs[0].chapter_title
        argument = chapter_docs[0].chapter_argument
        handle_parts = [f"## 第{chapter_index}章 {chapter_title}"]
        if argument:
            handle_parts.extend(["", f"> 本章论断：{argument}"])
        for unit_index, document in enumerate(chapter_docs, start=1):
            clean = clean_unit_body(document.body)
            title = concise_unit_title(document.focus, (title_overrides or {}).get(document.job.unit_id, ""))
            normalized_handle, next_table_number = normalize_tables(
                clean, table_number, title, document.table_purposes
            )
            table_count = next_table_number - table_number
            table_number = next_table_number
            handle_parts.extend(["", f"### {chapter_index}.{unit_index} {title}", "", normalized_handle])
            prepared.append((chapter_id, document, normalized_handle, table_count))
            unit_rows.append({
                "chapter_id": document.job.chapter_id,
                "unit_id": document.job.unit_id,
                "result_path": str(document.result_path),
                "status": document.status,
                "body_chars": len(clean),
                "tables": table_count,
            })
        handle_chapters[chapter_id] = "\n".join(handle_parts).strip() + "\n"
    ordered_texts = [front_matter] if front_matter.strip() else []
    ordered_texts.extend(normalized_handle for _, _, normalized_handle, _ in prepared)
    if back_matter.strip():
        ordered_texts.append(back_matter)
    for text in ordered_texts:
        canonical_order, unknown = scan_citations(text, identity)
        for canonical in canonical_order:
            if canonical not in references_order:
                references_order.append(canonical)
        for handle in unknown:
            if handle not in unknown_citations:
                unknown_citations.append(handle)
    number_by_canonical = {canonical: index for index, canonical in enumerate(references_order, start=1)}
    # Render numeric chapters in the same prepared order while retaining the
    # chapter/unit hierarchy from the handle draft.
    prepared_by_chapter: dict[str, list[tuple[UnitDocument, str]]] = {}
    for chapter_id, document, normalized_handle, _ in prepared:
        prepared_by_chapter.setdefault(chapter_id, []).append((document, normalized_handle))
    for chapter_index, (chapter_id, chapter_docs) in enumerate(by_chapter.items(), start=1):
        chapter_title = chapter_docs[0].chapter_title
        argument = chapter_docs[0].chapter_argument
        numeric_parts = [f"## 第{chapter_index}章 {chapter_title}"]
        for unit_index, (document, normalized_handle) in enumerate(prepared_by_chapter.get(chapter_id, []), start=1):
            title = concise_unit_title(document.focus, (title_overrides or {}).get(document.job.unit_id, ""))
            numeric_body = replace_citations_numbered(normalized_handle, identity, number_by_canonical)
            numeric_body, _ = editorial_cleanup(numeric_body, editorial_repairs)
            numeric_parts.extend(["", f"### {chapter_index}.{unit_index} {title}", "", numeric_body])
        numeric_chapters[chapter_id] = "\n".join(numeric_parts).strip() + "\n"
    references: list[dict[str, Any]] = []
    used_by_reference: dict[str, list[str]] = {}
    citation_texts = [document.body for document in documents]
    citation_texts.extend(item for item in (front_matter, back_matter) if item)
    for citation_text in citation_texts:
        for handle in citation_handles(citation_text):
            canonical = identity.reference_key(handle)
            if canonical:
                used_by_reference.setdefault(canonical, [])
                if handle not in used_by_reference[canonical]:
                    used_by_reference[canonical].append(handle)
    for number, canonical in enumerate(references_order, start=1):
        references.append(identity.reference(canonical, number, used_by_reference.get(canonical, [])))
    front_handles = front_matter.strip()
    back_handles = back_matter.strip()
    front_numeric = replace_citations_numbered(front_handles, identity, number_by_canonical) if front_handles else ""
    back_numeric = replace_citations_numbered(back_handles, identity, number_by_canonical) if back_handles else ""
    front_numeric, _ = editorial_cleanup(front_numeric, editorial_repairs)
    back_numeric, _ = editorial_cleanup(back_numeric, editorial_repairs)
    handle_sections = [section for section in (front_handles, *handle_chapters.values(), back_handles) if section]
    numeric_sections = [section for section in (front_numeric, *numeric_chapters.values(), back_numeric) if section]
    numeric_full = "\n\n".join(numeric_sections).strip()
    handle_full = "\n\n".join(handle_sections).strip()
    return {
        "chapters_handles": handle_chapters,
        "chapters_numeric": numeric_chapters,
        "full_handles": handle_full + "\n",
        "full_numeric": numeric_full + "\n",
        "front_handles": front_handles,
        "back_handles": back_handles,
        "front_numeric": front_numeric,
        "back_numeric": back_numeric,
        "references": references,
        "references_order": references_order,
        "unknown_citations": unknown_citations,
        "table_count": table_number - 1,
        "unit_rows": unit_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble the Chinese review from completed unit results (offline only).")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--batch-root", default=str(DEFAULT_BATCH_ROOT))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--title", default="", help="review title; otherwise use manifest or front-matter heading")
    parser.add_argument("--unit", action="append", default=[], help="restrict an offline check to selected unit IDs")
    parser.add_argument("--check-only", action="store_true", help="validate mapping and citation preservation without writing draft files")
    parser.add_argument("--allow-partial", action="store_true", help="allow missing units for a diagnostic check; never claims full completion")
    return parser


def _catalogs(arrangements: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [arrangement.get("source_catalog") or {} for arrangement in arrangements.values()]


def _load_title_overrides(batch_root: Path) -> dict[str, str]:
    path = batch_root / "TITLE_OVERRIDES.json"
    if not path.is_file():
        return {}
    raw = _read_json(path)
    values = raw.get("titles") if isinstance(raw, Mapping) and isinstance(raw.get("titles"), Mapping) else raw
    if not isinstance(values, Mapping):
        raise AssemblyError(f"title_overrides_not_object:{path}")
    return {
        str(key).strip(): str(value).strip()
        for key, value in values.items()
        if str(key).strip() and str(value).strip()
    }


def _load_table_titles(batch_root: Path) -> dict[int, str]:
    path = batch_root / "TABLE_TITLES.json"
    if not path.is_file():
        return {}
    raw = _read_json(path)
    values = raw.get("titles") if isinstance(raw, Mapping) and isinstance(raw.get("titles"), Mapping) else raw
    if isinstance(values, list):
        return {index: str(value).strip() for index, value in enumerate(values, start=1) if str(value).strip()}
    if not isinstance(values, Mapping):
        raise AssemblyError(f"table_titles_not_object:{path}")
    result: dict[int, str] = {}
    for key, value in values.items():
        try:
            number = int(str(key).strip())
        except (TypeError, ValueError):
            continue
        if number > 0 and str(value).strip():
            result[number] = str(value).strip()
    return result


def _apply_table_titles(text: str, titles: Mapping[int, str]) -> str:
    """Make every table caption a bold ordinary line with one global number."""

    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        candidate = re.sub(r"^#{1,6}\s*", "", stripped)
        candidate = candidate.replace("**", "").strip()
        match = re.match(
            r"^(?:表|Table)\s*(\d+)(?:\s*[-–—]\s*\d+)?\s*(?:[.：:、]\s*)?(.*)$",
            candidate,
            flags=re.IGNORECASE,
        )
        if match and (stripped.startswith("#") or stripped.startswith("**") or candidate.startswith(("表", "Table"))):
            number = int(match.group(1))
            title = str(titles.get(number) or match.group(2) or "").strip()
            out.append(f"**表 {number}. {title}**")
        else:
            out.append(line)
    return "\n".join(out).strip() + "\n"


def _without_duplicate_title(front_matter: str, title: str) -> str:
    lines = front_matter.strip().splitlines()
    if lines and lines[0].strip().lstrip("#").strip() == title:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _report_markdown(summary: Mapping[str, Any]) -> str:
    missing = summary.get("missing_units") or []
    lines = [
        "# 全稿汇编运行报告",
        "",
        f"- 状态：`{summary.get('status', '')}`",
        f"- 预计单元数：{summary.get('expected_units', 0)}；已载入：{summary.get('loaded_units', 0)}；缺少：{len(missing)}",
        f"- 实际使用论文数：{summary.get('used_papers', 0)}；未使用论文数：{summary.get('unused_papers', 0)}",
        f"- 全文统一表号：{summary.get('table_count', 0)} 张",
        f"- 未能映射的原始引用 handle：{', '.join(summary.get('unknown_citations') or []) or '无'}",
        "",
        "## 单元状态",
        "",
        "| 章节 | 单元 | 状态 | 正文字符数 | 结果路径 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in summary.get("unit_rows") or []:
        lines.append(f"| {row['chapter_id']} | {row['unit_id']} | {row['status']} | {row['body_chars']} | `{row['result_path']}` |")
    if missing:
        lines.extend(["", "## 缺少单元", "", *[f"- `{item}`" for item in missing]])
    if summary.get("errors"):
        lines.extend(["", "## 结果错误", "", *[f"- `{item}`" for item in summary["errors"]]])
    lines.extend(["", "摘要与结语由主代理在全稿亲审后补写。", ""])
    return "\n".join(lines)


def _reference_line(reference: Mapping[str, Any], *, handles: bool = False) -> str:
    number = int(reference.get("reference_number") or 0)
    title = str(reference.get("title") or "题名缺失")
    year = str(reference.get("year") or "年份缺失")
    doi = str(reference.get("doi") or "").strip()
    prefix = ", ".join(str(item) for item in reference.get("handles") or []) if handles else str(number)
    line = f"{prefix}. {title}（{year}）"
    if doi:
        line += f". DOI: {doi}"
    return line


def _references_markdown(references: Iterable[Mapping[str, Any]], *, handles: bool = False) -> str:
    heading = "## 参考文献" if not handles else "## 参考文献（原始 handle）"
    lines = [heading, ""]
    lines.extend(_reference_line(reference, handles=handles) for reference in references)
    return "\n".join(lines).strip() + "\n"


def run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    batch_root = Path(args.batch_root).resolve()
    output_root = Path(args.output_root).resolve()
    manifest, fallback_jobs, arrangements = load_manifest(manifest_path)
    jobs = load_jobs(batch_root, fallback_jobs)
    selected = {str(item).strip() for item in args.unit if str(item).strip()} or None
    if selected and not args.check_only:
        raise AssemblyError("unit_selection_requires_check_only")
    documents, missing, errors = collect_documents(jobs, arrangements, batch_root, selected=selected)
    expected = len([job for job in jobs if selected is None or job.unit_id in selected])
    if errors:
        raise AssemblyError(";".join(errors))
    if missing and not args.allow_partial and not args.check_only:
        raise AssemblyError("missing_units:" + ",".join(missing))
    identity = IdentityIndex(_catalogs(arrangements))
    front_path = batch_root / "FRONT_MATTER.md"
    back_path = batch_root / "BACK_MATTER.md"
    front_matter = front_path.read_text(encoding="utf-8") if front_path.is_file() else ""
    back_matter = back_path.read_text(encoding="utf-8") if back_path.is_file() else ""
    front_title = re.search(r"^#\s+(.+)$", front_matter, re.MULTILINE)
    title = str(getattr(args, "title", "") or manifest.get("review_title") or manifest.get("title")
                or (front_title.group(1) if front_title else "文献综述")).strip()
    editorial_repairs = manifest.get("editorial_repairs") or {}
    if not isinstance(editorial_repairs, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in editorial_repairs.items()
    ):
        raise AssemblyError("editorial_repairs_must_be_string_mapping")
    front_matter = _without_duplicate_title(front_matter, title)
    title_overrides = _load_title_overrides(batch_root)
    table_titles = _load_table_titles(batch_root)
    assembled = assemble_documents(
        documents,
        arrangements,
        identity,
        front_matter=front_matter,
        back_matter=back_matter,
        title_overrides=title_overrides,
        editorial_repairs=editorial_repairs,
    )
    used_canonicals = set(assembled["references_order"])
    all_canonicals = {identity.reference_key(handle) for handle in identity.entries}
    all_canonicals.discard(None)
    summary: dict[str, Any] = {
        "schema_version": "optomind.full_review_draft.run_report.v1",
        "status": "complete" if not missing and not errors and selected is None and len(documents) == len(jobs) else "partial_check",
        "review_title": title,
        "expected_units": expected,
        "manifest_units": len(jobs),
        "loaded_units": len(documents),
        "missing_units": missing,
        "errors": errors,
        "used_papers": len(used_canonicals),
        "unused_papers": len(all_canonicals - used_canonicals),
        "catalog_papers": len(all_canonicals),
        "table_count": assembled["table_count"],
        "unknown_citations": assembled["unknown_citations"],
        "unit_rows": assembled["unit_rows"],
        "editorial_repairs": dict(editorial_repairs),
        "references_path": str(output_root / "REFERENCES.json"),
        "batch_root": str(batch_root),
        "front_matter": str(front_path) if front_path.is_file() else "",
        "back_matter": str(back_path) if back_path.is_file() else "",
        "title_overrides": len(title_overrides),
        "table_titles": len(table_titles),
    }
    if args.check_only:
        print(json.dumps({"summary": summary, "references": assembled["references"], "full_handles": assembled["full_handles"]}, ensure_ascii=False, indent=2))
        return 0
    if missing:
        raise AssemblyError("partial_assembly_refused:" + ",".join(missing))
    output_root.mkdir(parents=True, exist_ok=True)
    chapter_dir = output_root / "chapters"
    chapter_handle_dir = output_root / "chapters_handles"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    chapter_handle_dir.mkdir(parents=True, exist_ok=True)
    for chapter_id, text in assembled["chapters_numeric"].items():
        standalone = re.sub(r"^## ", "# ", _apply_table_titles(text, table_titles), count=1)
        (chapter_dir / f"{chapter_id}.md").write_text(standalone, encoding="utf-8")
    for chapter_id, text in assembled["chapters_handles"].items():
        standalone = re.sub(r"^## ", "# ", _apply_table_titles(text, table_titles), count=1)
        (chapter_handle_dir / f"{chapter_id}.md").write_text(standalone, encoding="utf-8")
    full_numeric = f"# {title}\n\n" + _apply_table_titles(assembled["full_numeric"], table_titles)
    full_numeric = full_numeric.rstrip() + "\n\n" + _references_markdown(assembled["references"])
    full_handles = f"# {title}\n\n" + _apply_table_titles(assembled["full_handles"], table_titles)
    full_handles = full_handles.rstrip() + "\n\n" + _references_markdown(assembled["references"], handles=True)
    (output_root / "REVIEW_DRAFT.md").write_text(full_numeric, encoding="utf-8")
    (output_root / "REVIEW_DRAFT_HANDLES.md").write_text(full_handles, encoding="utf-8")
    _write_json(output_root / "REFERENCES.json", {
        "schema_version": "optomind.full_review_draft.references.v1",
        "citation_style": "numeric_order_of_first_appearance",
        "references": assembled["references"],
        "handle_to_reference": {
            handle: next((item["reference_number"] for item in assembled["references"] if handle in item["handles"]), None)
            for handle in sorted(identity.canonical_for)
        },
        "catalog_papers": len(all_canonicals),
        "used_papers": len(used_canonicals),
        "unused_papers": len(all_canonicals - used_canonicals),
    })
    _write_json(output_root / "ASSEMBLY_SUMMARY.json", summary)
    (output_root / "RUN_REPORT.md").write_text(_report_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        return run(_parser().parse_args(argv))
    except AssemblyError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
