"""Unit writing entry: hand one arranged unit and its real material to a writer.

Work order 03 of ``outputs/work_orders/20260927_arrangement_finish_unit_writer``.

Scope: one unit, not a chapter merge, not the whole review, no review loop and no
new reading.  The program assembles the unit's tasks plus the *real* material of
every source those tasks use (A summary, full B planning material, any already
paid deep-read material, and the local locator read out from disk), sends it to a
writing model, and stores the model's Markdown with the citations it actually
used.  Nothing here invents a citation or fills a table cell the material does
not support.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .portable_paths import portable_component

SCHEMA_VERSION = "optomind.review_unit_writer.v1"
INPUT_SCHEMA = "optomind.review_unit_writer.input.v1"
RESULT_SCHEMA = "optomind.review_unit_writer.result.v1"
PROMPT_PATH = Path("prompts/review_unit_writer.md")
PLANNING_REVISION_INSTRUCTIONS = (
    "\n\n【章节论证模式】写作者以更新后的 paragraph_tasks/table_tasks 与 chapter_frame 为主，"
    "材料明确支持的局部准确修正可以直接落实；若问题会改变章节核心任务，完成现有材料支持的正文部分，并在响应"
    "封装的 issues 中记录章节/单元、依据来源和建议动作。不要把编排当作新的科学论证入口，不要把每个单元"
    "改写成性能比较；按任务实际要求解释概念、方法前提、机制、发展、背景、例证或边界。只有任务和材料确有"
    "可比依据时才比较，不补造差异原因。相互独立的维度可以并存；除非材料明确支持，不把它们写成互斥且穷尽的二分路径。"
    "\n\n【原任务消费】paragraph_task 的 `source_brief_details` 是章节负责人已写好的各条原任务，"
    "每条都是独立主张，各自带 point、development 和 source_handles：逐条讲清各自的研究对象、设置、"
    "结果与来源关系，再按编排意图综合。顶层 point 只是第一条主张，顶层 development 只是多条的兼容拼接，"
    "source_uses 是编排用途的并集——三者都不能替代逐条原任务。`portion` 说明本段只展开原任务的哪一部分，"
    "按它取舍；同一原任务为多个段落提供上下文时，各段讲各自的部分，不重复叙述整体。`owner_unit_context` "
    "是负责人单元级的条件、综合与衔接，与段落任务共同生效。"
    "\n\n【写作保真】保留决定判断含义的研究对象与设置（温度/压力/时间/循环等工况）、指标名称与其基准"
    "（含百分比的分母）、比较关系和限制条件，让它们随所依附的判断出现；不同研究或实验先分开描述再比较，"
    "不把不同实验的数字拼成同一结果，不把上限或最佳值说成典型值，不悄悄替换比例的基准。不是逐句填条件表，"
    "也不要求所有数值都进入正文。材料之间冲突且当前无法消解时，分别描述或省略该无法确认的精确数字并继续"
    "完成论述，不整段拒写。原材料明确支持时可以修正负责人文本的错误；允许依据 A/B/精读做有依据的综合。"
    "单元内 `sources` 每篇只提供一次，供各段共用，不要因多段使用而重复罗列。"
    '返回 JSON 对象 {"body_markdown": "单元正文 Markdown", "issues": []}。正常写作时 issues 为空；'
    "具体问题写入 issues，包含 unit_id、problem、source_handles 和 action（local_backfill、directed_read、supplement、chapter_owner 或 omit）。"
)

DEFAULT_MODEL = "qwen3.7-flash"
DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE = 20000
# Above this the unit should be split rather than silently trimmed.  Nothing is
# dropped when the warning fires: the caller decides.
LARGE_INPUT_TOKENS = 65536

# Bookkeeping keys that travel with a paid deep read but say nothing a writer can
# use.  The source paper's own bibliography (``references``) is also dropped by
# default: our writer cites handles, and the list costs ~28k chars per paper.
DEEP_READ_KEEP = ("question_material", "content", "open_questions", "plain_text", "paper_title")
DEEP_READ_DROP_ALWAYS = ("schema_version", "task_id", "review_id", "workflow", "status",
                         "material_ready", "paper_id", "reused_from")
DEEP_READ_DROP_BULKY = ("references",)

CITATION_RE = re.compile(r"\[([^\[\]]{1,120})\]")
# Handles may sit directly next to CJK text (for example ``[参考P0602]``),
# while still refusing identifier substrings such as ``AP0602`` or
# ``P0602suffix``.
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])P\d{3,}(?![A-Za-z0-9_])")
CITATION_PREFIX_RE = re.compile(
    r"^(?:(?:参考)|(?:ref(?:erence)?|citation|source))\s*[:：]?\s*",
    re.IGNORECASE,
)
CITATION_SEQUENCE_RE = re.compile(
    r"(?:P\d{3,}|\d+)(?:[ ,;\-–]*(?:P\d{3,}|\d+))*"
)

A_FIELDS = (
    "work_summary",
    "approach",
    "key_findings",
    "contribution_and_limits",
    "problem_or_question",
    "research_scope",
    "paper_kind",
)
B_FIELDS = (
    "planning_summary",
    "facet_contributions",
    "broader_review_uses",
    "scope_interpretation_cautions",
    "topic_handles",
)

# A reading card is a durable artifact with both scientific material and run
# bookkeeping.  The former can fill a hole in the arranged A/B material; the
# latter must never be sent to the writer.  In particular, the card's
# ``general_understanding`` and ``review_planning`` are normally exact copies
# of the catalogue's A/B fields.
CARD_MATERIAL_MANAGEMENT_KEYS = frozenset({
    "declared_content_depth", "identity_status", "snapshot_id",
    "snapshot_sha256", "usage_limits", "observation_count",
})


class UnitWritingError(Exception):
    """Any refusal or unusable input in the unit writing entry point."""


# --------------------------------------------------------------------------
# reading the arranged chapter
# --------------------------------------------------------------------------


def _load_json(path: str | Path, *, what: str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise UnitWritingError(f"{what}_missing:{target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise UnitWritingError(f"{what}_unreadable:{target}") from exc
    if not isinstance(payload, dict):
        raise UnitWritingError(f"{what}_not_object:{target}")
    return payload


def load_arrangement(path: str | Path) -> dict[str, Any]:
    arrangement = _load_json(path, what="arrangement")
    if not arrangement.get("units"):
        raise UnitWritingError("arrangement_has_no_units:" + str(path))
    # Honor the producer's verdict at the common file-consumption boundary,
    # before material reads, client construction or replacement output. Match
    # the feedback gate: genuinely old exports without a verdict still work,
    # but a supplied verdict must affirm success without contrary signals.
    if "validation" in arrangement:
        validation = arrangement["validation"]
        status = str(validation.get("status") or "").strip().casefold() \
            if isinstance(validation, Mapping) else ""
        if not isinstance(validation, Mapping) or (
            validation.get("ok") is False
            or validation.get("contract_ok") is False
            or validation.get("needs_arrangement") is True
            or bool(validation.get("errors"))
            or bool(validation.get("sources_never_mentioned") or validation.get("missing_sources"))
            or (bool(status) and status != "arranged")
            or not (validation.get("ok") is True or status == "arranged")
        ):
            raise UnitWritingError("arrangement_not_ready:" + (status or "partial") + ":" + str(path))
    return arrangement


def select_unit(arrangement: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    matches = [unit for unit in arrangement.get("units") or ()
               if isinstance(unit, Mapping) and str(unit.get("unit_id") or "") == unit_id]
    if len(matches) > 1:
        raise UnitWritingError(f"unit_id_duplicated:{unit_id}")
    if matches:
        return dict(matches[0])
    known = ", ".join(str(unit.get("unit_id")) for unit in arrangement.get("units") or ())
    raise UnitWritingError(f"unit_not_in_arrangement:{unit_id}|known:{known}")


def _checked_unit_tasks(unit: Mapping[str, Any], chapter_view: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve explicit brief links against the saved input, never task position.

    Persisted/reexported arrangements may predate current validation. Refuse
    ambiguous identities at this final read boundary as well; rebuild complete
    brief details from the authoritative input when references are present.
    """
    unit = deepcopy(dict(unit))
    unit_id = str(unit.get("unit_id") or "")
    all_briefs: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    for owner in chapter_view.get("units") or ():
        if not isinstance(owner, Mapping):
            continue
        for brief in owner.get("paragraph_briefs") or ():
            if not isinstance(brief, Mapping):
                continue
            brief_id = str(brief.get("paragraph_id") or "")
            if brief_id in all_briefs:
                raise UnitWritingError(f"paragraph_id_duplicated:{brief_id}")
            if brief_id:
                all_briefs[brief_id] = dict(brief)
                owners[brief_id] = str(owner.get("unit_id") or "")
    seen: set[str] = set()
    for task in unit.get("paragraph_tasks") or ():
        paragraph_id = str(task.get("paragraph_id") or "")
        if paragraph_id and paragraph_id in seen:
            raise UnitWritingError(f"paragraph_id_duplicated:{paragraph_id}")
        seen.add(paragraph_id)
        if paragraph_id in owners and owners[paragraph_id] != unit_id:
            raise UnitWritingError(f"paragraph_id_unit_mismatch:{paragraph_id}")
        refs = task.get("source_briefs")
        if paragraph_id in owners and task.get("portion"):
            raise UnitWritingError(f"paragraph_id_brief_conflict:{paragraph_id}")
        if refs is None:
            continue
        if not isinstance(refs, list):
            raise UnitWritingError(f"brief_reference_not_list:{paragraph_id}")
        if paragraph_id in owners and refs and (
            set(str(ref) for ref in refs) != {paragraph_id} or task.get("portion")
        ):
            raise UnitWritingError(f"paragraph_id_brief_conflict:{paragraph_id}")
        if not refs:
            continue
        bad = [str(ref) for ref in refs if str(ref) not in all_briefs or owners[str(ref)] != unit_id]
        if bad:
            raise UnitWritingError(f"brief_reference_unknown:{unit_id}:{','.join(bad)}")
        task["source_brief_details"] = [deepcopy(all_briefs[str(ref)]) for ref in refs]
        uses = task.setdefault("source_uses", [])
        handles = {str(use.get("source_handle") or "") for use in uses}
        for brief in task["source_brief_details"]:
            for handle in brief.get("source_handles") or ():
                if handle not in handles:
                    uses.append({"source_handle": handle, "role": "负责人指定", "use": ""})
                    handles.add(handle)
    return unit


def unit_handles(unit: Mapping[str, Any]) -> list[str]:
    """Every handle this unit's tasks actually use, in first-seen order."""

    handles: list[str] = []
    for task in unit.get("paragraph_tasks") or ():
        for use in task.get("source_uses") or ():
            handle = str(use.get("source_handle") or "").strip()
            if handle and handle not in handles:
                handles.append(handle)
    for table in unit.get("table_tasks") or ():
        for row in table.get("row_tasks") or ():
            for use in row.get("source_uses") or ():
                handle = str(use.get("source_handle") or "").strip()
                if handle and handle not in handles:
                    handles.append(handle)
    return handles


# --------------------------------------------------------------------------
# material assembly
# --------------------------------------------------------------------------


@dataclass
class UnitWritingView:
    chapter_id: str
    unit_id: str
    focus: str
    unit_index: int
    sibling_units: list[dict[str, str]]
    unit_count: int
    chapter_frame: dict[str, Any]
    other_chapters: list[dict[str, str]]
    paragraph_tasks: list[dict[str, Any]]
    table_tasks: list[dict[str, Any]]
    materials: list[dict[str, Any]]
    material_notes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    arrangement_path: str = ""
    view_path: str = ""
    unit_notes: str = ""
    # Multi-source or unattributed chapter tool returns relevant to this unit.
    # They keep their whole source set; they are never folded into one paper.
    chapter_tool_materials: list[dict[str, Any]] = field(default_factory=list)
    # The chapter owner's unit-level conditions/synthesis/transition, carried
    # through the arrangement so paragraph tasks are not their only carrier.
    owner_unit_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INPUT_SCHEMA,
            "chapter_id": self.chapter_id,
            "unit_id": self.unit_id,
            "focus": self.focus,
            "unit_index": self.unit_index,
            "unit_count": self.unit_count or self.unit_index + len(self.sibling_units),
            "sibling_units": self.sibling_units,
            "chapter_frame": self.chapter_frame,
            "other_chapters": self.other_chapters,
            "paragraph_tasks": self.paragraph_tasks,
            "table_tasks": self.table_tasks,
            "materials": self.materials,
            "material_notes": self.material_notes,
            "warnings": self.warnings,
            "source_count": len(self.materials),
            "arrangement_path": self.arrangement_path,
            "view_path": self.view_path,
            "unit_notes": self.unit_notes,
            "chapter_tool_materials": self.chapter_tool_materials,
        }

    def material_summary(self) -> dict[str, Any]:
        return {
            "sources": len(self.materials),
            "with_study_summary_A": sum(1 for item in self.materials if item.get("study_summary_A")),
            "with_review_planning_B": sum(1 for item in self.materials if item.get("review_planning_B")),
            "with_deep_read_material": sorted(
                item["source_handle"] for item in self.materials if item.get("deep_read_material")),
            "with_supplement_material": sum(1 for item in self.materials if item.get("supplement_material")),
            "with_supplement_materials": sorted(
                item["source_handle"] for item in self.materials if item.get("supplement_materials")),
            "with_local_passages": sorted(
                item["source_handle"] for item in self.materials if item.get("local_passages")),
            "with_tool_supplement_materials": sorted(
                item["source_handle"] for item in self.materials if item.get("tool_supplement_materials")),
            "chapter_tool_materials": len(self.chapter_tool_materials),
            "read_from_disk": sorted(
                item["source_handle"] for item in self.materials if item.get("material_read_from_disk")),
            "truncated": sorted(
                item["source_handle"] for item in self.materials if item.get("material_truncated")),
        }


def _readable_locator_file(path: str | Path) -> str:
    target = Path(path)
    if not target.is_file():
        raise UnitWritingError("locator_file_missing:" + str(target))
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, Mapping):
        for key in ("text", "content", "body", "markdown", "abstract", "fulltext"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _material_value(value: Any) -> bool:
    """Whether a value is useful material rather than an empty placeholder."""

    return value not in (None, "", [], {})


def _merge_missing_material(target: dict[str, Any], incoming: Mapping[str, Any]) -> list[str]:
    """Fill only absent A/B fields while retaining any distinct card facts.

    Cards are often generated from the same A/B records.  A shallow update
    would copy those records a second time, while replacing a non-empty value
    could discard an arranged value.  Mapping children are merged recursively;
    list children receive only items whose canonical JSON is not already
    present.  The returned paths are kept as audit evidence in the material
    note.
    """

    added: list[str] = []
    for key, value in incoming.items():
        if not _material_value(value):
            continue
        if key not in target or not _material_value(target.get(key)):
            target[key] = deepcopy(value)
            added.append(str(key))
            continue
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            for child in _merge_missing_material(current, value):
                added.append(f"{key}.{child}")
        elif isinstance(current, list) and isinstance(value, list):
            existing = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in current
            }
            for item in value:
                marker = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if marker not in existing:
                    current.append(deepcopy(item))
                    existing.add(marker)
                    added.append(f"{key}[{len(current) - 1}]")
    return added


def _read_card_material(
    path: str | Path,
    entry: dict[str, Any],
) -> tuple[Any | None, dict[str, Any]]:
    """Read a paper card, merging only missing science and keeping true extras.

    The returned value is deliberately small.  Full card metadata and copies
    of A/B are omitted; useful fields absent from the catalogue are merged into
    those existing sections.  Non-JSON locator files keep their text for
    backwards compatibility with older writer packets.
    """

    target = Path(path)
    if not target.is_file():
        raise UnitWritingError("locator_file_missing:" + str(target))
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        return text, {"format": "text", "extra_chars": len(text)}
    if not isinstance(payload, Mapping):
        return text, {"format": "text", "extra_chars": len(text)}

    # Locators are not identity authority. Reject a demonstrably foreign card
    # before filling any missing science; metadata-free legacy cards still work.
    from .chapter_arrangement import normalize_doi

    identities = [payload]
    for container in (payload, payload.get("planning_view"),
                      payload.get("general_understanding"), payload.get("review_planning")):
        if isinstance(container, Mapping):
            for key in ("paper_identity", "record_identity", "identity"):
                if isinstance(container.get(key), Mapping):
                    identities.append(container[key])
    for identity in identities:
        paper = str(identity.get("canonical_paper_id") or identity.get("paper_id") or "")
        doi = normalize_doi(identity.get("doi"))
        if ((paper and entry.get("paper_id") and paper != str(entry["paper_id"]))
                or (doi and entry.get("doi") and doi != normalize_doi(entry["doi"]))):
            raise UnitWritingError("locator_identity_conflict:" + str(target))

    card_note: dict[str, Any] = {"format": "paper_reading_card", "omitted_keys": [], "added_fields": []}
    general = payload.get("general_understanding")
    if isinstance(general, Mapping):
        card_note["omitted_keys"].append("general_understanding")
        target_a = entry.setdefault("study_summary_A", {})
        if isinstance(target_a, dict):
            card_note["added_fields"].extend(
                f"study_summary_A.{field}" for field in _merge_missing_material(target_a, general)
            )

    planning: dict[str, Any] = {}
    review_planning = payload.get("review_planning")
    if isinstance(review_planning, Mapping):
        planning.update(review_planning)
        card_note["omitted_keys"].append("review_planning")
    planning_view = payload.get("planning_view")
    if isinstance(planning_view, Mapping):
        # ``planning_view`` contains the same B fields plus identity/snapshot
        # details.  B fields from the explicit review section win if both exist.
        for key, value in planning_view.items():
            planning.setdefault(key, value)
        card_note["omitted_keys"].append("planning_view")
    # Only B's scientific planning fields may enter the A/B section.  The
    # planning view also carries snapshot, identity and declared-depth
    # bookkeeping which belongs in provenance, not in a writer prompt.
    planning = {
        str(key): value for key, value in planning.items()
        if str(key) in B_FIELDS and _material_value(value)
    }
    if planning:
        target_b = entry.setdefault("review_planning_B", {})
        if isinstance(target_b, dict):
            card_note["added_fields"].extend(
                f"review_planning_B.{field}" for field in _merge_missing_material(target_b, planning)
            )

    # The material envelope can contain genuine caveats or scope information,
    # but its run-depth, snapshot and usage fields are management metadata.
    extras: dict[str, Any] = {}
    material = payload.get("material")
    if isinstance(material, Mapping):
        for key, value in material.items():
            if key not in CARD_MATERIAL_MANAGEMENT_KEYS and _material_value(value):
                extras[str(key)] = deepcopy(value)
        if extras:
            card_note["extra_fields"] = sorted(extras)
        card_note["omitted_keys"].append("material")

    # Older locator cards may be a simple ``{"text": ...}`` object.  Preserve
    # that actual material, while still ignoring all recognized card metadata.
    for text_key in ("text", "content", "body", "markdown", "abstract", "fulltext"):
        direct_text = payload.get(text_key)
        if isinstance(direct_text, str) and direct_text.strip():
            extras = direct_text
            card_note["extra_fields"] = [text_key]
            break

    card_note["omitted_keys"] = sorted(set(card_note["omitted_keys"]))
    if not card_note.get("added_fields"):
        card_note.pop("added_fields", None)
    if not extras:
        card_note["duplicate_sections_only"] = True
        return None, card_note
    return extras, card_note


def _deep_read_text(value: Any) -> Any:
    return value


def trim_deep_read_material(material: Any, *, keep_references: bool = False) -> tuple[Any, dict[str, Any]]:
    """Keep the writing-relevant part of a paid deep read, and record what went.

    Nothing is dropped silently: the caller stores the dropped key names and
    their size, and the full material stays available through the locator.
    """

    if not isinstance(material, Mapping) or not material:
        return material, {}
    drop = list(DEEP_READ_DROP_ALWAYS) + ([] if keep_references else list(DEEP_READ_DROP_BULKY))
    kept: dict[str, Any] = {}
    dropped: dict[str, int] = {}
    for key, value in material.items():
        if key in DEEP_READ_KEEP:
            kept[key] = value
        elif key in drop:
            dropped[key] = len(json.dumps(value, ensure_ascii=False))
        else:
            kept[key] = value
    # ``content`` often repeats the top-level question material verbatim.
    content = kept.get("content")
    question = kept.get("question_material")
    if isinstance(content, Mapping) and set(content.keys()) <= {"question_material"} and question is not None:
        if json.dumps(content.get("question_material"), ensure_ascii=False, sort_keys=True) == \
                json.dumps(question, ensure_ascii=False, sort_keys=True):
            dropped["content(duplicate_of_question_material)"] = len(
                json.dumps(content, ensure_ascii=False))
            kept.pop("content", None)
    note = {}
    if dropped:
        note = {
            "dropped_keys": dropped,
            "dropped_chars": sum(dropped.values()),
            "note": "深读材料里的记账字段与论文自带的参考文献表已省略；科学内容（问句材料/发现/条件）保留。"
                    "需要时可用 locator 读原文。",
        }
    return kept, note


def _material_entry(
    handle: str,
    catalog: Mapping[str, Any],
    *,
    packet_materials: Mapping[str, Any] | None,
    max_chars_per_source: int,
    keep_deep_read_references: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One source, sent once: identity + A + full B + paid deep-read material.

    The locator card is parsed before any size ceiling is applied.  Its copies
    of A/B are omitted, missing science fills the existing A/B sections, and
    only true extra material is retained.  If a source is longer than the
    per-source ceiling, the cut is recorded in ``material_truncated`` instead
    of happening invisibly.
    """

    catalog_entry = catalog.get(handle)
    note: dict[str, Any] = {"source_handle": handle, "issues": []}
    if not isinstance(catalog_entry, Mapping):
        note["issues"].append("handle_not_in_source_catalog")
        return {"source_handle": handle, "missing_material": True}, note

    entry: dict[str, Any] = {
        "source_handle": handle,
        "title": str(catalog_entry.get("title") or ""),
        "year": str(catalog_entry.get("year") or ""),
        "doi": str(catalog_entry.get("doi") or ""),
        "paper_id": str(catalog_entry.get("paper_id") or ""),
        "material_depth": str(catalog_entry.get("material_depth") or ""),
        "material_status": str(catalog_entry.get("material_status") or ""),
        "aliases": list(catalog_entry.get("aliases") or []),
    }
    a_material = catalog_entry.get("study_summary_A") or {}
    if a_material:
        entry["study_summary_A"] = {
            key: deepcopy(a_material[key]) for key in A_FIELDS if a_material.get(key)
        }
        for key, value in a_material.items():
            if key not in entry["study_summary_A"] and value not in (None, "", [], {}):
                entry["study_summary_A"][key] = deepcopy(value)
    b_material = catalog_entry.get("review_planning_B") or {}
    if b_material:
        entry["review_planning_B"] = {
            key: deepcopy(b_material[key]) for key in B_FIELDS if b_material.get(key)
        }
        for key, value in b_material.items():
            if key not in entry["review_planning_B"] and value not in (None, "", [], {}):
                entry["review_planning_B"][key] = deepcopy(value)
    deep_materials: list[dict[str, Any]] = []
    deep_signatures: set[str] = set()
    for raw in [catalog_entry.get("deep_read_material"), *(catalog_entry.get("deep_read_materials") or [])]:
        if not isinstance(raw, Mapping) or not raw:
            continue
        signature = json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, default=str)
        if signature in deep_signatures:
            continue
        deep_signatures.add(signature)
        deep_materials.append(dict(raw))
    if deep_materials:
        trimmed_materials: list[Any] = []
        trim_notes: list[dict[str, Any]] = []
        for material in deep_materials:
            deep, deep_note = trim_deep_read_material(
                material, keep_references=keep_deep_read_references)
            trimmed_materials.append(_deep_read_text(deep))
            if deep_note:
                trim_notes.append(deep_note)
        entry["deep_read_material"] = trimmed_materials[0]
        if len(trimmed_materials) > 1:
            entry["deep_read_materials"] = trimmed_materials[1:]
        note["paid_deep_read_material"] = True
        if trim_notes:
            entry["deep_read_material_trimmed"] = (
                trim_notes[0] if len(trim_notes) == 1 else trim_notes)
            note["deep_read_trimmed"] = (
                trim_notes[0]["dropped_keys"] if len(trim_notes) == 1
                else [item["dropped_keys"] for item in trim_notes])
    if catalog_entry.get("supplement_material"):
        entry["supplement_material"] = catalog_entry["supplement_material"]
    if isinstance(catalog_entry.get("supplement_materials"), list) and catalog_entry["supplement_materials"]:
        entry["supplement_materials"] = [dict(item) for item in catalog_entry["supplement_materials"]
                                         if isinstance(item, Mapping)]
    local_passages = [
        dict(raw) for raw in [catalog_entry.get("local_passages"), *(catalog_entry.get("local_passages_variants") or [])]
        if isinstance(raw, Mapping) and raw
    ]
    if local_passages:
        # Kept under its own reading mode; a local passage is never merged into
        # deep_read_material or presented as a paid reading.
        entry["local_passages"] = local_passages[0]
        if len(local_passages) > 1:
            entry["local_passages_variants"] = local_passages[1:]
    tool_supplements = [dict(item) for item in (catalog_entry.get("tool_supplement_materials") or [])
                        if isinstance(item, Mapping)]
    if tool_supplements:
        # need_id groups rounds of one information need: identical full entries
        # fold, while every non-identical complementary result reaches the
        # writing model.
        from .chapter_arrangement import merge_tool_supplement_entry

        folded: list[dict[str, Any]] = []
        for item in tool_supplements:
            folded = merge_tool_supplement_entry(folded, item)
        entry["tool_supplement_materials"] = folded

    locator = catalog_entry.get("locator") or {}
    card_path = str(locator.get("card_path") or "")
    entry["locator"] = {
        "writer_packet": str(locator.get("writer_packet") or ""),
        "source_handle": str(locator.get("source_handle") or handle),
        "card_path": card_path,
    }
    if card_path:
        try:
            card_material, card_note = _read_card_material(card_path, entry)
        except UnitWritingError as exc:
            note["issues"].append(str(exc))
            if str(exc).startswith("locator_identity_conflict:"):
                entry["material_identity_conflict"] = "foreign_locator_card_ignored"
        else:
            if card_material is not None:
                entry["card_material"] = card_material
            note["card_dedup"] = card_note
            entry["material_read_from_disk"] = card_path
            note["read_from_disk"] = card_path
    elif packet_materials is not None:
        # The catalogue was completed from the chapter's writer packet; that is
        # the local material behind it, so it is read out rather than pointed at.
        packet_row = packet_materials.get(handle)
        if isinstance(packet_row, Mapping):
            entry["material_read_from_disk"] = str(
                (catalog_entry.get("locator") or {}).get("writer_packet") or "")
            note["read_from_packet"] = entry["material_read_from_disk"]
        else:
            note["issues"].append("no_local_material_for_handle")

    if max_chars_per_source and max_chars_per_source > 0:
        blob = json.dumps(entry, ensure_ascii=False)
        if len(blob) > max_chars_per_source:
            entry["material_truncated"] = {
                "original_chars": len(blob),
                "kept_chars": max_chars_per_source,
                "note": "单篇材料超过上限，已截断；截断发生在单篇内部，不丢后续来源",
            }
            note["issues"].append("material_truncated")
            entry = _truncate_entry(entry, max_chars_per_source)
    return entry, note


def _clip_long_strings(value: Any, keep_chars: int) -> Any:
    """Clip long free text inside nested material structures, keeping shape."""

    if isinstance(value, str):
        return value if len(value) <= keep_chars else value[:keep_chars] + "…（本地截断）"
    if isinstance(value, list):
        return [_clip_long_strings(item, keep_chars) for item in value]
    if isinstance(value, dict):
        return {key: _clip_long_strings(item, keep_chars) for key, item in value.items()}
    return value


def _truncate_entry(entry: dict[str, Any], limit: int) -> dict[str, Any]:
    """Shrink the long free-text fields once each, keeping facts and flags intact.

    Structured A/B fields are never dropped: if they alone exceed the ceiling the
    entry is reported as still over the limit instead of being silently cut.
    """

    for key in ("card_material", "supplement_material", "deep_read_material"):
        if len(json.dumps(entry, ensure_ascii=False)) <= limit:
            break
        value = entry.get(key)
        if isinstance(value, str) and value:
            over = len(json.dumps(entry, ensure_ascii=False)) - limit
            keep = len(value) - over - 60
            entry[key] = (value[:keep] + "\n…（本地截断）") if keep > 0 else "（本地截断；原文见定位信息）"
    # Supplement lists and local passages carry their long text inside nested
    # structures; they are clipped in place with the recorded truncation note.
    for key in (
        "supplement_materials", "tool_supplement_materials", "local_passages",
        "local_passages_variants", "deep_read_materials",
    ):
        if len(json.dumps(entry, ensure_ascii=False)) <= limit:
            break
        if entry.get(key):
            entry[key] = _clip_long_strings(entry[key], 400)
    if len(json.dumps(entry, ensure_ascii=False)) > limit:
        entry.setdefault("material_truncated", {})["still_over_limit"] = True
        entry["material_truncated"]["note"] = (
            "结构性 A/B 材料超过单篇上限但没有丢：只截断了定位/补充材料文本，"
            "结构化字段完整保留，便于写作者核对")
    return entry


def build_unit_view(
    arrangement_path: str | Path,
    unit_id: str,
    *,
    view_path: str | Path | None = None,
    packet_root: str | Path | None = None,
    max_material_chars_per_source: int = DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE,
    keep_deep_read_references: bool = False,
) -> UnitWritingView:
    """Assemble one unit's tasks and the real material of the sources they use."""

    arrangement_file = Path(arrangement_path)
    arrangement = load_arrangement(arrangement_file)
    chapter_id = str(arrangement.get("chapter_id") or "")
    if not chapter_id:
        raise UnitWritingError("arrangement_without_chapter_id:" + str(arrangement_file))
    source_catalog = arrangement.get("source_catalog") or {}
    if not source_catalog:
        raise UnitWritingError("arrangement_without_source_catalog:" + str(arrangement_file))

    resolved_view_path = Path(view_path) if view_path else arrangement_file.parent / "ARRANGEMENT_INPUT.json"
    chapter_view: dict[str, Any] = {}
    if resolved_view_path.is_file():
        chapter_view = _load_json(resolved_view_path, what="arrangement_input")
    elif packet_root:
        chapter_view = _view_from_packet(packet_root, chapter_id)
    else:
        raise UnitWritingError("arrangement_input_missing:" + str(resolved_view_path))

    unit = _checked_unit_tasks(select_unit(arrangement, unit_id), chapter_view)
    units = list(arrangement.get("units") or ())
    index = [str(item.get("unit_id")) for item in units].index(unit_id)
    siblings = [
        {"unit_id": str(item.get("unit_id") or ""), "point": str(item.get("focus") or "")}
        for position, item in enumerate(units) if position != index
    ]

    packet_materials: dict[str, Any] | None = None
    locator_packet = ""
    for handle in unit_handles(unit)[:1]:
        locator_packet = str(((source_catalog.get(handle) or {}).get("locator") or {}).get("writer_packet") or "")
    packet_path = Path(locator_packet) if locator_packet else None
    if packet_path and packet_path.is_file():
        packet = _load_json(packet_path, what="writer_packet")
        packet_materials = {
            str(row.get("source_handle")): row for row in packet.get("source_materials") or ()
        }
        from .chapter_arrangement import resolve_tool_materials

        for tool in resolve_tool_materials(packet):
            if not str(tool.get("usable_content") or "").strip():
                continue
            for source in tool.get("sources") or ():
                handle = str(source.get("source_handle") or "")
                if handle:
                    packet_materials.setdefault(handle, source)

    handles = unit_handles(unit)
    relevant_tools = _unit_relevant_chapter_tool_materials(
        arrangement.get("chapter_tool_materials") or (), unit_id)
    for tool in relevant_tools:
        if not str(tool.get("usable_content") or "").strip():
            continue
        for source in tool.get("sources") or ():
            handle = str(source.get("source_handle") or "") if isinstance(source, Mapping) else ""
            if handle in source_catalog and handle not in handles:
                handles.append(handle)
    if not handles:
        raise UnitWritingError(f"unit_uses_no_sources:{unit_id}")
    materials: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for handle in handles:
        entry, note = _material_entry(
            handle, source_catalog, packet_materials=packet_materials,
            max_chars_per_source=max_material_chars_per_source,
            keep_deep_read_references=keep_deep_read_references)
        materials.append(entry)
        notes.append(note)

    # The final arrangement owns this chapter's argument.  Keep the original
    # review question and chapter responsibility fields from ARRANGEMENT_INPUT;
    # only the chapter-specific thesis/argument is superseded when present.
    final_chapter_argument = str(arrangement.get("chapter_argument") or "").strip()
    chapter_thesis = final_chapter_argument or str(chapter_view.get("thesis") or "")
    review_argument = str(chapter_view.get("review_argument") or "")
    view = UnitWritingView(
        chapter_id=chapter_id,
        unit_id=unit_id,
        focus=str(unit.get("focus") or ""),
        unit_index=index + 1,
        unit_count=len(units),
        sibling_units=siblings,
        chapter_frame={
            "chapter_title": str(chapter_view.get("title") or ""),
            "chapter_purpose": str(chapter_view.get("purpose") or ""),
            "chapter_scope": str(chapter_view.get("scope") or ""),
            "chapter_thesis": chapter_thesis,
            "chapter_argument": final_chapter_argument or chapter_thesis,
            "reader_objective": str(chapter_view.get("reader_objective") or ""),
            "research_question": str(chapter_view.get("research_question") or ""),
            "review_argument": review_argument,
            "shared_scope": dict(chapter_view.get("shared_scope") or {}),
            "review_argument_status": chapter_view.get("review_argument_status") or (
                "provided_unspecified" if review_argument else "missing"),
            "review_argument_source": chapter_view.get("review_argument_source") or (
                "provided_unspecified" if review_argument else "missing"),
        },
        other_chapters=[
            {"chapter_id": str(row.get("chapter_id") or ""), "purpose": str(row.get("purpose") or "")}
            for row in (chapter_view.get("other_chapters") or ())
        ],
        paragraph_tasks=[dict(task) for task in unit.get("paragraph_tasks") or ()],
        table_tasks=[dict(task) for task in unit.get("table_tasks") or ()],
        materials=materials,
        material_notes=notes,
        arrangement_path=str(arrangement_file),
        view_path=str(resolved_view_path),
        unit_notes=str(unit.get("unit_notes") or ""),
        chapter_tool_materials=relevant_tools,
        owner_unit_context=(
            dict(unit["owner_unit_context"]) if isinstance(unit.get("owner_unit_context"), Mapping)
            else {}),
    )
    view.sources = {handle: source_catalog.get(handle) for handle in handles}
    missing = [item["source_handle"] for item in materials if item.get("missing_material")]
    if missing:
        view.warnings.append({"code": "sources_missing_from_catalog", "handles": missing})
    no_material = [
        item["source_handle"] for item in materials
        if not (item.get("study_summary_A") or item.get("review_planning_B")
                or item.get("deep_read_material") or item.get("card_material"))
    ]
    if no_material:
        view.warnings.append({"code": "sources_without_any_material", "handles": no_material})
    return view


def _view_from_packet(packet_root: str | Path, chapter_id: str) -> dict[str, Any]:
    from .chapter_arrangement import build_chapter_view

    root = Path(packet_root)
    plan_path = root / "DETAILED_REVIEW_PLAN.json"
    plan = _load_json(plan_path, what="plan") if plan_path.is_file() else {}
    view = build_chapter_view(
        root / "writer_packets" / (chapter_id + ".json"),
        shared_outline=plan.get("shared_outline") or [],
        review_argument=str(plan.get("review_argument") or ""),
        shared_scope=plan.get("shared_scope"),
        review_argument_status=str(plan.get("review_argument_status") or ""),
        review_argument_source=str(plan.get("review_argument_source") or ""),
    )
    return view.to_dict(include_material=False)


# --------------------------------------------------------------------------
# prompt and messages
# --------------------------------------------------------------------------


def load_writer_prompt(path: str | Path | None = None, *, planning_revision: bool = False) -> str:
    target = Path(path) if path else Path(__file__).resolve().parents[3] / PROMPT_PATH
    if not target.is_file():
        raise UnitWritingError("writer_prompt_missing:" + str(target))
    prompt = target.read_text(encoding="utf-8")
    if not planning_revision:
        return prompt
    # The opt-in prompt must resolve the old blanket comparison wording before
    # the local role reminder is added.
    prompt = prompt.replace(
        "本章最终论证（`chapter_argument`，优先于旧的 `chapter_thesis`）",
        "本章最终论证（`chapter_argument` 是章节负责人更新后的任务；结合所给材料作局部准确修正，不把编排字段当作新的科学定论）",
    ).replace(
        "再解释来源之间的一致、差异或互补，最后回到本段论点。",
        "再解释来源之间有材料依据的一致、差异或互补；若本段不是比较任务，按其概念、方法、机制、发展或边界功能完成论证，最后回到本段论点。",
    ).replace(
        "多篇同段时据此解释它们为何一致、相反、条件不同或互补，",
        "多篇同段时仅在材料支持的情况下解释其互补、条件不同或其他关系，",
    ).replace(
        "以材料支持的比较、机制或概念解释收束，",
        "以材料支持的比较、机制、概念解释、发展关系或边界说明收束，",
    )
    return _with_revision_output(prompt)


def _with_revision_output(prompt: str) -> str:
    prompt = prompt.replace(
        "- **只返回单元正文 Markdown**（可包含标题、段落与 Markdown 表格）。",
        "- 正文放在 body_markdown，可包含标题、段落与 Markdown 表格。",
    ).replace(
        "- 不要返回 JSON、不要复述任务、不要输出元数据或来源清单。",
        "- 不复述任务或来源清单；按下面指定的 JSON 封装返回正文和问题。",
    )
    if PLANNING_REVISION_INSTRUCTIONS not in prompt:
        prompt += PLANNING_REVISION_INSTRUCTIONS
    return prompt


def _unit_relevant_chapter_tool_materials(
    items: Sequence[Mapping[str, Any]],
    unit_id: str,
) -> list[dict[str, Any]]:
    """Chapter-level tool returns this unit should see.

    An item that names a target unit only reaches that unit; an item without a
    unit target serves the whole chapter and reaches every unit once.  Source
    sets are preserved verbatim.
    """

    relevant: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        unit_key = str(raw.get("unit_key") or "").strip()
        if unit_key:
            target = unit_key.split(":", 1)[-1].strip()
            if target and target != unit_id:
                continue
        relevant.append(dict(raw))
    return relevant


def unit_payload(
    view: UnitWritingView, *, language: str = "zh",
    citation_number_map: Mapping[Any, Any] | None = None,
    planning_revision: bool = False,
) -> dict[str, Any]:
    """The compact payload the writing model receives (each source only once)."""

    payload = {
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "language": language,
        "chapter_frame": view.chapter_frame,
        "other_chapters": view.other_chapters,
        "unit_position": {"index": view.unit_index, "of": view.unit_count or view.unit_index + len(view.sibling_units)},
        "unit_focus": view.focus,
        "unit_notes": view.unit_notes,
        "sibling_units": view.sibling_units,
        "chapter_tool_materials": view.chapter_tool_materials,
        **({"owner_unit_context": view.owner_unit_context} if view.owner_unit_context else {}),
        "paragraph_tasks": [
            {
                "paragraph_id": task.get("paragraph_id"),
                **({"source_briefs": list(task["source_briefs"])}
                   if task.get("source_briefs") else {}),
                **({"source_brief_details": [dict(item) for item in task["source_brief_details"]]}
                   if task.get("source_brief_details") else {}),
                "point": task.get("point"),
                "development": task.get("development"),
                **({"portion": task["portion"]} if task.get("portion") else {}),
                "source_uses": [
                    {"source_handle": use.get("source_handle"), "role": use.get("role"),
                     "use": use.get("use")}
                    for use in task.get("source_uses") or ()
                ],
            }
            for task in view.paragraph_tasks
        ],
        "table_tasks": [
            {
                "table_id": table.get("table_id"),
                "purpose": table.get("purpose"),
                "columns": list(table.get("columns") or []),
                "row_tasks": [
                    {
                        "content": row.get("content"),
                        "source_uses": [
                            {"source_handle": use.get("source_handle"), "role": use.get("role"),
                             "use": use.get("use")}
                            for use in row.get("source_uses") or ()
                        ],
                    }
                    for row in table.get("row_tasks") or ()
                ],
            }
            for table in view.table_tasks
        ],
        "sources": view.materials,
    }
    if planning_revision:
        payload["planning_revision_mode"] = True
        if citation_number_map is None:
            citation_number_map = _local_numeric_citation_map(_known_unit_handles(view))
            payload["citation_number_map_origin"] = "generated_local_aliases"
        else:
            payload["citation_number_map_origin"] = "caller_explicit"
    if citation_number_map is not None:
        payload["citation_number_map"] = dict(citation_number_map)
    return payload



# --------------------------------------------------------------------------
# Explicit omitted-task completion
# --------------------------------------------------------------------------

_COMPLETION_INSTRUCTIONS = """
【定点补写模式】这不是重写单元。只处理 requested_task_ids 指定的任务，把
existing_body_markdown 当作只读上下文，不重复或改写其中已有正文；返回的
body_markdown 只能是需要追加的片段。返回 JSON 对象，且只使用以下字段：
body_markdown、status（appended、already_covered 或 pending）、covered_task_ids
（仅诊断信息）和 issues。covered_task_ids 不能代替对正文的实际检查，也不能单独
证明任务已经完成。若任务已在现有正文中充分完成，返回空 body_markdown 和
already_covered，不要杜撰内容；若材料不足或无法完成，返回 pending 并说明问题。
只使用本次给出的任务和来源材料，不新检索、不调用未给出的资料。

若指定任务是表格任务，必须在 body_markdown 中写出真正的 Markdown 表格：有表头、
分隔行和至少一行数据；不能返回 table_tasks、row_tasks 或任务描述来冒充已完成
表格。表格单元格只写材料支持的内容，材料没有提供的值写“所给材料未提供”。
""".strip()


def _source_handles_from_value(value: Any) -> list[str]:
    """Collect explicit source handles without treating arbitrary text as IDs."""

    found: list[str] = []

    def add(raw: Any) -> None:
        handle = str(raw or "").strip()
        if handle and handle not in found:
            found.append(handle)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if "source_handle" in node:
                add(node.get("source_handle"))
            if "source_handles" in node:
                raw = node.get("source_handles")
                if isinstance(raw, str):
                    add(raw)
                elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                    for item in raw:
                        if isinstance(item, Mapping):
                            add(item.get("source_handle"))
                        else:
                            add(item)
            for key in (
                "source_uses", "source_brief_details", "source_briefs", "row_tasks",
                "sources", "source_materials", "materials", "references",
            ):
                if key in node:
                    walk(node.get(key))
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for item in node:
                walk(item)

    walk(value)
    return found


def _completion_task_parts(
    view: UnitWritingView,
    task_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    requested = [str(item).strip() for item in task_ids if str(item or "").strip()]
    if not requested:
        raise UnitWritingError("completion_task_required")
    if len(set(requested)) != len(requested):
        raise UnitWritingError("completion_task_duplicate")
    paragraphs = {
        str(task.get("paragraph_id") or ""): dict(task)
        for task in view.paragraph_tasks
        if str(task.get("paragraph_id") or "")
    }
    tables = {
        str(task.get("table_id") or ""): dict(task)
        for task in view.table_tasks
        if str(task.get("table_id") or "")
    }
    selected_paragraphs: list[dict[str, Any]] = []
    selected_tables: list[dict[str, Any]] = []
    for task_id in requested:
        if task_id in paragraphs:
            selected_paragraphs.append(deepcopy(paragraphs[task_id]))
        elif task_id in tables:
            selected_tables.append(deepcopy(tables[task_id]))
        else:
            raise UnitWritingError(f"completion_task_not_found:{task_id}")
    handles: list[str] = []
    for task in [*selected_paragraphs, *selected_tables]:
        for handle in _source_handles_from_value(task):
            if handle not in handles:
                handles.append(handle)
    return selected_paragraphs, selected_tables, handles


def _completion_tool_materials(
    items: Sequence[Mapping[str, Any]],
    *,
    unit_id: str,
    handles: Sequence[str],
) -> list[dict[str, Any]]:
    """Keep only unit/source-linked chapter tools for a selected task."""

    handle_set = set(handles)
    selected: list[dict[str, Any]] = []
    for raw in items or ():
        if not isinstance(raw, Mapping):
            continue
        unit_keys = [
            str(raw.get(key) or "").strip()
            for key in ("unit_key", "unit_id")
            if str(raw.get(key) or "").strip()
        ]
        matches_unit = any(
            value == unit_id or value.rsplit(":", 1)[-1] == unit_id
            for value in unit_keys
        )
        matches_source = bool(handle_set.intersection(_source_handles_from_value(raw)))
        if matches_unit or matches_source:
            selected.append(deepcopy(dict(raw)))
    return selected


def build_completion_payload(
    view: UnitWritingView,
    existing_body: str,
    task_ids: Sequence[str],
    *,
    language: str = "zh",
) -> dict[str, Any]:
    """Build a narrow, read-only payload for explicit task completion."""

    paragraph_tasks, table_tasks, handles = _completion_task_parts(view, task_ids)
    by_handle = {
        str(item.get("source_handle") or ""): item
        for item in view.materials
        if isinstance(item, Mapping)
    }
    tools = _completion_tool_materials(
        view.chapter_tool_materials, unit_id=view.unit_id, handles=handles)
    for item in tools:
        if not str(item.get("usable_content") or "").strip():
            continue
        for handle in _source_handles_from_value(item):
            if handle in by_handle and handle not in handles:
                handles.append(handle)
    missing = [
        handle for handle in handles
        if handle not in by_handle or by_handle[handle].get("missing_material")
    ]
    if missing:
        raise UnitWritingError("completion_source_missing:" + ",".join(missing))
    sources = [deepcopy(by_handle[handle]) for handle in handles]
    return {
        "completion_mode": True,
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "language": language,
        "chapter_frame": deepcopy(view.chapter_frame),
        "other_chapters": deepcopy(view.other_chapters),
        "unit_position": {
            "index": view.unit_index,
            "of": view.unit_count or view.unit_index + len(view.sibling_units),
        },
        "unit_focus": view.focus,
        "unit_notes": view.unit_notes,
        "sibling_units": deepcopy(view.sibling_units),
        **({"owner_unit_context": deepcopy(view.owner_unit_context)}
           if view.owner_unit_context else {}),
        "paragraph_tasks": paragraph_tasks,
        "table_tasks": table_tasks,
        "sources": sources,
        "chapter_tool_materials": tools,
        "existing_body_markdown": existing_body,
        "requested_task_ids": [str(item) for item in task_ids],
        "requested_source_handles": handles,
    }


def completion_messages(
    view: UnitWritingView,
    existing_body: str,
    task_ids: Sequence[str],
    *,
    prompt: str | None = None,
    language: str = "zh",
    planning_revision: bool = False,
    citation_number_map: Mapping[Any, Any] | None = None,
) -> list[dict[str, str]]:
    payload = build_completion_payload(view, existing_body, task_ids, language=language)
    if citation_number_map is not None:
        payload["citation_number_map"] = dict(citation_number_map)
        payload["citation_number_map_origin"] = "caller_explicit"
    elif planning_revision:
        payload["citation_number_map"] = _local_numeric_citation_map(
            payload.get("requested_source_handles") or ())
        payload["citation_number_map_origin"] = "generated_local_aliases"
    system = prompt if prompt is not None else load_writer_prompt(planning_revision=planning_revision)
    if planning_revision:
        system = _with_revision_output(system)
    system = system.rstrip() + "\n\n" + _COMPLETION_INSTRUCTIONS
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def _completion_envelope(response: Any) -> Mapping[str, Any]:
    """Extract completion JSON without accepting table metadata as prose."""

    if isinstance(response, Mapping):
        if any(key in response for key in ("body_markdown", "table_markdown", "status", "covered_task_ids", "issues")):
            return response
        content = response.get("content")
        if isinstance(content, str):
            decoded = _decode_json_content(content)
            if decoded is not None:
                return decoded
            if content.strip():
                return {
                    "body_markdown": content,
                    "status": "appended",
                    "issues": [{"code": "plain_text_response"}],
                }
        for key in ("response", "message"):
            nested = response.get(key)
            if isinstance(nested, (Mapping, str)):
                try:
                    return _completion_envelope(nested)
                except UnitWritingError:
                    pass
        choices = response.get("choices")
        if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes, bytearray)) and choices:
            return _completion_envelope(choices[0])
    elif isinstance(response, str):
        decoded = _decode_json_content(response)
        if decoded is not None:
            return decoded
        if response.strip():
            return {
                "body_markdown": response,
                "status": "appended",
                "issues": [{"code": "plain_text_response"}],
            }
    raise UnitWritingError("completion_response_unreadable")


def _fenced_line_indices(text: str) -> set[int]:
    """Identify fenced code (including an unfinished fence), without unwrapping it."""
    hidden: set[int] = set()
    fence = ""
    for index, line in enumerate(text.splitlines()):
        if fence:
            hidden.add(index)
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = ""
        else:
            match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if match:
                fence = match.group(1)
                hidden.add(index)
    return hidden


def _table_cells(line: str) -> list[str]:
    # Escaped pipes are cell text, not additional columns.
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _markdown_tables(text: str) -> list[dict[str, Any]]:
    """Syntactic finished tables only; never a semantic task-coverage judgment."""
    lines = text.splitlines()
    hidden = _fenced_line_indices(text) | {index for index, line in enumerate(lines)
                                         if line.startswith(("    ", "\t"))}
    found: list[dict[str, Any]] = []
    index = 0
    while index + 2 < len(lines):
        if index in hidden or index + 1 in hidden or index + 2 in hidden:
            index += 1
            continue
        header, separator = _table_cells(lines[index]), _table_cells(lines[index + 1])
        if len(header) < 2 or not all(header) or len(separator) != len(header) or not all(
            re.fullmatch(r":?-{3,}:?", cell) for cell in separator
        ):
            index += 1
            continue
        end = index + 2
        data: list[list[str]] = []
        while end < len(lines) and end not in hidden and "|" in lines[end] and lines[end].strip():
            data.append(_table_cells(lines[end]))
            end += 1
        if data and all(len(row) == len(header) and all(row) for row in data):
            found.append({"start": index, "end": end, "header_cells": len(header),
                          "data_cells": len(data[0]), "line": index + 1,
                          "fingerprint": (tuple(header), tuple(tuple(row) for row in data))})
        index = max(index + 1, end)
    return found


def _markdown_table_check(text: str) -> dict[str, Any]:
    tables = _markdown_tables(str(text or ""))
    if tables:
        return {"valid": True, **{key: tables[0][key] for key in ("header_cells", "data_cells", "line")}}
    return {"valid": False, "header_cells": 0, "data_cells": 0, "line": None}


def run_unit_completion(
    view: UnitWritingView,
    *,
    existing_body: str,
    task_ids: Sequence[str],
    client: Any,
    model: str = DEFAULT_MODEL,
    prompt: str | None = None,
    language: str = "zh",
    output_tokens: int = 4000,
    thinking_budget: int = 0,
    raw_response_dir: str | Path | None = None,
    planning_revision: bool = False,
    simulated: bool = False,
    citation_number_map: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Make at most one explicit completion call and preserve a pending result."""

    from .module4.runtime import invoke_client

    messages = completion_messages(
        view, existing_body, task_ids, prompt=prompt, language=language,
        planning_revision=planning_revision, citation_number_map=citation_number_map)
    call_id = f"unit_completion_{view.chapter_id}_{view.unit_id}_{int(time.time())}"
    response: Any = None
    call_error = ""
    try:
        response = invoke_client(
            client, messages, call_id=call_id, model=model,
            max_output_tokens=output_tokens, thinking_budget=thinking_budget)
    except Exception as exc:
        call_error = type(exc).__name__ + ":" + str(exc)
        record = getattr(exc, "record", None)
        response = dict(record) if isinstance(record, Mapping) else {
            "error_type": type(exc).__name__, "error": str(exc), "complete": False,
        }

    payload = json.loads(messages[-1]["content"])
    raw_path = ""
    if raw_response_dir is not None:
        target = Path(raw_response_dir)
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        raw_target = target / (portable_component(f"{call_id}_{stamp}") + ".raw")
        raw_target.write_text(json.dumps(response, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        raw_path = str(raw_target)

    result: dict[str, Any] = {
        "messages": messages,
        "payload": payload,
        "existing_body_markdown": existing_body,
        "task_ids": [str(item) for item in task_ids],
        "source_handles": payload.get("requested_source_handles", []),
        "body_markdown": existing_body,
        "completion_fragment": "",
        "pending": True,
        "model_status": "pending",
        "covered_task_ids": [],
        "content_review_status": "not_reviewed",
        "citation_scope": "completion_fragment",
        "issues": [],
        "raw_response": raw_path,
        "usage": dict(response.get("usage") or {}) if isinstance(response, Mapping) else {},
        "finish_reason": response.get("finish_reason") or "" if isinstance(response, Mapping) else "",
        "complete": (
            bool(response.get("complete")) if response.get("complete") is not None
            else not bool(call_error)
        ) if isinstance(response, Mapping) else False,
        "simulated": bool(simulated),
        "model": model,
        "call_error": call_error,
    }
    try:
        envelope = _completion_envelope(response)
        if any(isinstance(envelope.get(key), str) and envelope[key].strip()
               for key in ("body_markdown", "table_markdown")):
            fragment, normalization = _consume_unit_output(envelope)
        else:
            fragment, normalization = "", []
        explicit_map = citation_number_map
        map_origin = "caller_explicit" if citation_number_map is not None else str(
            payload.get("citation_number_map_origin") or "")
        if explicit_map is None:
            candidate = payload.get("citation_number_map")
            explicit_map = candidate if isinstance(candidate, Mapping) else None
        if explicit_map is not None and not map_origin:
            map_origin = "caller_explicit"
        mapping_diagnostics: list[dict[str, Any]] = []
        effective_map, title_map = _effective_numeric_citation_map(
            fragment, view.materials, explicit_map,
            origin=map_origin, planning_revision=planning_revision, diagnostics=mapping_diagnostics,
        )
        fragment, repairs = _repair_numeric_citations(
            fragment, effective_map, payload.get("requested_source_handles", []))
        for repair in repairs:
            repair["mapping_origin"] = ("bibliography_exact_title" if map_origin == "generated_local_aliases"
                                        or explicit_map is None else "caller_explicit")
        result.update(_output_diagnostics(fragment, payload.get("requested_source_handles", []),
                                          _known_tool_identifiers(payload)))
        result["citation_mapping_diagnostics"] = mapping_diagnostics
        result["citation_problems"].extend(mapping_diagnostics)
        result["citation_scope"] = "completion_fragment"
        result["numeric_citation_repairs"] = repairs
        result["bibliography_title_citation_map"] = title_map
        result["citation_number_map_origin"] = map_origin or "none"
        result["output_normalization"] = normalization
        result["completion_fragment"] = fragment
        status = str(envelope.get("status") or "").strip() or "pending"
        covered = envelope.get("covered_task_ids")
        result["covered_task_ids"] = [
            str(item) for item in covered
        ] if isinstance(covered, Sequence) and not isinstance(covered, (str, bytes, bytearray)) else []
        raw_issues = envelope.get("issues")
        result["issues"] = list(raw_issues) if isinstance(raw_issues, list) else ([raw_issues] if raw_issues else [])
        result["issues"].extend(item for item in normalization if item.get("code") == "table_markdown_missing_or_invalid")
        result["model_status"] = status
        result["response_envelope"] = dict(envelope)
        table_requested = bool(payload.get("table_tasks"))
        table_check = _markdown_table_check(fragment) if table_requested else {"valid": None}
        result["table_check"] = table_check
        incomplete = bool(call_error) or result["finish_reason"] == "length" or not result["complete"]
        if incomplete:
            if fragment.strip():
                result["completion_fragment"] = fragment
            result["issues"].append({
                "code": "completion_response_incomplete",
                "finish_reason": result["finish_reason"],
                "complete": result["complete"],
            })
        elif status == "already_covered" and not fragment.strip():
            result["issues"].append({
                "code": "model_already_covered",
                "note": "模型判断已覆盖；未自动验收",
            })
        elif status != "appended":
            if fragment.strip():
                result["completion_fragment"] = fragment
            result["issues"].append({
                "code": "completion_status_not_appended",
                "status": status,
            })
        elif not fragment.strip():
            result["issues"].append({"code": "completion_fragment_empty"})
        elif table_requested and not table_check["valid"]:
            result["issues"].append({"code": "markdown_table_missing_or_invalid"})
        else:
            if simulated:
                fragment = "> ⚠️ 模拟补写（假客户端，仅验证接线，非质量结论）\n\n" + fragment
                result["completion_fragment"] = fragment
            else:
                result["completion_fragment"] = fragment
            result["body_markdown"] = existing_body + ("" if not existing_body else "\n\n") + result["completion_fragment"]
            result["pending"] = False
    except Exception as exc:
        result["issues"].append({
            "code": "completion_response_parse_failed",
            "error": type(exc).__name__ + ":" + str(exc),
        })
        result["model_status"] = "pending"
    return result


def write_unit_completion(
    view: UnitWritingView,
    result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    estimate: Mapping[str, Any],
    language: str,
) -> dict[str, Any]:
    """Write completion artifacts without altering the source or normal unit output."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    original = str(result.get("existing_body_markdown") or "")
    fragment = str(result.get("completion_fragment") or "")
    completed = str(result.get("body_markdown") or original)
    (target / "ORIGINAL_BODY.md").write_bytes(original.encode("utf-8"))
    (target / "COMPLETION_FRAGMENT.md").write_text(fragment, encoding="utf-8")
    (target / "COMPLETED_BODY.md").write_bytes(completed.encode("utf-8"))
    (target / "COMPLETION_MESSAGES.json").write_text(
        json.dumps(result.get("messages") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    payload = result.get("payload") or {}
    (target / "COMPLETION_INPUT.json").write_text(json.dumps({
        "schema_version": INPUT_SCHEMA + ".completion",
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "language": language,
        "existing_body_path": str(target / "ORIGINAL_BODY.md"),
        "payload": payload,
        "estimate": dict(estimate),
        "source_handles": list(result.get("source_handles") or []),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "schema_version": RESULT_SCHEMA + ".completion",
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "language": language,
        "task_ids": list(result.get("task_ids") or []),
        "source_handles": list(result.get("source_handles") or []),
        "model": result.get("model") or "",
        "simulated": bool(result.get("simulated")),
        "pending": bool(result.get("pending", True)),
        "model_status": result.get("model_status") or "pending",
        "covered_task_ids": list(result.get("covered_task_ids") or []),
        "table_check": result.get("table_check") or {},
        "content_review_status": "not_reviewed",
        "citation_scope": "completion_fragment",
        "used_source_handles": list(result.get("used_source_handles") or []),
        "unknown_citations": list(result.get("unknown_citations") or []),
        "unresolved_numeric_citations": list(result.get("unresolved_numeric_citations") or []),
        "citation_problems": list(result.get("citation_problems") or []),
        "fence_report": result.get("fence_report") or {},
        "output_normalization": list(result.get("output_normalization") or []),
        "numeric_citation_repairs": list(result.get("numeric_citation_repairs") or []),
        "bibliography_title_citation_map": dict(result.get("bibliography_title_citation_map") or {}),
        "citation_number_map_origin": result.get("citation_number_map_origin") or "none",
        "citation_mapping_diagnostics": list(result.get("citation_mapping_diagnostics") or []),
        "known_tool_identifiers": list(result.get("known_tool_identifiers") or []),
        "non_source_identifier_citations": list(result.get("non_source_identifier_citations") or []),
        "issues": list(result.get("issues") or []),
        "call_error": result.get("call_error") or "",
        "estimate": dict(estimate),
        "usage": dict(result.get("usage") or {}),
        "finish_reason": result.get("finish_reason") or "",
        "complete": bool(result.get("complete", False)),
        "raw_response": result.get("raw_response") or "",
        "original_body_path": str(target / "ORIGINAL_BODY.md"),
        "fragment_path": str(target / "COMPLETION_FRAGMENT.md"),
        "completed_body_path": str(target / "COMPLETED_BODY.md"),
        "note": "定点补写结果；模型状态和 covered_task_ids 仅作诊断，未自动宣称质量通过。",
    }
    (target / "COMPLETION_RESULT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def unit_messages(
    view: UnitWritingView,
    *,
    prompt: str | None = None,
    language: str = "zh",
    payload: Mapping[str, Any] | None = None,
    planning_revision: bool = False,
    citation_number_map: Mapping[Any, Any] | None = None,
) -> list[dict[str, str]]:
    body = (dict(payload) if payload is not None else
            unit_payload(view, language=language, planning_revision=planning_revision,
                         citation_number_map=citation_number_map))
    planning_revision = bool(planning_revision or body.get("planning_revision_mode"))
    if citation_number_map is not None:
        if planning_revision:
            body["planning_revision_mode"] = True
        body["citation_number_map"] = dict(citation_number_map)
        body["citation_number_map_origin"] = "caller_explicit"
    elif planning_revision and "citation_number_map" not in body:
        body["planning_revision_mode"] = True
        body["citation_number_map"] = _local_numeric_citation_map(_known_unit_handles(view))
        body["citation_number_map_origin"] = "generated_local_aliases"
    system = prompt if prompt is not None else load_writer_prompt(planning_revision=planning_revision)
    if planning_revision:
        system = _with_revision_output(system)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False, indent=2)},
    ]


@lru_cache(maxsize=1)
def _default_qwen_token_counter() -> Any | None:
    """Load the repository's local Qwen tokenizer without downloading assets."""

    try:
        from .progressive_review_plan import qwen_local_token_counter

        path = Path(__file__).resolve().parents[3] / "data/tokenizers/qwen3_5_9b/tokenizer.json"
        return qwen_local_token_counter(path)
    except Exception:
        # The offline preview remains usable in a minimal environment.  The
        # fallback below is a conservative UTF-8 upper bound, never the old
        # character/3 guess that undercounted Chinese prompts.
        return None


def _fallback_prompt_token_upper_bound(messages: Sequence[Mapping[str, Any]]) -> int:
    raw = json.dumps(messages, ensure_ascii=False).encode("utf-8")
    return max(1, len(raw) + 8192 + 256 * max(1, len(messages)))


def estimate_unit_cost(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    output_tokens: int = 4000,
    thinking_budget: int = 0,
    token_counter: Any | None = None,
) -> dict[str, Any]:
    """Conservative pre-dispatch estimate for one unit writing call."""

    from .module4.runtime import estimated_cost_cny

    counter = token_counter or _default_qwen_token_counter()
    if counter is not None:
        prompt_tokens = int(counter(b"", messages))
        estimate_method = "qwen_local_tokenizer" if token_counter is None else "provided_token_counter"
    else:
        prompt_tokens = _fallback_prompt_token_upper_bound(messages)
        estimate_method = "utf8_conservative_upper_bound"
    reserved_input = int(prompt_tokens * 1.12) + 8192
    return {
        "prompt_tokens_estimate": prompt_tokens,
        "reserved_input_tokens": reserved_input,
        "output_tokens": int(output_tokens),
        "thinking_budget": int(thinking_budget),
        "total_context_tokens": reserved_input + int(output_tokens) + int(thinking_budget),
        "estimated_cost_cny": estimated_cost_cny(
            {"prompt_tokens": reserved_input, "completion_tokens": int(output_tokens) + int(thinking_budget)},
            model=model, conservative=True),
        "model": model,
        "tokenizer": estimate_method,
    }


# --------------------------------------------------------------------------
# output handling
# --------------------------------------------------------------------------


def _strip_fences(text: str, *, json_envelope: bool = False) -> str:
    """Unwrap one complete Markdown transport wrapper, not an actual code example."""
    value = text.strip("\r\n").rstrip()
    lines = value.splitlines()
    if len(lines) < 3:
        return value
    opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})([A-Za-z]*)\s*", lines[0])
    if not opening:
        return value
    fence, language = opening.group(1), opening.group(2).lower()
    allowed = {"", "markdown", "md"} | ({"json"} if json_envelope else set())
    if language not in allowed:
        return value
    closer = re.compile(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*")
    closes = [index for index, line in enumerate(lines[1:], 1) if closer.fullmatch(line)]
    if not closes or closes[0] != len(lines) - 1:
        return value
    inner = "\n".join(lines[1:-1]).strip("\r\n").rstrip()
    if not language and not (json_envelope and inner.startswith("{")):
        # With no language label, only structural Markdown makes this an
        # unambiguous output wrapper rather than a standalone code example.
        if not re.search(r"(?m)^#{1,6}\s+\S", inner) and not _markdown_table_check(inner)["valid"]:
            return value
    return inner


def _inline_link_spans(text: str) -> Iterable[tuple[int, int]]:
    """Bound link protection at its own closing parenthesis, including titles."""
    for match in re.finditer(r"!?\[[^\]\n]*\]\(", text):
        depth, quote, index = 1, "", match.end()
        while index < len(text) and text[index] not in "\r\n":
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'" and text[index - 1].isspace():
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield match.start(), index + 1
                    break
            index += 1


def _prose_segments(
    text: str, *, formatted_paper_citations: bool = False,
) -> Iterable[tuple[str, bool]]:
    """Keep code and Markdown link constructs byte-for-byte during repairs."""
    protected: list[tuple[int, int]] = []
    hidden = _fenced_line_indices(text)
    position = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        if index in hidden or line.startswith(("    ", "\t")):
            protected.append((position, position + len(line)))
        position += len(line)
    for match in re.finditer(r"(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", text):
        inner = match.group(0)[len(match.group(1)):-len(match.group(1))].strip()
        reference_only = re.fullmatch(
            r"(?:\[\s*P\d{3,}(?:\s*[,;]\s*P\d{3,})*\s*\]\s*)+", inner)
        # Some real writer responses format references as inline code. Count
        # only pure paper-reference spans, without relaxing numeric repairs.
        if not (formatted_paper_citations and reference_only):
            protected.append((match.start(), match.end()))
    for pattern in (
        r"(?m)^ {0,3}\[[^\]\n]+\]:[^\n]*(?:\n[ \t]+[^\n]*)*",
        r"https?://[^\s<>()]+",
    ):
        protected.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    protected.extend(_inline_link_spans(text))
    # Numeric-looking reference labels are links when defined in this body.
    # Preserve full, collapsed and shortcut references rather than guessing.
    definitions = {" ".join(match.group(1).split()).casefold() for match in re.finditer(
        r"(?m)^ {0,3}\[([^\]\n]+)\]:", text)}
    for match in re.finditer(r"!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?", text):
        label = match.group(2) if match.group(2) else match.group(1)
        if " ".join(label.split()).casefold() in definitions:
            protected.append((match.start(), match.end()))
    position = 0
    for start, end in sorted(protected):
        if end <= position:
            continue
        if start > position:
            yield text[position:start], True
        yield text[max(start, position):end], False
        position = end
    yield text[position:], True


def _citation_matches(
    text: str, *, canonical_handles: bool = False, known_identifiers: Iterable[str] = (),
) -> Iterable[re.Match[str]]:
    matches = list(CITATION_RE.finditer(text))

    def citation_like(value: str) -> bool:
        stripped = value.strip()
        if stripped in known_identifiers or CITATION_SEQUENCE_RE.fullmatch(stripped):
            return True
        prefix = CITATION_PREFIX_RE.match(stripped)
        return bool(prefix and CITATION_SEQUENCE_RE.fullmatch(stripped[prefix.end():].strip()))

    for index, match in enumerate(matches):
        if match.start() and text[match.start() - 1] in "\\!":
            continue
        if index and matches[index - 1].end() == match.start():
            previous_start = matches[index - 1].start()
            if previous_start and text[previous_start - 1] == "!":
                continue
        # Actual defined reference links are already masked by _prose_segments.
        # An undefined explanatory [label] next to [Pxxxx] is prose, not a
        # reason to lose a canonical handle (including an unknown handle).
        # Keep the conservative adjacency rule for numeric citation repairs.
        if canonical_handles and re.fullmatch(
            r"\s*P\d{3,}(?:\s*[,;]\s*P\d{3,})*\s*", match.group(1)
        ):
            yield match
            continue
        suffix = text[match.end():].lstrip()
        # Reference links have two bracket groups. Preserve both, while still
        # recognizing the writer's canonical adjacent [P0001][P0002] citations.
        if suffix.startswith("[") and index + 1 < len(matches):
            following = matches[index + 1]
            if not (citation_like(match.group(1)) and citation_like(following.group(1))):
                continue
        if index and matches[index - 1].end() == match.start():
            if not citation_like(matches[index - 1].group(1)):
                continue
        yield match


def _repair_numeric_citations(
    text: str, mapping: Mapping[Any, Any] | None, known_handles: Iterable[str],
) -> tuple[str, list[dict[str, str]]]:
    """Only a caller's explicit number -> currently provided handle can repair."""
    known = set(known_handles)
    explicit = {str(key): value for key, value in (mapping or {}).items()
                if str(key).isdigit() and isinstance(value, str) and value in known}
    repairs: list[dict[str, str]] = []
    output: list[str] = []
    for segment, prose in _prose_segments(text):
        if not prose:
            output.append(segment)
            continue
        position = 0
        for match in _citation_matches(segment):
            bracket = match.group(1).strip()
            if not re.fullmatch(r"\d+(?:\s*[,;]\s*\d+)*", bracket):
                continue
            numbers = re.split(r"\s*[,;]\s*", bracket)
            if not all(number in explicit for number in numbers):
                continue
            replacement = "".join("[" + explicit[number] + "]" for number in numbers)
            output.extend((segment[position:match.start()], replacement))
            repairs.append({"original": match.group(0), "replacement": replacement})
            position = match.end()
        output.append(segment[position:])
    return "".join(output), repairs


def _local_numeric_citation_map(known_handles: Iterable[str]) -> dict[str, str]:
    """Map unique numeric aliases from the current unit's local handles.

    This is deliberately narrower than a publication-reference mapping: only
    the unit's own ``P####`` handles participate, and aliases shared by more
    than one handle remain unresolved.
    """

    aliases: dict[str, set[str]] = {}
    for raw_handle in known_handles:
        handle = str(raw_handle or "").strip()
        match = re.fullmatch(r"P(\d{3,})", handle)
        if not match:
            continue
        digits = match.group(1)
        keys = {digits, str(int(digits))}
        for key in keys:
            aliases.setdefault(key, set()).add(handle)
    return {
        key: next(iter(handles))
        for key, handles in aliases.items()
        if len(handles) == 1
    }


def _trailing_numbered_bibliography(text: str) -> list[tuple[str, str]]:
    """Read only complete numbered title lines at the end of a model body."""

    lines = str(text or "").splitlines()
    found: list[tuple[str, str]] = []
    index = len(lines) - 1
    while index >= 0:
        line = lines[index].strip()
        if not line:
            index -= 1
            continue
        match = re.fullmatch(r"\[(\d+)\]\s+(.+?)\s*", line)
        if not match:
            break
        found.append((match.group(1), match.group(2)))
        index -= 1
    return list(reversed(found))


def _citation_title_key(value: Any) -> str:
    """Keep scientific symbols; normalize only Unicode, case and whitespace."""
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def _bibliography_title_citation_map(
    text: str, materials: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Resolve trailing bibliography numbers by exact local source title only."""

    titles: dict[str, set[str]] = {}
    for item in materials or ():
        if not isinstance(item, Mapping):
            continue
        handle = str(item.get("source_handle") or "").strip()
        title = _citation_title_key(item.get("title"))
        if handle and title:
            titles.setdefault(title, set()).add(handle)
    candidates: dict[str, set[str]] = {}
    invalid: set[str] = set()
    for number, title in _trailing_numbered_bibliography(text):
        handles = titles.get(_citation_title_key(title), set())
        if len(handles) != 1:
            invalid.add(number)
        candidates.setdefault(number, set()).update(handles)
    return {
        number: next(iter(handles))
        for number, handles in candidates.items()
        if len(handles) == 1 and number not in invalid
    }


def _numeric_citation_numbers(text: str) -> set[str]:
    return {number for segment, prose in _prose_segments(text) if prose
            for match in _citation_matches(segment)
            if re.fullmatch(r"\d+(?:\s*[,;\-–]\s*\d+)*", match.group(1).strip())
            for number in re.split(r"\s*[,;\-–]\s*", match.group(1).strip())}


def _effective_numeric_citation_map(
    text: str,
    materials: Iterable[Mapping[str, Any]],
    mapping: Mapping[Any, Any] | None,
    *,
    origin: str = "",
    planning_revision: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Use confirmed caller identities or complete, unique local title evidence.

    An advertised P-number suffix alias does not establish the dialect chosen
    by a model. Never partially apply those aliases to sequential references.
    Title matching is lexical evidence, not independent DOI verification.
    """
    title_map = (_bibliography_title_citation_map(text, materials)
                 if planning_revision else {})
    supplied = {str(key): value for key, value in (mapping or {}).items()}
    if origin != "generated_local_aliases" and (mapping is not None or origin == "caller_explicit"):
        return supplied, title_map
    numbers = _numeric_citation_numbers(text)
    problems = diagnostics if diagnostics is not None else []
    if numbers and origin == "generated_local_aliases" and not numbers.issubset(title_map):
        problems.append({"code": "numeric_citation_generated_aliases_unconfirmed",
                         "numbers": sorted(numbers),
                         "note": "Generated handle suffixes do not confirm the model's numeric reference identities; preserved verbatim."})
    definitions = {number for number, _ in _trailing_numbered_bibliography(text)}
    ambiguous = definitions - title_map.keys() if planning_revision else set()
    if ambiguous or (title_map and not numbers.issubset(title_map)):
        problems.append({"code": "numeric_citation_mapping_ambiguous",
                         "numbers": sorted(ambiguous | (numbers - title_map.keys())),
                         "note": "Incomplete or conflicting full-title evidence; no partial numeric conversion."})
        return {}, title_map
    return dict(title_map), title_map


def _known_tool_identifiers(payload: Any) -> list[str]:
    """Read explicitly typed tool IDs, including JSON-encoded reading answers.

    Free prose and model output cannot establish an identifier. IDs are only
    collected below material/tool containers, never inferred from Q/R prefixes.
    """
    found: set[str] = set()
    id_keys = {"question_id", "question_ids", "tool_call_id", "tool_id", "request_id"}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in id_keys:
                    values = item if isinstance(item, list) else [item]
                    found.update(v for v in values if isinstance(v, str)
                                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", v)
                                 and not re.fullmatch(r"P\d{3,}", v))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return
            if isinstance(decoded, (dict, list)):
                visit(decoded)

    if isinstance(payload, Mapping):
        for key in ("chapter_tool_materials", "sources", "materials"):
            visit(payload.get(key))
    return sorted(found)


def _output_diagnostics(
    body: str, known_handles: Iterable[str], known_tool_identifiers: Iterable[str] = (),
) -> dict[str, Any]:
    used = citations_in(body)
    known = set(known_handles)
    unknown = [handle for handle in used if handle not in known]
    numeric: list[str] = []
    for segment, prose in _prose_segments(body):
        if prose:
            for match in _citation_matches(segment):
                if re.fullmatch(r"\d+(?:\s*[,;\-–]\s*\d+)*", match.group(1).strip()):
                    if match.group(0) not in numeric:
                        numeric.append(match.group(0))
    tool_ids = set(known_tool_identifiers)
    tool_citations: list[str] = []
    tool_problems: list[dict[str, str]] = []
    for segment, prose in _prose_segments(body):
        if not prose:
            continue
        for match in _citation_matches(segment, known_identifiers=tool_ids):
            identifier = match.group(1).strip()
            if identifier in tool_ids and match.group(0) not in tool_citations:
                tool_citations.append(match.group(0))
                tool_problems.append({"code": "non_source_identifier_citation",
                                      "identifier": identifier, "citation": match.group(0),
                                      "note": "Known input tool identifier used in citation form; preserved without guessing a paper."})
    problems = [{"code": "citation_not_in_unit_sources", "handle": handle,
                 "note": "保留模型原文，不猜替换成别的来源"} for handle in unknown]
    problems.extend({"code": "numeric_citation_unresolved", "citation": value,
                     "note": "没有可用的显式编号到当前来源映射，保留原文"} for value in numeric)
    problems.extend(tool_problems)
    fenced = sorted(index + 1 for index in _fenced_line_indices(body))
    return {"used_source_handles": used, "unknown_citations": unknown,
            "known_tool_identifiers": sorted(tool_ids),
            "non_source_identifier_citations": tool_citations,
            "unresolved_numeric_citations": numeric, "citation_problems": problems,
            "fence_report": {"remaining_fenced_lines": fenced},
            "content_review_status": "not_reviewed"}


def _merge_table_markdown(body: str, table: str) -> str:
    known = {item["fingerprint"] for item in _markdown_tables(body)}
    duplicates = [item for item in _markdown_tables(table) if item["fingerprint"] in known]
    lines = table.splitlines()
    for item in reversed(duplicates):
        del lines[item["start"]:item["end"]]
    remaining = "\n".join(lines).strip()
    return body + ("\n\n" if body and remaining else "") + remaining


def _consume_unit_output(response: Any) -> tuple[str, list[dict[str, Any]]]:
    """Consume generated prose and independent finished tables, never tasks."""
    if isinstance(response, str):
        envelope = _decode_json_content(response)
        if envelope is not None:
            return _consume_unit_output(envelope)
        text = _strip_fences(response)
        if text:
            issue = [{"code": "outer_markdown_fence_unwrapped"}] if text != response.strip("\r\n").rstrip() else []
            return text, issue
    if isinstance(response, Mapping):
        body = ""
        issues: list[dict[str, Any]] = []
        for key in ("body_markdown", "content", "markdown", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                if key == "body_markdown":
                    body = _strip_fences(value)
                    issues = ([{"code": "outer_markdown_fence_unwrapped", "field": key}]
                              if body != value.strip("\r\n").rstrip() else [])
                else:
                    body, issues = _consume_unit_output(value)
                break
        if not body:
            for key in ("response", "message"):
                if isinstance(response.get(key), (Mapping, str)):
                    body, issues = _consume_unit_output(response[key])
                    break
            choices = response.get("choices")
            if not body and isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)) and choices:
                body, issues = _consume_unit_output(choices[0])
        table = response.get("table_markdown")
        if isinstance(table, str) and table.strip():
            normalized = _strip_fences(table)
            if normalized != table.strip("\r\n").rstrip():
                issues.append({"code": "outer_markdown_fence_unwrapped", "field": "table_markdown"})
            if _markdown_table_check(normalized)["valid"]:
                body = _merge_table_markdown(body, normalized)
            else:
                issues.append({"code": "table_markdown_missing_or_invalid"})
        if body:
            return body, issues
    raise UnitWritingError("unit_body_empty_or_unreadable")


def _known_unit_handles(view: UnitWritingView) -> list[str]:
    return list(view.sources) or [str(item.get("source_handle") or "") for item in view.materials]


def _table_consumption_report(body: str, table_requested: bool) -> dict[str, Any]:
    check = _markdown_table_check(body) if table_requested else {"valid": None}
    missing = table_requested and not check["valid"]
    return {"table_check": check, "output_consumption_status": "pending_table" if missing else "consumed",
            "content_review_status": "not_reviewed",
            "issues": [{"code": "markdown_table_missing_or_invalid"}] if missing else []}

def _decode_json_content(value: str) -> Mapping[str, Any] | None:
    """Decode a provider content string when it contains our JSON envelope."""

    text = _strip_fences(value, json_envelope=True).strip()
    if not text or text[0] != "{" or text[-1] != "}":
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        try:
            # Some providers preserve literal newlines in the JSON string.
            # strict=False accepts those control characters without changing
            # the Markdown payload.
            parsed = json.loads(text, strict=False)
        except (TypeError, ValueError):
            # Models sometimes emit LaTeX such as \sim with a single JSON
            # backslash. Preserve that text literally without rewriting prose.
            if '"body_markdown"' not in text:
                return None
            repaired = re.sub(
                r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})|\\',
                lambda match: "\\\\" if match.group(0) == "\\" else match.group(0),
                text,
            )
            try:
                parsed = json.loads(repaired, strict=False)
            except (TypeError, ValueError):
                # Some provider responses quote the body as JSON but leave a
                # literal quote in prose (for example ``"irAE"``). Recover
                # only that body string and preserve any readable issues.
                marker = '"body_markdown"'
                marker_at = text.find(marker)
                if marker_at < 0:
                    return None
                value_at = text.find('"', text.find(":", marker_at) + 1)
                if value_at < 0:
                    return None
                suffixes = list(re.finditer(r'"\s*,\s*"issues"\s*:', text[value_at + 1:]))
                if not suffixes:
                    return None
                end_at = value_at + 1 + suffixes[-1].start()
                fragment = text[value_at + 1:end_at]
                encoded: list[str] = []
                index = 0
                while index < len(fragment):
                    char = fragment[index]
                    if char == "\\":
                        if index + 1 < len(fragment):
                            nxt = fragment[index + 1]
                            if nxt == "u" and index + 5 < len(fragment) and re.match(r"^[0-9a-fA-F]{4}$", fragment[index + 2:index + 6]):
                                encoded.append(fragment[index:index + 6])
                                index += 6
                                continue
                            if nxt in '"\\/bfnrt':
                                encoded.append(fragment[index:index + 2])
                                index += 2
                                continue
                        encoded.append("\\\\")
                        index += 1
                        continue
                    if char == '"':
                        encoded.append('\\"')
                    elif char == "\n":
                        encoded.append("\\n")
                    elif char == "\r":
                        encoded.append("\\r")
                    elif char == "\t":
                        encoded.append("\\t")
                    else:
                        encoded.append(char)
                    index += 1
                try:
                    body = json.loads('"' + ''.join(encoded) + '"')
                except (TypeError, ValueError):
                    return None

                issues: Any = [{
                    "issue_id": "malformed_json_envelope",
                    "problem": "issues_field_unreadable",
                }]
                issues_at = text.find('"issues"', end_at)
                if issues_at >= 0:
                    colon_at = text.find(":", issues_at)
                    closing_at = text.rfind("}")
                    if colon_at >= 0 and closing_at > colon_at:
                        raw_issues = text[colon_at + 1:closing_at].strip()
                        try:
                            parsed_issues = json.loads(raw_issues, strict=False)
                        except (TypeError, ValueError):
                            pass
                        else:
                            issues = parsed_issues if isinstance(parsed_issues, list) else [parsed_issues]
                return {"body_markdown": body, "issues": issues}
    return parsed if isinstance(parsed, Mapping) else None


def parse_unit_body(response: Any) -> str:
    """Return all generated prose and finished tables from a provider envelope."""
    return _consume_unit_output(response)[0]

def _response_issues(response: Any) -> list[dict[str, Any]]:
    """Read optional caller-facing issues without making them part of prose."""

    if isinstance(response, str):
        envelope = _decode_json_content(response)
        if envelope is not None:
            return _response_issues(envelope)
    raw = response.get("issues") if isinstance(response, Mapping) else None
    if raw is None and isinstance(response, Mapping):
        nested = response.get("response")
        if isinstance(nested, Mapping):
            return _response_issues(nested)
        content = response.get("content")
        if isinstance(content, str):
            envelope = _decode_json_content(content)
            if envelope is not None:
                return _response_issues(envelope)
        choices = response.get("choices")
        if isinstance(choices, Sequence):
            for choice in choices:
                found = _response_issues(choice)
                if found:
                    return found
        message = response.get("message")
        if isinstance(message, Mapping):
            return _response_issues(message)
    output: list[dict[str, Any]] = []
    for item in raw or ():
        if isinstance(item, Mapping):
            row = {
                key: item[key]
                for key in ("issue_id", "chapter_id", "unit_id", "paragraph_id", "problem", "source_handles", "action", "status")
                if key in item and item[key] not in (None, "", [], {})
            }
        elif isinstance(item, str) and item.strip():
            row = {"problem": item.strip()}
        else:
            continue
        if row:
            output.append(row)
    return output


def citations_in(body: str) -> list[str]:
    """Handles actually cited in the body (including inside Markdown tables)."""

    found: list[str] = []
    for segment, prose in _prose_segments(body or "", formatted_paper_citations=True):
        if prose:
            for bracket in _citation_matches(segment, canonical_handles=True):
                for handle in HANDLE_RE.findall(bracket.group(1)):
                    if handle not in found:
                        found.append(handle)
    return found


def simulated_banner(view: UnitWritingView, source: str) -> str:
    return (
        "> ⚠️ **模拟输出（假客户端，不是模型生成的正文）**\n"
        "> 来源：%s\n"
        "> 单元：%s / %s。本文件只用于验证接线（素材是否送到、引用是否对上），"
        "**不能作为写作质量通过的依据**。\n\n" % (source, view.chapter_id, view.unit_id)
    )


def write_unit_output(
    view: UnitWritingView,
    body: str,
    output_dir: str | Path,
    *,
    model: str,
    language: str,
    mode: str,
    used_messages: Sequence[Mapping[str, Any]],
    estimate: Mapping[str, Any],
    usage: Mapping[str, Any] | None = None,
    simulated_from: str = "",
    response_path: str = "",
    finish_reason: str = "",
    complete: bool = True,
    partial_error: str = "",
    issues: Sequence[Mapping[str, Any]] = (),
    citation_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Store the model's Markdown and the program's honest bookkeeping."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    context = {"materials": view.materials, "chapter_tool_materials": view.chapter_tool_materials}
    known_ids = set(_known_tool_identifiers(context))
    prior = citation_diagnostics or {}
    known_ids.update(prior.get("known_tool_identifiers") or [])
    for message in used_messages:
        try:
            message_payload = json.loads(message.get("content") or "")
        except (ValueError, TypeError):
            continue
        known_ids.update(_known_tool_identifiers(message_payload))
    diagnostics = _output_diagnostics(body, _known_unit_handles(view), known_ids)
    for key in ("numeric_citation_repairs", "bibliography_title_citation_map",
                "citation_number_map_origin", "citation_mapping_diagnostics"):
        if key in prior:
            diagnostics[key] = deepcopy(prior[key])
    diagnostics["citation_problems"].extend(prior.get("citation_mapping_diagnostics") or [])
    consumption = _table_consumption_report(body, bool(view.table_tasks))
    used = diagnostics["used_source_handles"]
    unknown = diagnostics["unknown_citations"]
    unused = [handle for handle in view.sources if handle not in used]
    banner = simulated_banner(view, simulated_from) if mode == "fake" else ""
    markdown_path = target / ("UNIT_BODY.simulated.md" if mode == "fake" else "UNIT_BODY.md")
    markdown_path.write_text(banner + body.rstrip() + "\n", encoding="utf-8")

    result = {
        "schema_version": RESULT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": mode,
        "simulated": mode == "fake",
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "model": model,
        "language": language,
        "body_markdown": body,
        "body_path": str(markdown_path),
        "used_source_handles": used,
        "unused_source_handles": unused,
        "unknown_citations": unknown,
        "source_count": len(view.sources),
        "material_summary": view.material_summary(),
        "material_notes": view.material_notes,
        "warnings": view.warnings,
        "estimate": dict(estimate),
        "usage": dict(usage or {}),
        "finish_reason": finish_reason,
        "complete": bool(complete),
        "completion_status": (
            "partial_length" if finish_reason == "length" or (not complete and body)
            else "complete"
        ),
        "partial_error": partial_error,
        "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
        **diagnostics,
        **{key: value for key, value in consumption.items() if key != "issues"},
        "arrangement_path": view.arrangement_path,
        "view_path": view.view_path,
        "input_path": str(target / "UNIT_INPUT.json"),
        "messages_path": str(target / "UNIT_MESSAGES.json"),
        "raw_response": response_path,
        "simulated_from": simulated_from,
        "note": (
            "假客户端模拟输出，只验证接线；正文质量未经验证。"
            if mode == "fake" else
            "真实模型返回长度受限的可续用正文；本次输出未宣称完成。"
            if finish_reason == "length" or (not complete and body) else
            "真实模型输出；未做全章合稿、未做审稿循环、未做事实核查。"
        ),
    }
    for issue in consumption["issues"]:
        if issue not in result["issues"]:
            result["issues"].append(issue)
    (target / "UNIT_RESULT.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def write_unit_input(
    view: UnitWritingView,
    messages: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    *,
    estimate: Mapping[str, Any],
    language: str,
) -> Path:
    """The exact input a reviewer can inspect before any real call."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": INPUT_SCHEMA,
        "chapter_id": view.chapter_id,
        "unit_id": view.unit_id,
        "language": language,
        "arrangement_path": view.arrangement_path,
        "view_path": view.view_path,
        "material_summary": view.material_summary(),
        "materials": view.materials,
        "material_notes": view.material_notes,
        "warnings": view.warnings,
        "estimate": dict(estimate),
        "task_counts": {
            "paragraph_tasks": len(view.paragraph_tasks),
            "table_tasks": len(view.table_tasks),
        },
        "handles": list(view.sources),
    }
    (target / "UNIT_INPUT.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "UNIT_MESSAGES.json").write_text(
        json.dumps(list(messages), ensure_ascii=False, indent=2), encoding="utf-8")
    return target / "UNIT_INPUT.json"


def run_unit_writing(
    view: UnitWritingView,
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    prompt: str | None = None,
    language: str = "zh",
    payload: Mapping[str, Any] | None = None,
    raw_response_dir: str | Path | None = None,
    planning_revision: bool = False,
    citation_number_map: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """One writing call, with the raw response kept on disk."""

    from .module4.runtime import invoke_client

    messages = unit_messages(
        view, prompt=prompt, language=language, payload=payload,
        planning_revision=planning_revision, citation_number_map=citation_number_map,
    )
    effective_payload: Mapping[str, Any] | None = payload
    try:
        decoded_payload = json.loads(messages[-1]["content"])
    except (TypeError, ValueError, KeyError, IndexError):
        decoded_payload = None
    if isinstance(decoded_payload, Mapping):
        effective_payload = decoded_payload
    partial_error = ""
    try:
        response = invoke_client(client, messages)
    except Exception as exc:
        # The direct Qwen client records a ``length`` response before raising
        # its transport error.  Keep that usable Markdown for continuation,
        # while exposing the incomplete finish reason to the caller.
        record = getattr(exc, "record", None)
        if not isinstance(record, Mapping) or not isinstance(record.get("content"), str):
            raise
        if not record.get("content", "").strip():
            raise
        response = dict(record)
        partial_error = type(exc).__name__ + ":" + str(exc)
    raw_path = ""
    if raw_response_dir is not None:
        directory = Path(raw_response_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        raw_path = str(directory / (portable_component(f"{view.chapter_id}_{view.unit_id}_{stamp}") + ".raw"))
        Path(raw_path).write_text(json.dumps(response, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    body, normalization = _consume_unit_output(response)
    # The caller's payload is trusted task context; never read a mapping out of
    # the model response. No implicit positional/P-number-suffix conversion.
    map_origin = "caller_explicit" if citation_number_map is not None else str(
        effective_payload.get("citation_number_map_origin") or "") if effective_payload else ""
    explicit_map = citation_number_map
    if explicit_map is None and effective_payload is not None:
        candidate = effective_payload.get("citation_number_map")
        explicit_map = candidate if isinstance(candidate, Mapping) else None
    if explicit_map is not None and not map_origin:
        map_origin = "caller_explicit"
    mapping_diagnostics: list[dict[str, Any]] = []
    effective_map, title_map = _effective_numeric_citation_map(
        body, view.materials, explicit_map,
        origin=map_origin, planning_revision=planning_revision, diagnostics=mapping_diagnostics,
    )
    known_handles = _known_unit_handles(view)
    body, repairs = _repair_numeric_citations(body, effective_map, known_handles)
    for repair in repairs:
        repair["mapping_origin"] = ("bibliography_exact_title" if map_origin == "generated_local_aliases"
                                    or explicit_map is None else "caller_explicit")
    diagnostics = _output_diagnostics(body, known_handles, _known_tool_identifiers(effective_payload))
    diagnostics["citation_mapping_diagnostics"] = mapping_diagnostics
    diagnostics["citation_problems"].extend(mapping_diagnostics)
    consumption = _table_consumption_report(body, bool(view.table_tasks))
    issues = _response_issues(response)
    issues.extend(item for item in normalization if item.get("code") == "table_markdown_missing_or_invalid")
    issues.extend(item for item in consumption["issues"] if item not in issues)
    return {
        "body_markdown": body,
        **diagnostics,
        **{key: value for key, value in consumption.items() if key != "issues"},
        "output_normalization": normalization,
        "numeric_citation_repairs": repairs,
        "bibliography_title_citation_map": title_map,
        "citation_number_map_origin": map_origin or "none",
        "messages": messages,
        "usage": dict(response.get("usage") or {}),
        "finish_reason": response.get("finish_reason") or "",
        "complete": bool(response.get("complete", not partial_error)),
        "partial_error": partial_error,
        "model": response.get("model") or model,
        "raw_response": raw_path,
        "issues": issues,
    }
