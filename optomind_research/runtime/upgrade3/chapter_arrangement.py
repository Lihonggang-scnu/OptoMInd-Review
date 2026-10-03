"""Chapter-internal arrangement for review writing plans.

Work order package ``outputs/work_orders/20260926_chapter_arrangement``.

This module turns one chapter of an existing writer packet into (a) a compact
arrangement input for a chapter editor model and (b) exported paragraph/table
tasks a writer can execute.  It is deliberately downstream-only: it does not
retrieve, read, or re-plan anything, and it never rewrites the upstream plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "optomind.chapter_arrangement.v1"
VIEW_SCHEMA = "optomind.chapter_arrangement.view.v1"
OUTPUT_SCHEMA = "optomind.chapter_arrangement.output.v1"
USAGE_SCHEMA = "optomind.chapter_arrangement.source_usage.v1"
ID_MAP_SCHEMA = "optomind.chapter_arrangement.id_map.v1"

#: Handle shape used by the upstream plan (e.g. P0001).  Bare handles are legal
#: input and are kept even when their material is not resolvable locally.
_HANDLE_RE = re.compile(r"\bP\d{4}\b")
_PARAGRAPH_FIELDS = ("paragraph_briefs",)
#: Units in some chapters carry the paragraph-shaped tasks under another key.
_LEGACY_PARAGRAPH_FIELDS = ("ordered_development",)
_CASE_FIELDS = (
    "supporting_studies",
    "concrete_studies",
    "cases_and_sources",
    "cases_and_references",
)

#: B fields handed to the arrangement model, in a fixed order.
B_FIELDS = (
    "planning_summary",
    "facet_contributions",
    "broader_review_uses",
    "scope_interpretation_cautions",
    "topic_handles",
)


class ChapterArrangementError(ValueError):
    """Invalid chapter input or arrangement output."""


# --------------------------------------------------------------------------
# identity helpers
# --------------------------------------------------------------------------


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "https://dx.doi.org/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def normalize_title(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_close(left: str, right: str, threshold: float = 0.90) -> bool:
    """Conservative title comparison used only to confirm a DOI duplicate."""

    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    return overlap >= threshold


# --------------------------------------------------------------------------
# view records
# --------------------------------------------------------------------------


@dataclass
class SourceMaterial:
    """Everything the local program knows about one selected handle."""

    source_handle: str
    paper_id: str = ""
    title: str = ""
    year: str = ""
    doi: str = ""
    material_depth: str = ""
    card_path: str = ""
    planning_view: dict[str, Any] = field(default_factory=dict)
    study_summary_a: dict[str, Any] = field(default_factory=dict)
    deep_read_material: dict[str, Any] = field(default_factory=dict)
    deep_read_materials: list[dict[str, Any]] = field(default_factory=list)
    supplement_material: dict[str, Any] = field(default_factory=dict)
    supplement_materials: list[dict[str, Any]] = field(default_factory=list)
    local_passages: dict[str, Any] = field(default_factory=dict)
    local_passages_variants: list[dict[str, Any]] = field(default_factory=list)
    tool_supplement_materials: list[dict[str, Any]] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    material_status: str = "resolved"  # resolved | unresolvable_handle | no_material

    def to_dict(self, *, include_material: bool = True) -> dict[str, Any]:
        row = {
            "source_handle": self.source_handle,
            "paper_id": self.paper_id,
            "title": self.title,
            "year": self.year,
            "doi": self.doi,
            "material_depth": self.material_depth,
            "material_status": self.material_status,
            "aliases": list(self.aliases),
        }
        if include_material:
            row["card_path"] = self.card_path
            row["review_planning_B"] = {
                key: self.planning_view.get(key)
                for key in B_FIELDS
                if self.planning_view.get(key) not in (None, "", [], {})
            }
            if self.study_summary_a:
                row["study_summary_A"] = self.study_summary_a
            if self.deep_read_material:
                row["deep_read_material"] = self.deep_read_material
            if self.deep_read_materials:
                row["deep_read_materials"] = [dict(item) for item in self.deep_read_materials]
            if self.supplement_material:
                row["supplement_material"] = self.supplement_material
            if self.supplement_materials:
                row["supplement_materials"] = [dict(item) for item in self.supplement_materials]
            if self.local_passages:
                # Kept under its own reading mode so a local passage is never
                # relabelled as a paid deep read downstream.
                row["local_passages"] = dict(self.local_passages)
            if self.local_passages_variants:
                row["local_passages_variants"] = [dict(item) for item in self.local_passages_variants]
            if self.tool_supplement_materials:
                row["tool_supplement_materials"] = [dict(item) for item in self.tool_supplement_materials]
        return row


@dataclass
class ParagraphBrief:
    paragraph_id: str
    unit_id: str
    ordinal: int
    point: str
    development: str
    source_handles: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paragraph_id": self.paragraph_id,
            "unit_id": self.unit_id,
            "ordinal": self.ordinal,
            "point": self.point,
            "development": self.development,
            "source_handles": list(self.source_handles),
        }


@dataclass
class UnitView:
    unit_id: str
    unit_index: int
    title: str
    substantive_point: str
    ordered_development: str
    paragraph_briefs: list[ParagraphBrief]
    case_uses: list[dict[str, Any]]
    evidence_conditions: str
    synthesis: str
    transition: str
    raw_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "unit_index": self.unit_index,
            "title": self.title,
            "substantive_point": self.substantive_point,
            "ordered_development": self.ordered_development,
            "paragraph_briefs": [brief.to_dict() for brief in self.paragraph_briefs],
            "case_uses": self.case_uses,
            "evidence_conditions": self.evidence_conditions,
            "synthesis": self.synthesis,
            "transition": self.transition,
        }


@dataclass
class ChapterView:
    chapter_id: str
    title: str
    purpose: str
    scope: str
    thesis: str
    reader_objective: str
    research_question: str
    review_argument: str
    other_chapters: list[dict[str, str]]
    units: list[UnitView]
    sources: list[SourceMaterial]
    source_uses: dict[str, list[dict[str, Any]]]
    open_questions: list[str]
    id_map_path: str = ""
    packet_path: str = ""
    # Multi-source or unattributed tool returns for this chapter.  They keep
    # their whole source set and are exported next to the per-handle catalog.
    chapter_tool_materials: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, include_material: bool = True) -> dict[str, Any]:
        return {
            "schema_version": VIEW_SCHEMA,
            "chapter_id": self.chapter_id,
            "title": self.title,
            "purpose": self.purpose,
            "scope": self.scope,
            "thesis": self.thesis,
            "reader_objective": self.reader_objective,
            "research_question": self.research_question,
            "review_argument": self.review_argument,
            "other_chapters": self.other_chapters,
            "units": [unit.to_dict() for unit in self.units],
            "sources": [source.to_dict(include_material=include_material) for source in self.sources],
            "source_uses": self.source_uses,
            "open_questions": list(self.open_questions),
            "id_map_path": self.id_map_path,
            "packet_path": self.packet_path,
            "chapter_tool_materials": (
                [dict(item) for item in self.chapter_tool_materials] if include_material else []
            ),
        }

    def arrangement_payload(self, *, max_source_chars: int = 900) -> dict[str, Any]:
        """The compact payload the arrangement model actually receives.

        The writer packet holds full B material for every source; sending all of
        it would swamp the call and the budget.  The model only needs, per
        source, who it is, how deep the material is, where it is currently used,
        and a one-line purpose — the catalogue with the full material stays
        local and is attached to the exported tasks instead.
        """

        def clip(value: Any) -> Any:
            if isinstance(value, str):
                return value if len(value) <= max_source_chars else value[:max_source_chars] + " …"
            if isinstance(value, list):
                return [clip(item) for item in value]
            if isinstance(value, dict):
                return {key: clip(item) for key, item in value.items()}
            return value

        sources = []
        for source in self.sources:
            uses = self.source_uses.get(source.source_handle, [])
            purpose = str(source.planning_view.get("planning_summary") or "").strip()
            if not purpose:
                purpose = str(source.planning_view.get("planning_summary") or "")
            sources.append({
                "source_handle": source.source_handle,
                "title": clip(source.title),
                "year": source.year,
                "material_depth": source.material_depth,
                "material_status": source.material_status,
                "aliases": list(source.aliases),
                "current_uses": [
                    {
                        "kind": use.get("kind"),
                        "unit_id": use.get("unit_id", ""),
                        "paragraph_id": use.get("paragraph_id", ""),
                        "purpose": clip(str(use.get("text") or "")) if use.get("kind") == "case" else "",
                    }
                    for use in uses
                ],
                "material_purpose": clip(purpose),
                "conditions": clip(
                    "; ".join(str(item) for item in (source.planning_view.get("scope_interpretation_cautions") or [])[:2])
                ),
            })
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "purpose": clip(self.purpose),
            "scope": clip(self.scope),
            "thesis": clip(self.thesis),
            "reader_objective": clip(self.reader_objective),
            "research_question": self.research_question,
            "review_argument": clip(self.review_argument),
            "other_chapters": [
                {"chapter_id": row.get("chapter_id"), "title": row.get("title"), "purpose": clip(row.get("purpose"))}
                for row in self.other_chapters
            ],
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "unit_index": unit.unit_index,
                    "title": unit.title,
                    "substantive_point": clip(unit.substantive_point),
                    "ordered_development": clip(unit.ordered_development),
                    "evidence_conditions": clip(unit.evidence_conditions),
                    "synthesis": clip(unit.synthesis),
                    "transition": clip(unit.transition),
                    "existing_paragraph_tasks": [brief.to_dict() for brief in unit.paragraph_briefs],
                    "case_level_uses": [
                        {
                            "source_handle": row["source_handle"],
                            "field": row["field"],
                            "purpose": clip(row["text"]),
                            "conditions": clip(row["conditions"]),
                        }
                        for row in unit.case_uses
                    ],
                }
                for unit in self.units
            ],
            "sources": sources,
            "open_questions": list(self.open_questions),
        }


# --------------------------------------------------------------------------
# stable ids
# --------------------------------------------------------------------------


class IdMap:
    """Stable local ids for units and paragraphs.

    Ids are assigned once and persisted; a later run with a reordered plan reads
    the saved map instead of inventing new identities.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.units: dict[str, str] = {}
        self.paragraphs: dict[str, str] = {}
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if isinstance(payload, Mapping):
                self.units = {str(k): str(v) for k, v in (payload.get("units") or {}).items()}
                self.paragraphs = {str(k): str(v) for k, v in (payload.get("paragraphs") or {}).items()}
        self._dirty = False

    @staticmethod
    def unit_signature(chapter_id: str, unit_index: int, substantive_point: str) -> str:
        return f"{chapter_id}#u{unit_index}"

    @staticmethod
    def paragraph_signature(unit_id: str, ordinal: int, point: str) -> str:
        return f"{unit_id}#p{ordinal}"

    def unit_id(self, signature: str, fallback: str) -> str:
        if signature not in self.units:
            self.units[signature] = fallback
            self._dirty = True
        return self.units[signature]

    def paragraph_id(self, signature: str, fallback: str) -> str:
        if signature not in self.paragraphs:
            self.paragraphs[signature] = fallback
            self._dirty = True
        return self.paragraphs[signature]

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "schema_version": ID_MAP_SCHEMA,
            "units": self.units,
            "paragraphs": self.paragraphs,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self._dirty = False


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_writer_packet(packet_path: str | Path) -> dict[str, Any]:
    payload = _read_json(Path(packet_path))
    if not isinstance(payload, Mapping):
        raise ChapterArrangementError("writer_packet_must_be_object")
    return dict(payload)


def chapter_identity(packet: Mapping[str, Any]) -> tuple[str, str]:
    """Chapter id and title, from whichever shape the packet uses."""

    plan = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    chapter_id = str(
        plan.get("chapter_id") or packet.get("chapter_id") or chapter.get("chapter_id") or ""
    ).strip()
    title = str(plan.get("title") or chapter.get("title") or packet.get("title") or "").strip()
    if not chapter_id:
        raise ChapterArrangementError("chapter_id_missing")
    return chapter_id, title


def _unit_title(unit: Mapping[str, Any], index: int) -> str:
    for key in ("unit_title", "title", "focus", "unit_focus", "name"):
        value = str(unit.get(key) or "").strip()
        if value:
            return value
    return f"unit {index}"


def _paragraph_rows(unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Paragraph-shaped tasks, in either documented field form."""

    rows: list[dict[str, Any]] = []
    for key in _PARAGRAPH_FIELDS:
        for item in unit.get(key) or ():
            if isinstance(item, Mapping) and (item.get("point") or item.get("development")):
                rows.append(dict(item))
    if not rows:
        # CH06-style units carry the same shape inside ordered_development.
        for item in unit.get(_LEGACY_PARAGRAPH_FIELDS[0]) or ():
            if isinstance(item, Mapping) and (item.get("point") or item.get("development")):
                rows.append(dict(item))
    return rows


def _handles_in(value: Any) -> list[str]:
    """Handles from a field that may be a list, a dict, or free text."""

    found: list[str] = []
    if value is None:
        return found
    if isinstance(value, str):
        found.extend(_HANDLE_RE.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_handles_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_handles_in(item))
    return found


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _case_uses(unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in _CASE_FIELDS:
        for item in unit.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            handle = str(item.get("source_handle") or "").strip()
            if not re.fullmatch(r"P\d{4}", handle):
                handles = _handles_in(item)
                handle = handles[0] if handles else ""
            if not handle:
                continue
            rows.append({
                "field": key,
                "source_handle": handle,
                "paper_id": str(item.get("paper_id") or ""),
                "text": str(
                    item.get("contribution")
                    or item.get("macro_contribution")
                    or item.get("finding")
                    or item.get("use")
                    or ""
                ).strip(),
                "conditions": str(item.get("conditions_limits") or item.get("conditions") or "").strip(),
            })
    return rows


def tool_supplement_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact one tool return for attachment to its single clear source.

    Identity comes from the host source record, so the entry carries the
    research content (question, usable content, conditions, limits) and the
    need it answers, never a guessed paper identity.
    """

    return {
        "need_id": str(item.get("need_id") or ""),
        "question": str(item.get("question") or ""),
        "intended_use": str(item.get("intended_use") or ""),
        "decision": str(item.get("decision") or ""),
        "usable_content": str(item.get("usable_content") or ""),
        "conditions": [str(row) for row in (item.get("conditions") or []) if str(row).strip()],
        "limits": [str(row) for row in (item.get("limits") or []) if str(row).strip()],
        "still_missing": str(item.get("still_missing") or ""),
        "allowed_use": [str(row) for row in (item.get("allowed_use") or []) if str(row).strip()],
        **({"sources": [dict(row) for row in item["sources"] if isinstance(row, Mapping)]}
           if item.get("sources") else {}),
        **({"provenance": dict(item["provenance"])}
           if isinstance(item.get("provenance"), Mapping) else {}),
    }


def compact_chapter_tool_material(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact one multi-source tool return for the chapter-level catalog.

    The source set and the task attribution stay intact: a synthesis built on
    several papers is never folded into one handle, and its chapter/unit
    ownership travels with it.  Sources that never resolved to a handle keep
    the identity fields they arrived with (paper_id/DOI/title), so nothing is
    guessed and nothing is lost.
    """

    entry = tool_supplement_entry(item)
    entry["chapter_ids"] = [str(row) for row in (item.get("chapter_ids") or []) if str(row).strip()]
    entry["unit_key"] = str(item.get("unit_key") or "")
    entry["sources"] = [
        {
            key: str(source.get(key) or "")
            for key in ("source_handle", "paper_id", "canonical_paper_id", "title", "year", "doi",
                        "original_source_handle", "identity_status")
            if str(source.get(key) or "").strip()
        }
        for source in (item.get("sources") or ())
        if isinstance(source, Mapping) and (
            str(source.get("source_handle") or "").strip()
            or str(source.get("paper_id") or "").strip()
            or str(source.get("canonical_paper_id") or "").strip()
            or str(source.get("doi") or "").strip()
            or str(source.get("title") or "").strip()
        )
    ]
    return entry


def _ordered_source_handles(item: Mapping[str, Any]) -> list[str]:
    handles: list[str] = []
    for source in item.get("sources") or ():
        if isinstance(source, Mapping):
            handle = str(source.get("source_handle") or "").strip()
            if handle and handle not in handles:
                handles.append(handle)
    return handles


def _source_identity_key(source: Any) -> str:
    """One stable identity per cited source, handle or not.

    A source without a resolved handle still counts as its own paper via
    paper_id/DOI/title; only a completely anonymous entry is empty.
    """

    if isinstance(source, str):
        text = source.strip()
        return "h:" + text if text else ""
    if not isinstance(source, Mapping):
        return ""
    handle = str(source.get("source_handle") or "").strip()
    paper = str(source.get("canonical_paper_id") or source.get("paper_id") or "").strip()
    if paper:
        return "p:" + paper
    doi = str(source.get("doi") or "").strip()
    if doi:
        return "d:" + normalize_doi(doi)
    if handle:
        return "h:" + handle
    title = str(source.get("title") or "").strip()
    return ("t:" + title.casefold()) if title else ""


def distinct_tool_material_sources(item: Mapping[str, Any]) -> list[str]:
    """Distinct source identities a tool return is built on.

    Deciding single-source attachment on this full set — not on the resolved
    handles alone — is what keeps a synthesis that cites one handle plus one
    paper_id-only source from being folded into the handle's paper.
    """

    keys: list[str] = []
    for source in item.get("sources") or ():
        key = _source_identity_key(source)
        if key and key not in keys:
            keys.append(key)
    return keys


def _tool_supplement_signature(entry: Mapping[str, Any]) -> str:
    """Canonical identity for a complete tool return.

    A need id groups related rounds but says nothing about whether one round
    replaces another.  Compare the whole serialized entry so fields such as
    question, scope and unresolved limitations cannot be lost by projection.
    """
    return json.dumps(
        dict(entry),
        ensure_ascii=False, sort_keys=True, default=str,
    )


def merge_tool_supplement_entry(
    entries: Sequence[Mapping[str, Any]],
    incoming: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Fold one tool supplement into a source's existing list.

    ``need_id`` groups rounds of the same information need; it is not by
    itself a duplicate or replacement signal.  Only a fully identical entry
    folds onto the kept copy.  Every non-identical entry remains available as
    complementary material, including entries whose text happens to contain
    an earlier result.
    """

    merged_incoming = dict(incoming)
    incoming_signature = _tool_supplement_signature(merged_incoming)
    output: list[dict[str, Any]] = []
    for entry in entries:
        if _tool_supplement_signature(entry) == incoming_signature:
            return [dict(item) for item in entries]
        output.append(dict(entry))
    output.append(merged_incoming)
    return output


def _source_catalog(packet: Mapping[str, Any], chapter_id: str) -> dict[str, SourceMaterial]:
    """Local catalog of the chapter's sources, keyed by handle."""

    catalog: dict[str, SourceMaterial] = {}
    for key in ("source_materials",):
        for item in packet.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            handle = str(item.get("source_handle") or "").strip()
            if not handle:
                continue
            catalog[handle] = SourceMaterial(
                source_handle=handle,
                paper_id=str(item.get("paper_id") or ""),
                title=str(item.get("title") or ""),
                year=str(item.get("year") or ""),
                doi=str(item.get("doi") or ""),
                material_depth=str(item.get("material_depth") or ""),
                card_path=str(item.get("card_path") or ""),
                planning_view=dict(item.get("review_planning_B") or {}) if isinstance(item.get("review_planning_B"), Mapping) else {},
                study_summary_a=dict(item.get("study_summary_A") or {}) if isinstance(item.get("study_summary_A"), Mapping) else {},
                deep_read_material=dict(item.get("deep_read_material") or {}) if isinstance(item.get("deep_read_material"), Mapping) else {},
                deep_read_materials=[
                    dict(row) for row in (item.get("deep_read_materials") or [])
                    if isinstance(row, Mapping)
                ],
                supplement_material=dict(
                    item.get("supplement_gap_material") or item.get("supplement_material") or {})
                if isinstance(item.get("supplement_gap_material") or item.get("supplement_material"), Mapping)
                else {},
                supplement_materials=[
                    dict(row) for row in (
                        item.get("supplement_gap_materials") or item.get("supplement_materials") or [])
                    if isinstance(row, Mapping)
                ],
                local_passages=dict(item.get("local_passages") or {}) if isinstance(item.get("local_passages"), Mapping) else {},
                local_passages_variants=[
                    dict(row) for row in (item.get("local_passages_variants") or [])
                    if isinstance(row, Mapping)
                ],
                tool_supplement_materials=[
                    dict(row) for row in (item.get("tool_supplement_materials") or [])
                    if isinstance(row, Mapping)
                ],
            )
    identity = packet.get("source_identity_map") if isinstance(packet.get("source_identity_map"), Mapping) else {}
    for handle, row in identity.items():
        if not isinstance(row, Mapping):
            continue
        entry = catalog.setdefault(str(handle), SourceMaterial(source_handle=str(handle)))
        current_paper = str(row.get("canonical_paper_id") or row.get("paper_id") or "")
        current_doi = normalize_doi(row.get("doi"))
        if ((entry.paper_id and current_paper and entry.paper_id != current_paper)
                or (entry.doi and current_doi and normalize_doi(entry.doi) != current_doi)):
            raise ChapterArrangementError("source_identity_conflict:" + str(handle))
        if not entry.paper_id:
            entry.paper_id = current_paper
        if not entry.title:
            entry.title = str(row.get("title") or "")
        if not entry.year:
            entry.year = str(row.get("year") or "")
        if not entry.doi:
            entry.doi = str(row.get("doi") or "")
        if not entry.card_path:
            entry.card_path = str(row.get("card_path") or "")
    return catalog


def _resolved_tool_materials(
    packet: Mapping[str, Any], catalog: dict[str, SourceMaterial],
) -> list[dict[str, Any]]:
    """Resolve tool references against current identities without trusting old labels.

    Already assigned, unclaimed tool handles with a stable identity are usable;
    stable-ID-only sources without a current handle remain visible and unresolved.
    Multi-source content is never copied into a single source's study summary.
    """

    def compatible(row: Mapping[str, Any], target: SourceMaterial) -> bool:
        paper = str(row.get("canonical_paper_id") or row.get("paper_id") or "").strip()
        doi = normalize_doi(row.get("doi"))
        title = normalize_title(row.get("title"))
        title_conflict = (not paper and not doi and title and target.title
                          and title != normalize_title(target.title))
        return not ((paper and target.paper_id and paper != target.paper_id)
                    or (doi and target.doi and doi != normalize_doi(target.doi))
                    or title_conflict)

    output: list[dict[str, Any]] = []
    raw_materials = [*(packet.get("tool_materials") or ()),
                     *(packet.get("chapter_tool_materials") or ())]
    for material in packet.get("source_materials") or ():
        if isinstance(material, Mapping):
            raw_materials.extend(material.get("tool_materials") or ())
    seen: set[str] = set()
    for raw in raw_materials:
        if not isinstance(raw, Mapping):
            continue
        signature = json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        item = dict(raw)
        sources: list[dict[str, Any]] = []
        for source in raw.get("sources") or ():
            if not isinstance(source, Mapping):
                continue
            row = dict(source)
            old = str(row.get("source_handle") or "").strip()
            paper = str(row.get("canonical_paper_id") or row.get("paper_id") or "").strip()
            doi = normalize_doi(row.get("doi"))
            identity_matches = [handle for handle, target in catalog.items()
                                if (paper and paper == target.paper_id)
                                or (doi and doi == normalize_doi(target.doi))]
            matches = [handle for handle in identity_matches if compatible(row, catalog[handle])]
            conflicting_identity = any(not compatible(row, catalog[handle]) for handle in identity_matches)
            resolved = matches[0] if len(matches) == 1 and not conflicting_identity else ""
            if not resolved and not identity_matches and old in catalog and compatible(row, catalog[old]):
                resolved = old
            if not resolved and not identity_matches and old not in catalog and re.fullmatch(r"P\d{4}", old) and (paper or doi):
                # Preserve an existing tool assignment; never allocate a new handle.
                catalog[old] = SourceMaterial(source_handle=old, paper_id=paper,
                    title=str(row.get("title") or ""), year=str(row.get("year") or ""),
                    doi=str(row.get("doi") or ""))
                resolved = old
            if resolved:
                row["source_handle"] = resolved
                if old and old != resolved:
                    row["original_source_handle"] = old
                    row["identity_status"] = "remapped"
                target = catalog[resolved]
                for key in ("paper_id", "title", "year", "doi"):
                    if not row.get(key) and getattr(target, key):
                        row[key] = getattr(target, key)
            else:
                row.pop("source_handle", None)
                if old:
                    row["original_source_handle"] = old
                row["identity_status"] = row.get("identity_status") or (
                    "conflicting_identity" if conflicting_identity else
                    "conflicting_handle" if old in catalog else "unresolved_identity")
            sources.append(row)
        item["sources"] = sources
        output.append(item)
    return output


def resolve_tool_materials(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Identity-safe tool rows for packet merging and downstream handoff."""

    return _resolved_tool_materials(packet, _source_catalog(packet, ""))


def chapter_tool_materials_from_packet(packet: Mapping[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split packet-level tool returns into per-source and chapter-level parts.

    Returns ``(per_source, chapter_level)``.  An item attaches to a single
    source only when its COMPLETE source set is one distinct identity that
    resolves to a handle present in the packet's source materials — a second
    source that never resolved to a handle (paper_id/DOI/title only) keeps
    the whole item chapter-level with both identities.  Items with several
    sources, an unknown handle or no handle at all stay chapter-level with
    their source set intact; ownership is never guessed.
    """

    catalog = _source_catalog(packet, "")
    materials = _resolved_tool_materials(packet, catalog)
    catalog_handles = {
        str(row.get("source_handle") or "")
        for row in packet.get("source_materials") or () if isinstance(row, Mapping)
    }
    per_source: dict[str, list[dict[str, Any]]] = {}
    chapter_level: list[dict[str, Any]] = []
    for raw in materials:
        identities = distinct_tool_material_sources(raw)
        handles = _ordered_source_handles(raw)
        if (len(identities) == 1 and len(handles) == 1 and handles[0] in catalog_handles
                and not str(raw.get("unit_key") or "").strip()):
            per_source[handles[0]] = merge_tool_supplement_entry(
                per_source.get(handles[0]) or [], tool_supplement_entry(raw))
            continue
        chapter_level.append(compact_chapter_tool_material(raw))
    return per_source, chapter_level


def _material_signature(value: Mapping[str, Any]) -> str:
    """Stable full-content identity for serialized source material."""

    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)


def _unique_materials(values: Iterable[Any]) -> list[dict[str, Any]]:
    """Keep every distinct mapping in arrival order."""

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        if not item:
            continue
        signature = _material_signature(item)
        if signature in seen:
            continue
        seen.add(signature)
        output.append(item)
    return output


def _merge_duplicate_source_material(keeper: SourceMaterial, source: SourceMaterial) -> None:
    """Fold duplicate-record material without overwriting complementary data."""

    deep_reads = _unique_materials([
        keeper.deep_read_material,
        *keeper.deep_read_materials,
        source.deep_read_material,
        *source.deep_read_materials,
    ])
    keeper.deep_read_material = deep_reads[0] if deep_reads else {}
    keeper.deep_read_materials = deep_reads[1:]

    supplements = _unique_materials([
        keeper.supplement_material,
        *keeper.supplement_materials,
        source.supplement_material,
        *source.supplement_materials,
    ])
    keeper.supplement_material = supplements[0] if supplements else {}
    keeper.supplement_materials = supplements[1:]

    local_passages = _unique_materials([
        keeper.local_passages,
        *keeper.local_passages_variants,
        source.local_passages,
        *source.local_passages_variants,
    ])
    keeper.local_passages = local_passages[0] if local_passages else {}
    keeper.local_passages_variants = local_passages[1:]

    for item in source.tool_supplement_materials:
        keeper.tool_supplement_materials = merge_tool_supplement_entry(
            keeper.tool_supplement_materials, item)


def merge_doi_duplicates(
    sources: Sequence[SourceMaterial],
    uses: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[SourceMaterial], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Merge obvious same-DOI duplicates and keep every use.

    A merge needs the same normalised DOI *and* a title that is not visibly
    different.  Similar titles alone are never merged: a preprint and its
    published version are separate sources on purpose.
    """

    merged: list[SourceMaterial] = []
    by_doi: dict[str, SourceMaterial] = {}
    merged_uses: dict[str, list[dict[str, Any]]] = {}
    merges: list[dict[str, Any]] = []
    for source in sources:
        doi = normalize_doi(source.doi)
        keeper = by_doi.get(doi) if doi else None
        if keeper is not None and not _title_close(normalize_title(keeper.title), normalize_title(source.title)):
            keeper = None
        if keeper is None:
            merged.append(source)
            merged_uses[source.source_handle] = list(uses.get(source.source_handle, []))
            if doi:
                by_doi[doi] = source
            continue
        keeper.aliases = _dedupe([*keeper.aliases, source.source_handle])
        if not keeper.paper_id and source.paper_id:
            keeper.paper_id = source.paper_id
        # Merged duplicate records keep every distinct material: supplements,
        # deep reads, local passages and tool returns move to the kept handle
        # instead of disappearing with the alias or overwriting a colliding key.
        _merge_duplicate_source_material(keeper, source)
        merged_uses[keeper.source_handle] = [*merged_uses.get(keeper.source_handle, []), *uses.get(source.source_handle, [])]
        merges.append({
            "kept": keeper.source_handle,
            "merged": source.source_handle,
            "basis": "same_normalized_doi_and_close_title",
            "doi": doi,
        })
    return merged, merged_uses, merges


def build_chapter_view(
    packet_path: str | Path,
    *,
    shared_outline: Sequence[Mapping[str, Any]] | None = None,
    review_argument: str = "",
    id_map_path: str | Path | None = None,
) -> ChapterView:
    """Build the arrangement view for one chapter from its writer packet."""

    packet = load_writer_packet(packet_path)
    chapter_id, title = chapter_identity(packet)
    plan = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    catalog = _source_catalog(packet, chapter_id)
    packet["tool_materials"] = _resolved_tool_materials(packet, catalog)

    resolved_id_map = Path(id_map_path) if id_map_path else (
        Path(packet_path).parent.parent / "chapter_arrangement" / "ID_MAP.json"
    )
    ids = IdMap(resolved_id_map)

    units: list[UnitView] = []
    uses: dict[str, list[dict[str, Any]]] = {}
    paragraph_handles: set[str] = set()
    for index, unit in enumerate(plan.get("units") or (), start=1):
        if not isinstance(unit, Mapping):
            continue
        substantive = str(unit.get("substantive_point") or "").strip()
        signature = IdMap.unit_signature(chapter_id, index, substantive)
        unit_id = ids.unit_id(signature, f"{chapter_id}_U{index:02d}")
        briefs: list[ParagraphBrief] = []
        for ordinal, row in enumerate(_paragraph_rows(unit), start=1):
            point = str(row.get("point") or "").strip()
            handles = tuple(_dedupe(_handles_in(row.get("source_handles")) or _handles_in(row)))
            paragraph_signature = IdMap.paragraph_signature(unit_id, ordinal, point)
            paragraph_id = ids.paragraph_id(paragraph_signature, f"{unit_id}_P{ordinal:02d}")
            briefs.append(ParagraphBrief(
                paragraph_id=paragraph_id,
                unit_id=unit_id,
                ordinal=ordinal,
                point=point,
                development=str(row.get("development") or "").strip(),
                source_handles=handles,
                raw=dict(row),
            ))
            paragraph_handles.update(handles)
        case_rows = _case_uses(unit)
        for row in case_rows:
            row["unit_id"] = unit_id
        units.append(UnitView(
            unit_id=unit_id,
            unit_index=index,
            title=_unit_title(unit, index),
            substantive_point=substantive,
            ordered_development=str(unit.get("ordered_development") or ""),
            paragraph_briefs=briefs,
            case_uses=case_rows,
            evidence_conditions=str(
                unit.get("evidence_conditions_and_limits")
                or unit.get("evidence_conditions")
                or unit.get("conditions_and_limits")
                or ""
            ),
            synthesis=str(
                unit.get("cross_paper_synthesis_and_conflicts")
                or unit.get("synthesis_and_conflicts")
                or unit.get("synthesis")
                or unit.get("synthesis_and_limits")
                or ""
            ),
            transition=str(unit.get("transition") or ""),
            raw_keys=tuple(sorted(unit.keys())),
        ))
    ids.save()

    # Every source the chapter actually uses: paragraphs plus case layer.
    for unit in units:
        for brief in unit.paragraph_briefs:
            for handle in brief.source_handles:
                uses.setdefault(handle, []).append({
                    "kind": "paragraph",
                    "unit_id": unit.unit_id,
                    "paragraph_id": brief.paragraph_id,
                    "text": brief.point,
                })
        for row in unit.case_uses:
            uses.setdefault(row["source_handle"], []).append({
                "kind": "case",
                "unit_id": row["unit_id"],
                "field": row["field"],
                "text": row["text"],
                "conditions": row["conditions"],
            })
    for unit in units:
        for handle in _dedupe(row["source_handle"] for row in unit.case_uses):
            # setdefault: a source that already has paragraph uses keeps them.
            uses.setdefault(handle, [])

    # Usable tool material gives its cited studies a selection opportunity even
    # when the earlier owner tasks did not yet name them.
    tool_handles = {
        str(source.get("source_handle") or "")
        for item in packet.get("tool_materials") or ()
        if str(item.get("usable_content") or "").strip()
        for source in item.get("sources") or ()
        if source.get("source_handle") in catalog
    }
    sources: list[SourceMaterial] = []
    for handle in sorted(set(uses) | tool_handles, key=lambda item: (len(item), item)):
        material = catalog.get(handle)
        if material is None:
            material = SourceMaterial(source_handle=handle, material_status="unresolvable_handle")
        elif not (material.planning_view or material.study_summary_a
                  or material.deep_read_material or material.deep_read_materials
                  or material.supplement_material or material.supplement_materials
                  or material.local_passages or material.local_passages_variants
                  or material.tool_supplement_materials or handle in tool_handles):
            material.material_status = "no_material"
        sources.append(material)

    # Tool returns that reached the packet only at top level (older merges and
    # multi-source syntheses) join the local catalog here, never the model
    # payload: single-source items land on their source, the rest stay
    # chapter-level with their source set intact.
    per_source, chapter_level = chapter_tool_materials_from_packet(packet)
    by_handle = {source.source_handle: source for source in sources}
    for handle, entries in per_source.items():
        target = by_handle.get(handle)
        if target is None:
            # An entry whose source is not used by this chapter stays out of
            # the writer view; the packet-level original remains in the packet.
            continue
        for entry in entries:
            target.tool_supplement_materials = merge_tool_supplement_entry(
                target.tool_supplement_materials, entry)

    sources, uses, merges = merge_doi_duplicates(sources, uses)
    aliases = {alias: source.source_handle for source in sources for alias in source.aliases}
    for item in chapter_level:
        for reference in item.get("sources") or ():
            old = str(reference.get("source_handle") or "")
            if old in aliases:
                reference.setdefault("original_source_handle", old)
                reference["source_handle"] = aliases[old]
                reference["identity_status"] = "remapped"
    for source in sources:
        for use in uses.get(source.source_handle, []):
            use["resolved_handle"] = source.source_handle
        # Every use of a merged handle points at the kept handle, so the export
        # never leaves a task referring to a source record that no longer exists.
        for alias in source.aliases:
            for use in uses.get(alias, []):
                use["resolved_handle"] = source.source_handle
                use["recorded_under_alias"] = alias
            uses.setdefault(source.source_handle, [])

    other_chapters = [
        {
            "chapter_id": str(row.get("chapter_id") or ""),
            "title": str(row.get("title") or ""),
            "purpose": str(row.get("purpose") or ""),
        }
        for row in (shared_outline or ())
        if isinstance(row, Mapping) and str(row.get("chapter_id") or "") != chapter_id
    ]

    return ChapterView(
        chapter_id=chapter_id,
        title=title,
        purpose=str(chapter.get("purpose") or plan.get("purpose") or ""),
        scope=str(chapter.get("scope") or plan.get("scope") or plan.get("coordinated_scope") or ""),
        thesis=str(plan.get("thesis") or ""),
        reader_objective=str(plan.get("reader_objective") or ""),
        research_question=str(packet.get("research_question") or ""),
        review_argument=review_argument or str(packet.get("review_argument") or ""),
        other_chapters=other_chapters,
        units=units,
        sources=sources,
        source_uses=uses,
        open_questions=[str(item) for item in (packet.get("open_questions") or []) if str(item).strip()],
        id_map_path=str(resolved_id_map),
        packet_path=str(packet_path),
        chapter_tool_materials=chapter_level,
    )


def _use_index(arrangement: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Where the arrangement placed each handle: paragraph and table tasks."""

    index: dict[str, list[dict[str, Any]]] = {}
    for unit in arrangement.get("units") or ():
        if not isinstance(unit, Mapping):
            continue
        for task in unit.get("paragraph_tasks") or ():
            if not isinstance(task, Mapping):
                continue
            for use in task.get("source_uses") or ():
                if not isinstance(use, Mapping):
                    continue
                handle = str(use.get("source_handle") or "").strip()
                if not handle:
                    continue
                index.setdefault(handle, []).append({
                    "kind": "paragraph",
                    "unit_id": str(unit.get("unit_id") or ""),
                    "paragraph_id": str(task.get("paragraph_id") or ""),
                    "role": str(use.get("role") or ""),
                    "use": str(use.get("use") or ""),
                })
        for table in unit.get("table_tasks") or ():
            if not isinstance(table, Mapping):
                continue
            for row in table.get("row_tasks") or ():
                if not isinstance(row, Mapping):
                    continue
                for use in row.get("source_uses") or ():
                    if not isinstance(use, Mapping):
                        continue
                    handle = str(use.get("source_handle") or "").strip()
                    if not handle:
                        continue
                    index.setdefault(handle, []).append({
                        "kind": "table",
                        "unit_id": str(unit.get("unit_id") or ""),
                        "table_id": str(table.get("table_id") or ""),
                        "role": str(use.get("role") or ""),
                        "use": str(use.get("use") or ""),
                    })
    return index


def source_usage_summary(view: ChapterView, arrangement: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Program-side accounting: which source landed where, or why it did not.

    With an arrangement, the exported rows show the *arranged* placement
    (paragraph/table task ids) and separate the counts: source records, distinct
    papers, task appearances, and papers still only at the case layer.
    """

    placed = _use_index(arrangement) if arrangement else {}
    unused_reasons = {
        str(item.get("source_handle")): str(item.get("reason") or "")
        for item in ((arrangement or {}).get("unused_sources") or ())
        if isinstance(item, Mapping) and item.get("source_handle")
    }
    rows = []
    for source in view.sources:
        uses = view.source_uses.get(source.source_handle, [])
        aliases = list(source.aliases)
        placed_uses = list(placed.get(source.source_handle, []))
        for alias in aliases:
            placed_uses.extend(placed.get(alias, []))
        paragraph_ids = _dedupe(
            [use.get("paragraph_id", "") for use in placed_uses if use.get("kind") == "paragraph"]
            or [use.get("paragraph_id", "") for use in uses if use.get("kind") == "paragraph"]
        )
        table_ids = _dedupe(use.get("table_id", "") for use in placed_uses if use.get("kind") == "table")
        case_units = _dedupe(use.get("unit_id", "") for use in uses if use.get("kind") == "case")
        input_level = (
            "paragraph" if any(use.get("kind") == "paragraph" for use in uses)
            else ("case_only" if case_units else "unused")
        )
        arranged_level = (
            "paragraph" if paragraph_ids
            else ("table" if table_ids else ("case_only" if case_units else "unused"))
        )
        rows.append({
            "source_handle": source.source_handle,
            "paper_id": source.paper_id,
            "title": source.title,
            "doi": source.doi,
            "aliases": aliases,
            "material_status": source.material_status,
            "input_level": input_level,
            "arranged_level": arranged_level if arrangement else "",
            "paragraph_ids": list(paragraph_ids),
            "table_ids": list(table_ids),
            "case_unit_ids": list(case_units),
            "use_count": len(uses),
            "task_appearances": len(placed_uses) if arrangement else 0,
            "placed": [
                {
                    "kind": use.get("kind"),
                    "unit_id": use.get("unit_id"),
                    "task_id": use.get("paragraph_id") or use.get("table_id"),
                    "role": use.get("role"),
                    "use": use.get("use"),
                }
                for use in placed_uses
            ],
            "unused_reason": unused_reasons.get(source.source_handle, ""),
        })
    summary = {
        "schema_version": USAGE_SCHEMA,
        "chapter_id": view.chapter_id,
        "arranged": arrangement is not None,
        "source_records": len(view.sources),
        "distinct_papers": len({row["paper_id"] or row["source_handle"] for row in rows}),
        "task_appearances": sum(row["task_appearances"] for row in rows),
        "sources": rows,
    }
    if arrangement:
        summary.update({
            "at_paragraph_level": sum(1 for row in rows if row["arranged_level"] == "paragraph"),
            "at_table_level": sum(1 for row in rows if row["arranged_level"] == "table"),
            "still_case_only": sum(1 for row in rows if row["arranged_level"] == "case_only"),
            "declared_unused": sum(1 for row in rows if row["unused_reason"]),
            "case_only_entered_tasks": sum(
                1 for row in rows
                if row["input_level"] == "case_only" and row["arranged_level"] in {"paragraph", "table"}
            ),
        })
    else:
        summary.update({
            "at_paragraph_level": sum(1 for row in rows if row["input_level"] == "paragraph"),
            "at_table_level": 0,
            "still_case_only": sum(1 for row in rows if row["input_level"] == "case_only"),
            "declared_unused": 0,
            "case_only_entered_tasks": 0,
        })
    return summary


def build_source_catalog(
    view: ChapterView,
    arrangement: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Locally completed catalogue for the exported tasks.

    The model only ever sees compact one-line purposes; the writer needs the
    real material and a local locator.  This is filled in by the program from
    the view, never by the model, so an exported task can be picked up without
    searching the whole library again.
    """

    placed = _use_index(arrangement) if arrangement else {}
    rows = source_usage_summary(view, arrangement) if arrangement else None
    arranged_level = {
        row["source_handle"]: row.get("arranged_level", "")
        for row in ((rows or {}).get("sources") or ())
    }
    catalog: dict[str, dict[str, Any]] = {}
    for source in view.sources:
        entry = source.to_dict(include_material=True)
        uses = placed.get(source.source_handle, [])
        for alias in source.aliases:
            uses = list(uses) + list(placed.get(alias, []))
        entry["locator"] = {
            "writer_packet": view.packet_path,
            "source_handle": source.source_handle,
            "card_path": source.card_path,
            "resolvable": bool(view.packet_path or source.card_path),
        }
        entry["used_by_tasks"] = _dedupe(
            use.get("paragraph_id") or use.get("table_id") for use in uses
            if use.get("paragraph_id") or use.get("table_id")
        )
        if arranged_level:
            entry["arranged_level"] = arranged_level.get(source.source_handle, "")
        catalog[source.source_handle] = entry
    return catalog


def compact_chapter_tool_materials(view: ChapterView) -> list[dict[str, Any]]:
    """Chapter-level tool materials exported next to the per-handle catalog."""

    return [dict(item) for item in view.chapter_tool_materials]


def render_arrangement_markdown(arrangement: Mapping[str, Any], view: ChapterView) -> str:
    """Readable export of the concrete tasks, with the real source uses."""

    lines: list[str] = []
    title = view.title or arrangement.get("chapter_id") or ""
    lines.append(f"# {arrangement.get('chapter_id', '')} 章内编排：{title}")
    lines.append("")
    validation = arrangement.get("validation") or {}
    status = str(validation.get("status") or ("arranged" if validation.get("ok") else "contract_failed"))
    status_label = {
        "arranged": "已完成（合同通过，全部已选来源有落点）",
        "needs_arrangement": "未完成：有已选来源既未落到任务也未声明不用，需补排（不得当成功）",
        "contract_failed": "合同有问题，未完成",
    }.get(status, status)
    lines.append(
        "状态：%s；单元 %s/%s；段落任务 %s；表格任务 %s；已安排来源 %s；声明未用 %s"
        % (
            status_label,
            validation.get("units_returned"), validation.get("units_expected"),
            validation.get("paragraph_tasks"), validation.get("table_tasks"),
            validation.get("sources_arranged"), validation.get("sources_marked_unused"),
        )
    )
    provenance = arrangement.get("provenance") or {}
    if provenance:
        lines.append(
            "来源说明：%s（模型调用：%s；旧响应：%s）"
            % (provenance.get("note") or "", provenance.get("model_call"),
               provenance.get("revalidated_from") or "-")
        )
    if validation.get("errors"):
        lines.append("")
        lines.append("校验问题：" + "；".join(str(item) for item in validation["errors"]))
    if validation.get("sources_never_mentioned"):
        missing = validation["sources_never_mentioned"]
        titles = {source.source_handle: source.title for source in view.sources}
        lines.append("")
        lines.append("**需要补排：以下已选来源既未落到任何任务，也未声明不用（本章未完成）**")
        for handle in missing:
            title = titles.get(str(handle)) or ""
            lines.append(f"- {handle}｜{title[:90]}")
        lines.append("- 处理方式：把它们排进合适的段落/表格，或写进 `unused_sources` 并给出具体理由；不要为凑覆盖硬塞。")
    if validation.get("paragraph_ids_reused_with_new_point"):
        lines.append(
            "沿用输入 id 但改写了判断的段落（合并前需人看）："
            + ", ".join(validation["paragraph_ids_reused_with_new_point"])
        )
    lines.append("")
    lines.append("本章判断：" + str(arrangement.get("chapter_argument") or ""))
    lines.append("")
    handles = {source.source_handle: source for source in view.sources}
    for unit in arrangement.get("units") or ():
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"## {unit.get('unit_id')} {unit.get('focus') or ''}".rstrip())
        if unit.get("unit_notes"):
            lines.append(f"> 调整说明：{unit['unit_notes']}")
        for task in unit.get("paragraph_tasks") or ():
            lines.append("")
            lines.append(f"### {task.get('paragraph_id')} 段落任务")
            lines.append(f"- 判断：{task.get('point') or ''}")
            lines.append(f"- 展开：{task.get('development') or ''}")
            for use in task.get("source_uses") or ():
                source = handles.get(str(use.get("source_handle")))
                label = f"{use.get('source_handle')}"
                if source and source.title:
                    label += f"（{source.title[:60]}）"
                lines.append(f"- 来源 {label}｜角色：{use.get('role') or '未标'}｜用途：{use.get('use') or ''}")
        for table in unit.get("table_tasks") or ():
            lines.append("")
            lines.append(f"### {table.get('table_id')} 表格任务")
            lines.append(f"- 目的：{table.get('purpose') or ''}")
            lines.append("- 列：" + " | ".join(str(item) for item in (table.get("columns") or [])))
            for index, row in enumerate(table.get("row_tasks") or (), start=1):
                sources = ", ".join(
                    f"{use.get('source_handle')}({use.get('role') or '未标'})"
                    for use in row.get("source_uses") or ()
                )
                lines.append(f"  - 第 {index} 行：{row.get('content') or ''}｜来源：{sources}")
    if arrangement.get("unused_sources"):
        lines.append("")
        lines.append("## 声明不使用的来源")
        for item in arrangement["unused_sources"]:
            lines.append(f"- {item.get('source_handle')}：{item.get('reason') or ''}")
    catalog = arrangement.get("source_catalog")
    if isinstance(catalog, Mapping) and catalog:
        lines.append("")
        lines.append("## 来源目录（本地补全，写作者据此取素材）")
        for handle in sorted(catalog):
            entry = catalog[handle] if isinstance(catalog[handle], Mapping) else {}
            parts = [str(handle)]
            title = str(entry.get("title") or "")
            if title:
                parts.append(title[:80] + ("…" if len(title) > 80 else ""))
            if entry.get("year"):
                parts.append(str(entry["year"]))
            if entry.get("doi"):
                parts.append("DOI " + str(entry["doi"]))
            if entry.get("material_depth"):
                parts.append("材料深度 " + str(entry["material_depth"]))
            locator = entry.get("locator") if isinstance(entry.get("locator"), Mapping) else {}
            where = str(locator.get("card_path") or locator.get("writer_packet") or "")
            if where:
                parts.append("位置 " + where + "#" + str(locator.get("source_handle") or handle))
            tasks = entry.get("used_by_tasks") or []
            if tasks:
                parts.append("用于 " + ", ".join(str(item) for item in tasks))
            if entry.get("aliases"):
                parts.append("别名 " + ", ".join(str(item) for item in entry["aliases"]))
            lines.append("- " + "｜".join(parts))
    return "\n".join(lines) + "\n"



def write_view(view: ChapterView, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(view.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# round-scoped budget cap
# --------------------------------------------------------------------------

ROUND_CALL_PREFIX = "chapter-arrangement:20260926-deepseek:"
PROMPT_PATH = Path("prompts/chapter_arrangement_editor.md")
PLANNING_REVISION_INSTRUCTIONS = (
    "\n\n【章节论证模式】这是 opt-in 的论证编排路径。章节负责人已经建立本章 thesis、reader_objective、"
    "units 与科学判断；本步骤只负责把已有判断安排成段落、表格和衔接，不通过 chapter_argument、focus、"
    "point、development、synthesis 或表格字段重新提出未经材料支持的科学结论。不要强制每个单元比较研究"
    "结果，也不要在没有可比材料时补写差异原因。若材料更适合概念解释、方法前提、发展关系、背景、例证"
    "或边界说明，就按实际功能组织；只有确有依据时才比较。若发现科学含义可能需要改变，请在 issues 中"
    "记录具体章节/单元、依据来源和需要章节负责人处理的动作，保留当前任务供调用端回传。相互独立的维度可以并存；"
    "除非材料明确支持，不把它们写成互斥且穷尽的二分路径。\n\n"
    "【段落任务引用合同】existing_paragraph_tasks 是章节负责人已写好的具体任务，其中的研究对象、工况"
    "（温度/压力等）、数字与分母关系、比较对象和限制条件必须原样保留给写作者。因此：每个 paragraph_task "
    "用 `source_briefs` 列出它来自哪些 `paragraph_id`（合并写多个 id；拆分把同一 id 写进多个任务，并在 "
    "`portion` 说明各段讲原任务的哪一部分）。`point`/`development` 可以省略——程序会把负责人原文回填；"
    "你补充的是顺序、`source_uses`（每篇来源在这段做什么）与衔接意图，不是新的研究描述，也不要把具体"
    "条件概括成“特定条件”之类的话。每个已有段落 id 都必须被至少一个任务引用，漏掉任何一个都算本任务未完成。"
)


class RoundBudgetExceeded(ChapterArrangementError):
    """The round's own incremental ceiling would be exceeded."""


class RoundCappedLedger:
    """A per-round ceiling wrapped around the shared budget ledger.

    The shared ledger keeps its own 100 CNY total; this adapter adds a second,
    tighter ceiling for this round only.  It counts rows whose ``call_id``
    carries :data:`ROUND_CALL_PREFIX`, so restarts keep counting and the round
    can never reset itself, while the global ledger still enforces the total.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        round_cap_cny: float = 10.0,
        round_prefix: str = ROUND_CALL_PREFIX,
        ledger_factory: Any | None = None,
    ) -> None:
        from .module4.runtime import GlobalBudgetLedger

        self.path = Path(path)
        self.round_cap_cny = float(round_cap_cny)
        self.round_prefix = round_prefix
        factory = ledger_factory or GlobalBudgetLedger
        # The global ledger keeps its own stored limit; never pass the round cap
        # here, or the shared total would be silently rewritten.
        self.ledger = factory(path=self.path)
        # Load the stored total/actuals so the shared ledger's own state is
        # visible without any extra call.
        try:
            self.ledger._refresh_from_db()
        except AttributeError:
            pass
        self.reservations: list[dict[str, Any]] = []

    # -- round accounting --------------------------------------------------

    def _round_spend(self) -> tuple[float, float]:
        """Settled and held CNY for this round's call ids."""

        import sqlite3

        with sqlite3.connect(str(self.path), timeout=30.0) as db:
            row = db.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_cny, amount_cny) ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') THEN amount_cny ELSE 0 END),0) "
                "FROM reservations WHERE call_id LIKE ?",
                (self.round_prefix + "%",),
            ).fetchone()
        return float(row[0] or 0.0), float(row[1] or 0.0)

    def round_state(self) -> dict[str, Any]:
        settled, held = self._round_spend()
        return {
            "round_prefix": self.round_prefix,
            "round_cap_cny": self.round_cap_cny,
            "round_settled_cny": settled,
            "round_held_cny": held,
            "round_remaining_cny": max(0.0, self.round_cap_cny - settled - held),
            "ledger_path": str(self.path),
        }

    # -- ledger interface --------------------------------------------------

    def reserve(self, amount_cny: float, call_id: str) -> dict[str, Any]:
        amount = max(0.0, float(amount_cny))
        call = str(call_id)
        if call.startswith(self.round_prefix):
            settled, held = self._round_spend()
            if settled + held + amount > self.round_cap_cny + 1e-9:
                raise RoundBudgetExceeded("round_budget_exceeded")
        reservation = self.ledger.reserve(amount, call)
        self.reservations.append(dict(reservation))
        return reservation

    def settle(self, reservation_id: str, actual_cny: float | None, **kwargs: Any) -> None:
        self.ledger.settle(reservation_id, actual_cny, **kwargs)

    def as_dict(self) -> dict[str, Any]:
        payload = self.ledger.as_dict()
        payload.update(self.round_state())
        return payload

    @property
    def limit_cny(self) -> float | None:
        return self.ledger.limit_cny

    @property
    def actual_cny(self) -> float:
        return self.ledger.actual_cny

    @property
    def reserved_cny(self) -> float:
        return self.ledger.reserved_cny


# --------------------------------------------------------------------------
# model entry point
# --------------------------------------------------------------------------


def load_editor_prompt(path: str | Path | None = None, *, planning_revision: bool = False) -> str:
    target = Path(path) if path else Path(__file__).resolve().parents[3] / PROMPT_PATH
    if not target.is_file():
        raise ChapterArrangementError("editor_prompt_missing:" + str(target))
    prompt = target.read_text(encoding="utf-8")
    if not planning_revision:
        return prompt
    # Remove the old broad-comparison wording in the opt-in prompt itself;
    # appending a permission note would leave contradictory instructions.
    prompt = prompt.replace(
        "多篇同段时，要写清它们之间的关系（一致、相反、条件不同、还是互补），不要轮流介绍。",
        "多篇同段时，按材料实际关系写清互补、条件不同或其他有依据的联系；材料不形成比较时，不补造比较。",
    ).replace(
        "`development` 要足以让写作者组织比较，不能只写“介绍 A，介绍 B”。",
        "`development` 要足以让写作者组织本段实际论证关系，不能只写“介绍 A，介绍 B”；不强制比较。",
    ).replace(
        "4. **保留原有有效任务**：已有段落任务是有用起点，可以重排、合并、扩充或改写，但不要因为输出省字而整段删除。"
        "确需删除或大幅改动时，在 `unit_notes` 里写明理由。",
        "4. **引用负责人原任务，不重写研究内容**：用 `source_briefs` 引用 `existing_paragraph_tasks` 的 "
        "`paragraph_id`；`point`/`development` 可省略，程序会回填负责人原文。你决定的是顺序、来源用途与衔接；"
        "不要把具体工况概括成“特定条件”。每个已有段落 id 都必须被至少一个任务引用。",
    )
    return prompt + PLANNING_REVISION_INSTRUCTIONS


def estimate_arrangement_cost(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    output_tokens: int,
    thinking_budget: int,
    token_counter: Any | None = None,
) -> dict[str, Any]:
    """Conservative pre-dispatch estimate for one arrangement call."""

    from .module4.runtime import estimated_cost_cny

    prompt_tokens = 0
    if token_counter is not None:
        prompt_tokens = int(token_counter(b"", messages))
    else:
        raw = json.dumps(messages, ensure_ascii=False)
        prompt_tokens = max(1, len(raw) // 3)
    reserved_input = int(prompt_tokens * 1.12) + 8192
    total_context = reserved_input + int(output_tokens) + int(thinking_budget)
    return {
        "prompt_tokens_estimate": prompt_tokens,
        "reserved_input_tokens": reserved_input,
        "output_tokens": int(output_tokens),
        "thinking_budget": int(thinking_budget),
        "total_context_tokens": total_context,
        "estimated_cost_cny": estimated_cost_cny(
            {"prompt_tokens": reserved_input, "completion_tokens": int(output_tokens) + int(thinking_budget)},
            model=model,
            conservative=True,
        ),
        "model": model,
    }


def arrangement_messages(
    view_payload: Mapping[str, Any], *, prompt: str | None = None,
    planning_revision: bool = False,
) -> list[dict[str, str]]:
    planning_revision = bool(planning_revision or view_payload.get("planning_revision_mode"))
    body = prompt if prompt is not None else load_editor_prompt(planning_revision=planning_revision)
    if planning_revision and prompt is not None and PLANNING_REVISION_INSTRUCTIONS not in body:
        body += PLANNING_REVISION_INSTRUCTIONS
    return [
        {"role": "system", "content": body},
        {"role": "user", "content": json.dumps(view_payload, ensure_ascii=False, separators=(",", ":"))},
    ]


# --------------------------------------------------------------------------
# output contract
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_arrangement_response(raw: Any) -> dict[str, Any]:
    """Read the editor's JSON object from a client response."""

    content = raw
    if isinstance(raw, Mapping):
        if isinstance(raw.get("content"), str):
            content = raw["content"]
        elif isinstance(raw.get("response"), Mapping):
            return dict(raw["response"])
        elif "units" in raw or "chapter_id" in raw:
            return dict(raw)
        else:
            raise ChapterArrangementError("arrangement_response_not_json")
    if not isinstance(content, str):
        raise ChapterArrangementError("arrangement_response_not_text")
    text = _FENCE_RE.sub("", content.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json

            parsed = repair_json(text, return_objects=True)
        except Exception as exc:  # noqa: BLE001 - report the contract failure
            raise ChapterArrangementError("arrangement_response_unparsable") from exc
    if not isinstance(parsed, Mapping):
        raise ChapterArrangementError("arrangement_response_must_be_object")
    return dict(parsed)


def _normalize_uses(value: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in value or ():
        if isinstance(item, Mapping):
            handle = str(item.get("source_handle") or "").strip()
            if not handle:
                continue
            rows.append({
                "source_handle": handle,
                "role": str(item.get("role") or "").strip(),
                "use": str(item.get("use") or item.get("purpose") or "").strip(),
            })
        elif isinstance(item, str) and _HANDLE_RE.fullmatch(item.strip()):
            rows.append({"source_handle": item.strip(), "role": "", "use": ""})
    return rows


def _normalize_point(value: str) -> str:
    """Compare paragraph points without being fooled by spacing or punctuation."""

    text = str(value or "").strip().lower()
    return "".join(char for char in text if not char.isspace() and char not in "。，、；：,.;:!！?？“”\"'（）()")


def _normalize_issues(value: Any) -> list[dict[str, Any]]:
    """Keep a small caller-facing issue envelope without interpreting it here."""

    issues: list[dict[str, Any]] = []
    for raw in value or ():
        if isinstance(raw, Mapping):
            issue = {
                key: raw[key]
                for key in ("issue_id", "chapter_id", "unit_id", "paragraph_id", "problem", "source_handles", "action", "status")
                if key in raw and raw[key] not in (None, "", [], {})
            }
        elif isinstance(raw, str) and raw.strip():
            issue = {"problem": raw.strip()}
        else:
            continue
        if issue:
            issues.append(issue)
    return issues


def _restore_owner_paragraph_briefs(
    unit_id: str,
    paragraphs: list[dict[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]],
    explicit_ids: Sequence[bool],
    unit: UnitView,
    known_handles: set[str],
    errors: list[str],
) -> list[str]:
    """Restore the owner's original paragraph tasks in the opt-in mode.

    The arrangement model references tasks instead of rewriting them: each
    output task lists the original ``paragraph_id`` values it covers in
    ``source_briefs`` (merge = several ids, split = the same id in several
    tasks with a ``portion`` note).  The program then puts the owner's
    point/development back verbatim, so a condensing model cannot strip the
    conditions that decide what a finding means.  A task without references
    whose id the MODEL explicitly wrote and that matches an original brief is
    an explicit reuse and is restored too; an id the program generated from
    list position is never treated as a claim (no positional guessing), and
    such tasks keep the model's text and are reported as unmapped.  Every
    original brief must be claimed by at least one task — an unclaimed brief
    is a validation error, never a silent drop.  Returns the unmapped task
    ids for the unit record.
    """

    briefs_by_id = {brief.paragraph_id: brief for brief in unit.paragraph_briefs}
    if not briefs_by_id:
        return []
    claimed: set[str] = set()
    unmapped: list[str] = []
    for entry, raw, explicit_id in zip(paragraphs, raw_rows, explicit_ids):
        refs_raw = raw.get("source_briefs")
        refs = [str(item).strip() for item in refs_raw if str(item).strip()] \
            if isinstance(refs_raw, list) else []
        bad = [ref for ref in refs if ref not in briefs_by_id]
        if bad:
            errors.append(f"brief_reference_unknown:{unit_id}:{','.join(bad)}")
            refs = [ref for ref in refs if ref in briefs_by_id]
        if not refs and explicit_id and entry["paragraph_id"] in briefs_by_id:
            refs = [entry["paragraph_id"]]
        if refs:
            claimed.update(refs)
            briefs = [briefs_by_id[ref] for ref in refs]
            primary = briefs[0]
            entry["point"] = primary.point
            entry["development"] = "\n\n".join(
                brief.development for brief in briefs if brief.development).strip()
            entry["source_briefs"] = refs
            # Keep the complete local owner tasks beside the compact
            # references. source_uses is an arrangement-level view and
            # cannot tell the writer which source belonged to which original
            # brief after a merge.
            entry["source_brief_details"] = [brief.to_dict() for brief in briefs]
            entry["carried_over"] = True
            entry["point_changed_from_brief"] = False
            # A missing output id already received a unit-local fallback in
            # validate_arrangement. Keep that unique task id: the owner brief
            # id is a reference, not the identity of a split output task.
            existing_handles = {use["source_handle"] for use in entry["source_uses"]}
            for brief in briefs:
                for handle in brief.source_handles:
                    if handle not in existing_handles and handle in known_handles:
                        entry["source_uses"].append(
                            {"source_handle": handle, "role": "负责人指定", "use": ""})
                        existing_handles.add(handle)
        else:
            unmapped.append(entry["paragraph_id"])
        portion = str(raw.get("portion") or "").strip()
        if portion:
            entry["portion"] = portion
    unclaimed = sorted(ref for ref in briefs_by_id if ref not in claimed)
    if unclaimed:
        errors.append(f"briefs_unclaimed:{unit_id}:{','.join(unclaimed)}")
    return unmapped


def validate_arrangement(
    payload: Mapping[str, Any],
    view: ChapterView,
    *,
    planning_revision: bool = False,
) -> dict[str, Any]:
    """Check a parsed arrangement against the input it must belong to.

    Rules taken from the repair history: a wrong chapter id, a unit that was
    never in the plan, an unknown handle, or a missing unit are reported, never
    silently accepted or relabelled by position.
    """

    chapter_id, _title = chapter_identity({"chapter_plan": {"chapter_id": view.chapter_id}})
    reported_chapter = str(payload.get("chapter_id") or "").strip()
    errors: list[str] = []
    if reported_chapter and reported_chapter != view.chapter_id:
        errors.append(f"chapter_id_mismatch:{reported_chapter}!={view.chapter_id}")
    if not reported_chapter:
        errors.append("chapter_id_missing")

    known_units = {unit.unit_id: unit for unit in view.units}
    canonical_handles = {source.source_handle for source in view.sources}
    alias_to_canonical = {
        alias: source.source_handle
        for source in view.sources
        for alias in source.aliases
    }
    known_handles = set(canonical_handles)
    for source in view.sources:
        known_handles.update(source.aliases)

    def canonical_handle(handle: str) -> str:
        """Resolve an existing catalogue alias without inventing identities."""

        return alias_to_canonical.get(handle, handle)
    known_paragraphs = {
        brief.paragraph_id for unit in view.units for brief in unit.paragraph_briefs
    }
    brief_points = {
        brief.paragraph_id: _normalize_point(brief.point)
        for unit in view.units for brief in unit.paragraph_briefs
    }
    known_tables: set[str] = set()

    units_out: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    unknown_handles: set[str] = set()
    for raw_unit in payload.get("units") or ():
        if not isinstance(raw_unit, Mapping):
            errors.append("unit_not_object")
            continue
        unit_id = str(raw_unit.get("unit_id") or "").strip()
        # Unknown handles are reported even when the unit itself is rejected, so
        # a wrong unit id can never hide a fabricated citation.
        for handle in _handles_in(raw_unit):
            if handle not in known_handles:
                unknown_handles.add(handle)
        if unit_id not in known_units:
            errors.append(f"unit_id_unknown:{unit_id or '<empty>'}")
            continue
        if unit_id in seen_units:
            errors.append(f"unit_id_duplicated:{unit_id}")
            continue
        seen_units.add(unit_id)
        paragraphs: list[dict[str, Any]] = []
        raw_paragraph_rows: list[Mapping[str, Any]] = []
        explicit_paragraph_ids: list[bool] = []
        raw_tasks = list(raw_unit.get("paragraph_tasks") or ())
        explicit_ids_in_output = {
            str(raw.get("paragraph_id") or "").strip()
            for raw in raw_tasks
            if isinstance(raw, Mapping) and str(raw.get("paragraph_id") or "").strip()
        }
        generated_ids: set[str] = set()
        for ordinal, raw_paragraph in enumerate(raw_tasks, start=1):
            if not isinstance(raw_paragraph, Mapping):
                errors.append(f"paragraph_not_object:{unit_id}")
                # No paragraph entry is appended, so the three parallel
                # arrays consumed by _restore_owner_paragraph_briefs remain
                # aligned for the following valid row.
                continue
            paragraph_id = str(raw_paragraph.get("paragraph_id") or "").strip()
            explicit_id = bool(paragraph_id)
            explicit_paragraph_ids.append(explicit_id)
            if not paragraph_id:
                paragraph_id = f"{unit_id}_P{ordinal:02d}"
                if planning_revision and (
                    paragraph_id in known_paragraphs
                    or paragraph_id in explicit_ids_in_output
                    or paragraph_id in generated_ids
                ):
                    paragraph_id = f"{unit_id}__TASK_{ordinal:02d}"
                    suffix = 2
                    while paragraph_id in known_paragraphs or paragraph_id in explicit_ids_in_output or paragraph_id in generated_ids:
                        paragraph_id = f"{unit_id}__TASK_{ordinal:02d}_{suffix:02d}"
                        suffix += 1
                generated_ids.add(paragraph_id)
            elif paragraph_id in known_paragraphs and not paragraph_id.startswith(unit_id):
                errors.append(f"paragraph_id_unit_mismatch:{paragraph_id}")
            uses = _normalize_uses(raw_paragraph.get("source_uses"))
            for use in uses:
                if use["source_handle"] not in known_handles:
                    unknown_handles.add(use["source_handle"])
            point = str(raw_paragraph.get("point") or "").strip()
            # An id reused from the input keeps the task's identity (safe for a
            # later merge by id), but the judgement may have been rewritten.  Both
            # facts are reported so nothing is inferred from the id alone.
            previous_point = brief_points.get(paragraph_id)
            reused = previous_point is not None
            kept_point = reused and previous_point == _normalize_point(point)
            paragraphs.append({
                "paragraph_id": paragraph_id,
                "point": point,
                "development": str(raw_paragraph.get("development") or "").strip(),
                "source_uses": uses,
                "carried_over": reused,
                "point_changed_from_brief": reused and not kept_point,
            })
            raw_paragraph_rows.append(raw_paragraph)
        if planning_revision:
            unmapped_tasks = _restore_owner_paragraph_briefs(
                unit_id, paragraphs, raw_paragraph_rows, explicit_paragraph_ids,
                known_units[unit_id], known_handles, errors,
            )
        tables: list[dict[str, Any]] = []
        for ordinal, raw_table in enumerate(raw_unit.get("table_tasks") or (), start=1):
            if not isinstance(raw_table, Mapping):
                continue
            table_id = str(raw_table.get("table_id") or f"{unit_id}_T{ordinal:02d}").strip()
            if table_id in known_tables:
                errors.append(f"table_id_duplicated:{table_id}")
            known_tables.add(table_id)
            rows = []
            for raw_row in raw_table.get("row_tasks") or ():
                if not isinstance(raw_row, Mapping):
                    continue
                uses = _normalize_uses(raw_row.get("source_uses"))
                for use in uses:
                    if use["source_handle"] not in known_handles:
                        unknown_handles.add(use["source_handle"])
                rows.append({
                    "content": str(raw_row.get("content") or "").strip(),
                    "source_uses": uses,
                })
            tables.append({
                "table_id": table_id,
                "purpose": str(raw_table.get("purpose") or "").strip(),
                "columns": [str(item) for item in (raw_table.get("columns") or ())],
                "row_tasks": rows,
            })
        unit_title = str(raw_unit.get("unit_title") or raw_unit.get("title") or "").strip()
        owner_unit = known_units[unit_id]
        units_out.append({
            "unit_id": unit_id,
            "focus": str(raw_unit.get("focus") or owner_unit.substantive_point).strip(),
            # A model-supplied short title travels with the unit so the
            # assembly can head the section with it instead of truncating the
            # focus assertion; the arrangement model payload stays unchanged.
            **({"unit_title": unit_title} if unit_title else {}),
            "paragraph_tasks": paragraphs,
            "table_tasks": tables,
            "unit_notes": str(raw_unit.get("unit_notes") or "").strip(),
            **({
                # Model-added tasks that reference no owner brief: allowed
                # (connective organization), but visible instead of silent.
                **({"unmapped_paragraph_tasks": unmapped_tasks} if unmapped_tasks else {}),
                # The owner unit's own conditions/synthesis/transition travel
                # with the unit so paragraph-level content is not the only
                # place those constraints live.
                "owner_unit_context": {
                    "evidence_conditions": owner_unit.evidence_conditions,
                    "synthesis": owner_unit.synthesis,
                    "transition": owner_unit.transition,
                },
            } if planning_revision else {}),
        })

    missing_units = [unit_id for unit_id in known_units if unit_id not in seen_units]
    if missing_units:
        errors.append("units_missing:" + ",".join(sorted(missing_units)))
    if unknown_handles:
        errors.append("source_handle_unknown:" + ",".join(sorted(unknown_handles)))

    unused: list[dict[str, str]] = []
    for item in payload.get("unused_sources") or ():
        if isinstance(item, Mapping) and str(item.get("source_handle") or "").strip():
            handle = str(item["source_handle"]).strip()
            if handle not in known_handles:
                unknown_handles.add(handle)
                errors.append(f"unused_source_unknown:{handle}")
                continue
            unused.append({"source_handle": handle, "reason": str(item.get("reason") or "").strip()})

    arranged_handles = {
        canonical_handle(use["source_handle"])
        for unit in units_out
        for task in unit["paragraph_tasks"]
        for use in task["source_uses"]
    } | {
        canonical_handle(use["source_handle"])
        for unit in units_out
        for table in unit["table_tasks"]
        for row in table["row_tasks"]
        for use in row["source_uses"]
    }
    unused_handles = {canonical_handle(item["source_handle"]) for item in unused}
    never_mentioned = sorted(canonical_handles - arranged_handles - unused_handles)
    # A selected source that is neither placed nor explicitly declared unused
    # means the arrangement is unfinished, not that it succeeded: the chapter's
    # own job is to give every selected source a landing place.
    contract_ok = not errors
    needs_arrangement = bool(never_mentioned)
    status = (
        "contract_failed" if not contract_ok
        else ("needs_arrangement" if needs_arrangement else "arranged")
    )

    return {
        "schema_version": OUTPUT_SCHEMA,
        "chapter_id": view.chapter_id,
        "chapter_argument": view.thesis if planning_revision else str(payload.get("chapter_argument") or "").strip(),
        "units": units_out,
        "unused_sources": unused,
        "issues": _normalize_issues(payload.get("issues")),
        "validation": {
            "ok": contract_ok and not needs_arrangement,
            "contract_ok": contract_ok,
            "needs_arrangement": needs_arrangement,
            "status": status,
            "errors": errors,
            "units_returned": len(units_out),
            "units_expected": len(known_units),
            "paragraph_tasks": sum(len(unit["paragraph_tasks"]) for unit in units_out),
            "paragraph_tasks_carried_over": sum(
                1 for unit in units_out for task in unit["paragraph_tasks"] if task["carried_over"]
            ),
            "paragraph_tasks_with_changed_point": sum(
                1 for unit in units_out for task in unit["paragraph_tasks"]
                if task["point_changed_from_brief"]
            ),
            "paragraph_ids_reused_with_new_point": sorted(
                task["paragraph_id"] for unit in units_out for task in unit["paragraph_tasks"]
                if task["point_changed_from_brief"]
            ),
            "table_tasks": sum(len(unit["table_tasks"]) for unit in units_out),
            "sources_arranged": len(arranged_handles),
            "sources_marked_unused": len(unused_handles),
            "sources_never_mentioned": never_mentioned,
            "missing_sources": never_mentioned,
        },
    }


def run_arrangement(
    view: ChapterView,
    *,
    client: Any,
    model: str,
    prompt: str | None = None,
    view_payload: Mapping[str, Any] | None = None,
    call_id: str = "",
    raw_response_dir: str | Path | None = None,
    planning_revision: bool = False,
    feedback_issues: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One model call, then contract validation against the same view."""

    from .module4.runtime import invoke_client

    payload = dict(view_payload) if view_payload is not None else view.arrangement_payload()
    planning_revision = bool(planning_revision or payload.get("planning_revision_mode"))
    if planning_revision:
        payload["planning_revision_mode"] = True
        if feedback_issues:
            payload["feedback_issues"] = [dict(item) for item in feedback_issues if isinstance(item, Mapping)]
    messages = arrangement_messages(payload, prompt=prompt, planning_revision=planning_revision)
    identifier = call_id or f"{ROUND_CALL_PREFIX}{view.chapter_id}"
    raw = invoke_client(client, messages, model=model, call_id=identifier)
    parsed = parse_arrangement_response(raw)
    arrangement = validate_arrangement(parsed, view, planning_revision=planning_revision)
    arrangement["call"] = {
        "call_id": identifier,
        "model": model,
        "raw_response_dir": str(raw_response_dir) if raw_response_dir else "",
        "finish_reason": raw.get("finish_reason") if isinstance(raw, Mapping) else None,
        "complete": raw.get("complete") if isinstance(raw, Mapping) else None,
        "usage": raw.get("usage") if isinstance(raw, Mapping) else None,
    }
    return arrangement


__all__ = [
    "B_FIELDS",
    "ChapterArrangementError",
    "ChapterView",
    "IdMap",
    "OUTPUT_SCHEMA",
    "ParagraphBrief",
    "PROMPT_PATH",
    "ROUND_CALL_PREFIX",
    "RoundBudgetExceeded",
    "RoundCappedLedger",
    "SCHEMA_VERSION",
    "SourceMaterial",
    "UnitView",
    "VIEW_SCHEMA",
    "arrangement_messages",
    "build_chapter_view",
    "chapter_identity",
    "chapter_tool_materials_from_packet",
    "compact_chapter_tool_material",
    "compact_chapter_tool_materials",
    "distinct_tool_material_sources",
    "estimate_arrangement_cost",
    "load_editor_prompt",
    "load_writer_packet",
    "merge_doi_duplicates",
    "merge_tool_supplement_entry",
    "normalize_doi",
    "normalize_title",
    "parse_arrangement_response",
    "render_arrangement_markdown",
    "resolve_tool_materials",
    "run_arrangement",
    "source_usage_summary",
    "tool_supplement_entry",
    "validate_arrangement",
    "write_view",
]
