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
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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
HANDLE_RE = re.compile(r"\bP\d{3,}\b")

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
    return arrangement


def select_unit(arrangement: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    for unit in arrangement.get("units") or ():
        if str(unit.get("unit_id") or "") == unit_id:
            return dict(unit)
    known = ", ".join(str(unit.get("unit_id")) for unit in arrangement.get("units") or ())
    raise UnitWritingError(f"unit_not_in_arrangement:{unit_id}|known:{known}")


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
    chapter_frame: dict[str, str]
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

    unit = select_unit(arrangement, unit_id)
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
        review_argument=str((plan.get("shared_scope") or {}).get("statement") or ""),
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


def unit_payload(view: UnitWritingView, *, language: str = "zh") -> dict[str, Any]:
    """The compact payload the writing model receives (each source only once)."""

    return {
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
) -> list[dict[str, str]]:
    payload = build_completion_payload(view, existing_body, task_ids, language=language)
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
        if any(key in response for key in ("body_markdown", "status", "covered_task_ids", "issues")):
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


def _markdown_table_check(text: str) -> dict[str, Any]:
    rows = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for index in range(max(0, len(rows) - 2)):
        header = [cell.strip() for cell in rows[index].strip("|").split("|")]
        separator = [cell.strip() for cell in rows[index + 1].strip("|").split("|")]
        data = [cell.strip() for cell in rows[index + 2].strip("|").split("|")]
        if len(header) < 2 or len(separator) != len(header) or len(data) != len(header):
            continue
        if all(cell and re.fullmatch(r":?-{3,}:?", cell) for cell in separator) and all(data):
            return {
                "valid": True,
                "header_cells": len(header),
                "data_cells": len(data),
                "line": index + 1,
            }
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
) -> dict[str, Any]:
    """Make at most one explicit completion call and preserve a pending result."""

    from .module4.runtime import invoke_client

    messages = completion_messages(
        view, existing_body, task_ids, prompt=prompt, language=language,
        planning_revision=planning_revision)
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
        raw_target = target / f"{call_id}_{stamp}.raw"
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
        fragment = envelope.get("body_markdown")
        fragment = fragment if isinstance(fragment, str) else ""
        status = str(envelope.get("status") or "").strip() or "pending"
        covered = envelope.get("covered_task_ids")
        result["covered_task_ids"] = [
            str(item) for item in covered
        ] if isinstance(covered, Sequence) and not isinstance(covered, (str, bytes, bytearray)) else []
        raw_issues = envelope.get("issues")
        result["issues"] = list(raw_issues) if isinstance(raw_issues, list) else ([raw_issues] if raw_issues else [])
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
) -> list[dict[str, str]]:
    body = dict(payload) if payload is not None else unit_payload(view, language=language)
    planning_revision = bool(planning_revision or body.get("planning_revision_mode"))
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


def _strip_fences(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _decode_json_content(value: str) -> Mapping[str, Any] | None:
    """Decode a provider content string when it contains our JSON envelope."""

    text = _strip_fences(value)
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
    """Accept plain Markdown, ``{"body_markdown": ...}`` or a wrapped response."""

    if isinstance(response, Mapping):
        for key in ("body_markdown", "content", "markdown", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                if key == "content":
                    envelope = _decode_json_content(value)
                    if envelope is not None:
                        return parse_unit_body(envelope)
                stripped = _strip_fences(value)
                if stripped:
                    return stripped
        choices = response.get("choices")
        if isinstance(choices, Sequence) and choices:
            return parse_unit_body(choices[0])
        message = response.get("message")
        if isinstance(message, Mapping):
            return parse_unit_body(message)
        response = response.get("response") if isinstance(response.get("response"), Mapping) else response
        if isinstance(response, Mapping) and response.get("body_markdown"):
            return _strip_fences(str(response["body_markdown"]))
    if isinstance(response, str):
        text = _strip_fences(response)
        if text:
            envelope = _decode_json_content(text)
            if envelope is not None:
                return parse_unit_body(envelope)
            return text
    raise UnitWritingError("unit_body_empty_or_unreadable")


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
    for bracket in CITATION_RE.findall(body or ""):
        for handle in HANDLE_RE.findall(bracket):
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
) -> dict[str, Any]:
    """Store the model's Markdown and the program's honest bookkeeping."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    known = set(view.sources)
    used = citations_in(body)
    unknown = [handle for handle in used if handle not in known]
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
    if unknown:
        result["citation_problems"] = [
            {"code": "citation_not_in_unit_sources", "handle": handle,
             "note": "保留模型原文，不猜替换成别的来源"}
            for handle in unknown
        ]
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
) -> dict[str, Any]:
    """One writing call, with the raw response kept on disk."""

    from .module4.runtime import invoke_client

    messages = unit_messages(
        view, prompt=prompt, language=language, payload=payload,
        planning_revision=planning_revision,
    )
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
    body = parse_unit_body(response)
    issues = _response_issues(response)
    raw_path = ""
    if raw_response_dir is not None:
        directory = Path(raw_response_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        raw_path = str(directory / f"{view.chapter_id}_{view.unit_id}_{stamp}.raw")
        Path(raw_path).write_text(json.dumps(response, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    return {
        "body_markdown": body,
        "messages": messages,
        "usage": dict(response.get("usage") or {}),
        "finish_reason": response.get("finish_reason") or "",
        "complete": bool(response.get("complete", not partial_error)),
        "partial_error": partial_error,
        "model": response.get("model") or model,
        "raw_response": raw_path,
        "issues": issues,
    }
