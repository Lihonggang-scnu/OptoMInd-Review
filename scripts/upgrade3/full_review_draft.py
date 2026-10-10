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

# Keep the handle boundary ASCII-only so a handle adjacent to CJK text such
# as ``[参考P0602]`` is consumed, while identifier substrings remain excluded.
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])P\d{3,}(?![A-Za-z0-9_])")
CITATION_BRACKET_RE = re.compile(r"\[([^\[\]]{1,240})\]")
CITATION_PREFIX_RE = re.compile(
    r"^(?:(?:参考)|(?:ref(?:erence)?|citation|source))\s*[:：]?\s*",
    re.IGNORECASE,
)
CITATION_SEQUENCE_RE = re.compile(
    r"(?:P\d{3,}|\d+)(?:[ ,;\-–]*(?:P\d{3,}|\d+))*"
)
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
    unit_title: str = ""
    pending_problem: str = ""


def _valid_unit_title(value: Any) -> str:
    """A real title field, not a truncated assertion sentence."""

    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or len(text) > 60:
        return ""
    if text.endswith(("。", "．", ".", "！", "！", "?", "？", "；", ";")):
        return ""
    return text


def _unit_pending_problem(result: Mapping[str, Any]) -> str:
    """Why this unit's result is not a resolved, complete answer ("" = none)."""

    reasons = []
    if result.get("complete") is False or str(result.get("finish_reason") or "") == "length":
        reasons.append(str(result.get("completion_status") or "partial_length"))
    issues = result.get("issues") or []
    if issues:
        reasons.append(f"writer_issues:{len(issues)}")
    citation_problems = _unresolved_citation_problems(result)
    if citation_problems:
        reasons.append(f"citation_problems:{len(citation_problems)}")
    return ";".join(reasons)


def _issue_details(value: Any) -> list[dict[str, Any]]:
    """Keep writer issue objects available to downstream reports."""

    return [dict(item) for item in value or () if isinstance(item, Mapping)]


# Keep provenance and successful repairs visible without treating them as defects.
_CITATION_DIAGNOSTIC_FIELDS = (
    "citation_problems", "unresolved_numeric_citations", "unknown_citations",
    "numeric_citation_repairs", "bibliography_title_citation_map",
    "citation_number_map_origin", "citation_mapping_diagnostics",
    "known_tool_identifiers", "non_source_identifier_citations",
)


def _unresolved_citation_problems(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Carry identity uncertainty, including older results missing problem rows."""

    problems: list[dict[str, Any]] = []
    for item in [*_issue_details(result.get("citation_problems")),
                 *_issue_details(result.get("citation_mapping_diagnostics"))]:
        if item.get("resolved") is True or str(item.get("severity") or "").lower() in {
            "info", "informational", "debug",
        }:
            continue
        if item not in problems:
            problems.append(item)
    for field, code, identity_key in (
        ("unresolved_numeric_citations", "numeric_citation_unresolved", "citation"),
        ("unknown_citations", "citation_not_in_unit_sources", "handle"),
        ("non_source_identifier_citations", "non_source_identifier_citation", "citation"),
    ):
        for value in result.get(field) or []:
            item = dict(value) if isinstance(value, Mapping) else {identity_key: str(value)}
            item.setdefault("code", code)
            if not any(existing.get("code") == item["code"] and
                       existing.get(identity_key) == item.get(identity_key) for existing in problems):
                problems.append(item)
    return problems


def _citation_diagnostics(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in _CITATION_DIAGNOSTIC_FIELDS if key in result}


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
    body = ""
    body_path_value = str(result.get("body_path") or "").strip()
    if body_path_value:
        body_path = Path(body_path_value)
        # A moved run can retain sealed historical absolute metadata. Prefer
        # its same-attempt body rather than another tree at the old address.
        local_body = result_path.parent / "UNIT_BODY.md"
        if body_path.name == local_body.name and local_body.is_file():
            body_path = local_body
        elif not body_path.is_absolute():
            body_path = result_path.parent / body_path
        if body_path.is_file():
            body = body_path.read_text(encoding="utf-8", errors="replace")
    if not body:
        body = str(result.get("body_markdown") or "")
    body = body.strip()
    if not body or body.lower() in {"length", "partial", "none"}:
        # No usable text at all: nothing to assemble for this unit.
        raise AssemblyError(f"body_missing_or_invalid:{result_path}")
    # A length-cutoff or incomplete flag with usable text assembles into a
    # restricted draft: the body is kept, and the unresolved state travels in
    # unit_rows/pending_problems instead of blocking the whole manuscript.
    pending_problem = _unit_pending_problem(result)
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
        unit_title=_valid_unit_title(unit.get("unit_title") or unit.get("title")),
        pending_problem=pending_problem,
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

        stripped = bracket.strip()
        prefix = CITATION_PREFIX_RE.match(stripped)
        if prefix:
            citation_text = stripped[prefix.end():].strip()
            handles = HANDLE_RE.findall(citation_text)
            if handles and CITATION_SEQUENCE_RE.fullmatch(citation_text) and all(
                identity.reference_key(handle) in number_by_canonical for handle in handles
            ):
                return "[" + HANDLE_RE.sub(one, citation_text) + "]"

        return "[" + HANDLE_RE.sub(one, bracket) + "]"

    return CITATION_BRACKET_RE.sub(replace_number, text)


def replace_table_handle_cells(
    text: str, identity: IdentityIndex, number_by_canonical: Mapping[str, int]
) -> tuple[str, int, list[str]]:
    """Show formal reference numbers for known handles in TABLE rows only.

    Table source columns are the known residual location for bare ``P1234``
    handles.  Prose identifiers that merely look like handles are left alone,
    and handles with no identity mapping are never guessed — they are
    returned for the run report instead.
    """

    out: list[str] = []
    replacements = 0
    unknown: list[str] = []

    def one(match: re.Match[str]) -> str:
        nonlocal replacements
        handle = match.group(0)
        canonical = identity.reference_key(handle)
        if canonical is None or canonical not in number_by_canonical:
            return handle
        replacements += 1
        return f"[{number_by_canonical[canonical]}]"

    lines = text.splitlines()
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            index += 1
            continue
        if not in_fence and index + 1 < len(lines) and line.lstrip().startswith("|") and _is_table_separator(lines[index + 1]):
            source_columns = _table_source_columns(line)
            out.append(line)
            out.append(lines[index + 1])
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                row = lines[index]
                cells = _table_cells(row)
                if source_columns and cells:
                    for column in source_columns:
                        if column >= len(cells):
                            continue
                        cells[column] = HANDLE_RE.sub(one, cells[column])
                        for handle in HANDLE_RE.findall(cells[column]):
                            canonical = identity.reference_key(handle)
                            if (canonical is None or canonical not in number_by_canonical) and handle not in unknown:
                                unknown.append(handle)
                    row = _render_table_cells(cells)
                out.append(row)
                index += 1
            continue
        out.append(line)
        index += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), replacements, unknown


_SOURCE_COLUMN_RE = re.compile(
    r"(?:来源|文献|论文|参考|路线|source|reference|citation|paper|doi|handle|route)",
    re.IGNORECASE,
)


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return stripped.split("|")


def _render_table_cells(cells: list[str]) -> str:
    return "|" + "|".join(cells) + "|"


def _table_source_columns(header: str) -> set[int]:
    return {
        index for index, cell in enumerate(_table_cells(header))
        if _SOURCE_COLUMN_RE.search(cell.strip())
    }


def _table_source_handles(text: str) -> list[str]:
    """Return bare handles from explicit source columns in real tables."""

    found: list[str] = []
    lines = text.splitlines()
    in_fence = False
    index = 0
    while index + 1 < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        if not in_fence and line.lstrip().startswith("|") and _is_table_separator(lines[index + 1]):
            columns = _table_source_columns(line)
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                cells = _table_cells(lines[index])
                for column in columns:
                    if column < len(cells):
                        for handle in HANDLE_RE.findall(cells[column]):
                            if handle not in found:
                                found.append(handle)
                index += 1
            continue
        index += 1
    return found


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
    def resolved_title(document: UnitDocument) -> str:
        override = (title_overrides or {}).get(document.job.unit_id, "")
        if str(override or "").strip():
            return concise_unit_title(document.focus, override)
        # A real unit-title field from the arrangement beats a truncated
        # focus sentence; the concise fallback stays for units without one.
        return document.unit_title or concise_unit_title(document.focus, "")

    table_handle_replacements = 0
    unknown_table_handles: list[str] = []
    for chapter_index, (chapter_id, chapter_docs) in enumerate(by_chapter.items(), start=1):
        chapter_title = chapter_docs[0].chapter_title
        argument = chapter_docs[0].chapter_argument
        handle_parts = [f"## 第{chapter_index}章 {chapter_title}"]
        if argument:
            handle_parts.extend(["", f"> 本章论断：{argument}"])
        for unit_index, document in enumerate(chapter_docs, start=1):
            clean = clean_unit_body(document.body)
            title = resolved_title(document)
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
                "pending_problem": document.pending_problem,
                "completion_status": str(document.result.get("completion_status") or ""),
                "issues": _issue_details(document.result.get("issues")),
                **_citation_diagnostics(document.result),
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
        # A bare handle in a declared table source column is an actual use and
        # must receive a number even when the prose contains no bracketed cite.
        for handle in _table_source_handles(text):
            canonical = identity.reference_key(handle)
            if canonical and canonical not in references_order:
                references_order.append(canonical)
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
            title = resolved_title(document)
            numeric_body = replace_citations_numbered(normalized_handle, identity, number_by_canonical)
            numeric_body, replaced, unknown_cells = replace_table_handle_cells(
                numeric_body, identity, number_by_canonical)
            table_handle_replacements += replaced
            for handle in unknown_cells:
                if handle not in unknown_table_handles:
                    unknown_table_handles.append(handle)
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
        "table_handle_replacements": table_handle_replacements,
        "unknown_table_handles": unknown_table_handles,
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


def _status_from_value(value: Any, *, default: str = "not_checked") -> str:
    if isinstance(value, Mapping):
        value = value.get("status") or value.get("planning_status") or value.get("arrangement_status")
    text = str(value or "").strip().casefold()
    return text or default


def _status_from_locator(manifest: Mapping[str, Any], manifest_path: Path, keys: tuple[str, ...]) -> str:
    """Read only an explicitly supplied status or status file locator."""

    for key in keys:
        if key not in manifest:
            continue
        value = manifest.get(key)
        direct = "" if key.endswith("_path") else _status_from_value(value, default="")
        if direct:
            return direct
        locator = str(value or "").strip()
        if not locator:
            continue
        path = Path(locator)
        if not path.is_absolute():
            path = manifest_path.parent / path
        if path.is_file():
            raw = _read_json(path)
            status = _status_from_value(raw, default="")
            if status:
                return status
    return "not_checked"


def _delivery_statuses(
    manifest: Mapping[str, Any], manifest_path: Path,
    arrangements: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    planning = _status_from_locator(
        manifest, manifest_path,
        ("planning_status", "planning", "planning_result_path", "planning_status_path", "planning_report_path"),
    )
    arrangement = _status_from_locator(
        manifest, manifest_path,
        ("arrangement_status", "arrangement_result_path", "arrangement_status_path", "arrangement_report_path"),
    )
    record_statuses: list[str] = []
    arrangement_issues: list[dict[str, Any]] = []
    for chapter_id, item in arrangements.items():
        if not isinstance(item, Mapping):
            continue
        validation = item.get("validation") if isinstance(item.get("validation"), Mapping) else {}
        status = _status_from_value(
            validation.get("status") or item.get("arrangement_status") or item.get("status"),
            default="",
        )
        if validation.get("needs_arrangement") is True and status not in {"partial", "failed", "incomplete", "unresolved", "needs_arrangement"}:
            status = "needs_arrangement"
        if status:
            record_statuses.append(status)
        raw_issues = [*list(item.get("issues") or []), *list(validation.get("issues") or []), *list(validation.get("errors") or [])]
        for issue in raw_issues:
            if issue in (None, "", [], {}):
                continue
            arrangement_issues.append({"chapter_id": chapter_id, "issue": issue})
        for key in ("missing_sources", "sources_never_mentioned"):
            values = validation.get(key) or []
            if values:
                arrangement_issues.append({"chapter_id": chapter_id, "issue": {key: values}})
    if record_statuses:
        unique = set(record_statuses)
        blockers = {"partial", "failed", "incomplete", "unresolved", "blocked", "not_ready", "not_passed", "needs_arrangement"}
        if any(status in blockers for status in record_statuses):
            arrangement = record_statuses[0] if len(unique) == 1 else "partial"
        elif arrangement in blockers:
            pass  # An explicit unresolved upstream result is not cleared by loaded records.
        elif len(unique) == 1:
            arrangement = record_statuses[0]
        else:
            arrangement = "partial"
    return planning, arrangement, arrangement_issues


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
        f"- 装配齐全：{'是' if summary.get('status') == 'complete' else '否'}；已接入结果中的问题已解决：{'是' if summary.get('problems_resolved') else '否'}（不代表科学内容审校通过）",
        f"- 规划状态：`{summary.get('planning_status', 'not_checked')}`；编排状态：`{summary.get('arrangement_status', 'not_checked')}`",
        f"- 预计单元数：{summary.get('expected_units', 0)}；已载入：{summary.get('loaded_units', 0)}；缺少：{len(missing)}",
        f"- 实际使用论文数：{summary.get('used_papers', 0)}；未使用论文数：{summary.get('unused_papers', 0)}",
        f"- 全文统一表号：{summary.get('table_count', 0)} 张",
        f"- 未能映射的原始引用 handle：{', '.join(summary.get('unknown_citations') or []) or '无'}",
        f"- 表格来源栏已换正式编号：{summary.get('table_handle_replacements', 0)} 处；未能映射表格句柄：{', '.join(summary.get('unknown_table_handles') or []) or '无'}",
        "",
        "## 单元状态",
        "",
        "| 章节 | 单元 | 状态 | 正文字符数 | 待处理问题 | 结果路径 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in summary.get("unit_rows") or []:
        lines.append(f"| {row['chapter_id']} | {row['unit_id']} | {row['status']} | {row['body_chars']} | {row.get('pending_problem') or '无'} | `{row['result_path']}` |")
    if missing:
        lines.extend(["", "## 缺少单元", "", *[f"- `{item}`" for item in missing]])
    if summary.get("errors"):
        lines.extend(["", "## 结果错误", "", *[f"- `{item}`" for item in summary["errors"]]])
    if summary.get("pending_problems"):
        lines.extend(["", "## 待处理问题（不因装配齐全而视为解决）", ""])
        for item in summary["pending_problems"]:
            line = f"- `{item.get('unit_id') or '（全局）'}`：{item.get('code')}"
            if item.get("issues"):
                line += "；详情：" + json.dumps(item["issues"], ensure_ascii=False, separators=(",", ":"))
            if item.get("citation_problems"):
                line += "；引用详情：" + json.dumps(item["citation_problems"], ensure_ascii=False, separators=(",", ":"))
            lines.append(line)
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
    if errors and not args.check_only:
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
    # "Assembly complete" (every unit loaded) and "problems resolved" (no
    # partial writer results, writer issues, or unmapped handles) are separate
    # statements; a restricted draft is allowed to carry both honestly.
    pending_problems = [
        {"unit_id": row["unit_id"], "code": row["pending_problem"],
         "issues": row.get("issues") or [],
         **({"citation_problems": _unresolved_citation_problems(row)}
            if _unresolved_citation_problems(row) else {})}
        for row in assembled["unit_rows"] if row.get("pending_problem")
    ]
    unknown_table_handles = list(assembled.get("unknown_table_handles") or [])
    for handle in unknown_table_handles:
        pending_problems.append({"unit_id": "", "code": f"unmapped_table_handle:{handle}"})
    planning_status, arrangement_status, arrangement_issues = _delivery_statuses(manifest, manifest_path, arrangements)
    status_blockers = {
        "partial", "failed", "incomplete", "unresolved", "blocked", "not_ready", "not_passed", "needs_arrangement",
    }
    if planning_status in status_blockers:
        pending_problems.append({"unit_id": "", "code": f"planning_status:{planning_status}"})
    if arrangement_status in status_blockers:
        pending_problems.append({"unit_id": "", "code": f"arrangement_status:{arrangement_status}"})
    for item in arrangement_issues:
        pending_problems.append({"unit_id": "", "code": "arrangement_issue", "issues": [item]})
    summary: dict[str, Any] = {
        "schema_version": "optomind.full_review_draft.run_report.v1",
        "status": "complete" if not missing and not errors and selected is None and len(documents) == len(jobs) else "partial_check",
        "problems_resolved": (
            not pending_problems
            and not assembled["unknown_citations"]
            and not missing
            and not errors
        ),
        "pending_problems": pending_problems,
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
        "table_handle_replacements": assembled.get("table_handle_replacements", 0),
        "unknown_table_handles": unknown_table_handles,
        "unknown_citations": assembled["unknown_citations"],
        "planning_status": planning_status,
        "arrangement_status": arrangement_status,
        "arrangement_issues": arrangement_issues,
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
