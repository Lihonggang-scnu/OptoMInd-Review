"""Progressive, tool-connected planning for evidence-led review writing.

The planner reads the complete B pool before proposing a scope, runs planning
tools during both planning levels, and produces a coordinated outline plus
self-contained chapter packets. All collaborators are injectable so the full
workflow can be exercised without model or network calls.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .chapter_arrangement import (
    distinct_tool_material_sources,
    merge_tool_supplement_entry,
    tool_supplement_entry,
)


SCHEMA_VERSION = "optomind.progressive_review_plan.v1"
DEFAULT_PLANNER_MODEL = "qwen3.5-plus"
DEFAULT_READER_MODEL = "qwen3.7-flash"
DEFAULT_POOL_PATH = Path("outputs/planning_support/20260923/practical_refresh/supplement_live/PLANNING_POOL.jsonl")
DEFAULT_PLAN_PATH = Path("outputs/upgrade3/CROSSDOMAIN_REWORK/X1_microbiome_ICI/PLAN.json")
DEFAULT_BUDGET_LEDGER = Path("outputs/review_blueprint/20260922_phase1/budget.sqlite")
DEFAULT_TOKENIZER_PATH = Path("data/tokenizers/qwen3_5_9b/tokenizer.json")
MAX_INPUT_TOKENS = 991_808
TOKEN_MARGIN_MULTIPLIER = 1.12
TOKEN_FRAMING_MARGIN = 8_192
SOURCE_ROUTING_BATCH_SIZE = 60
SOURCE_ROUTING_WORKERS = 3
CASE_GROUP_ROUTE_BATCH_SIZE = 96

# This policy is owned by the local planner contract.  Model responses may
# describe editorial changes, but they cannot replace the citation policy used
# by writer packets and the final handoff.
CURRENT_CITATION_RULES = {
    "direct_source": "cite the supplied source_handle and bibliographic identity; the local packet maps it to paper_id",
    "review_reported_original": "keep the reporting review in source metadata; cite the resolved original study normally, with equal substantive use and no mandatory weaker wording or repeated review label",
    "scope": "preserve the actual reported findings and conditions; full-text access is not a prerequisite for using a sufficient abstract or expert review account",
}

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ProgressivePlanError(ValueError):
    """Invalid planner inputs, response, or resume state."""


@dataclass(frozen=True)
class ProgressivePlannerConfig:
    topic_id: str
    pool_path: Path
    plan_path: Path
    output_dir: Path
    shared_deep_read_budget: int = 40
    chapter_workers: int = 4
    reader_workers: int = 4
    planner_model: str = DEFAULT_PLANNER_MODEL
    chapter_model: str = DEFAULT_READER_MODEL
    reader_model: str = DEFAULT_READER_MODEL
    timeout_seconds: float = 900.0
    thinking_budget: int = 8_192
    planner_output_tokens: int = 18_000
    chapter_output_tokens: int = 14_000
    tokenizer_path: Path = DEFAULT_TOKENIZER_PATH
    # M1 is deliberately opt-in.  The legacy planner keeps its existing
    # chapter payload and call sequence when this is false.
    planning_revision_enabled: bool = False
    local_material_index_path: Path | None = None
    planning_revision_candidate_limit: int = 12
    planning_revision_passages_per_paper: int = 2
    planning_revision_passage_chars: int = 1200


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return str(value)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProgressivePlanError(f"json_unreadable:{path.name}") from exc


def _safe_id(value: Any, fallback: str = "item") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_-")
    return normalized[:80] or fallback


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _material_content_signature(value: Any) -> str:
    """Stable local signature for the material a downstream task consumes."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


_MATERIAL_CONTENT_FIELDS = (
    "study_summary_A", "review_planning_B", "deep_read_material",
    "local_passages", "supplement_gap_material", "supplement_gap_materials",
)


def _material_content(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only content a chapter role can use as evidence."""

    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in _MATERIAL_CONTENT_FIELDS if key in value and value[key] not in (None, "", [], {})}


def _refresh_local_material_snapshots(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reload card/tool snapshots after case work, without trusting its text.

    Case grouping can point at a source, but only the local card or explicitly
    saved tool material is authoritative.  The caller keeps the pre-case
    records separately so this refresh can be compared as a real content delta.
    """

    refreshed = json.loads(json.dumps(records, ensure_ascii=False, default=_json_default))
    for record in refreshed:
        for source in record.get("source_materials") or []:
            if not isinstance(source, dict):
                continue
            path = Path(str(source.get("card_path") or ""))
            if not path.is_file():
                continue
            try:
                card = _read_json(path)
            except ProgressivePlanError:
                continue
            if not isinstance(card, Mapping):
                continue
            a = card.get("general_understanding")
            b = card.get("review_planning")
            if isinstance(a, Mapping) and a:
                source["study_summary_A"] = dict(a)
            if isinstance(b, Mapping) and b:
                source["review_planning_B"] = dict(b)
    return refreshed


def load_original_plan(path: str | Path) -> dict[str, Any]:
    raw = _read_json(Path(path))
    if not isinstance(raw, Mapping):
        raise ProgressivePlanError("plan_must_be_object")
    if isinstance(raw.get("plan"), Mapping):
        if str(raw.get("status") or "").casefold() != "ok":
            raise ProgressivePlanError("plan_wrapper_status_not_ok")
        plan = dict(raw["plan"])
    else:
        plan = dict(raw)
    if not _text(plan.get("question_en") or plan.get("question")):
        raise ProgressivePlanError("plan_question_missing")
    if not isinstance(plan.get("facets"), list) or not plan["facets"]:
        raise ProgressivePlanError("plan_facets_missing")
    return plan


def _canonical_paper_id(row: Mapping[str, Any]) -> str:
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return _text(identity.get("canonical_paper_id") or row.get("paper_id") or row.get("canonical_paper_id"))


def _compact_b_record(row: Mapping[str, Any]) -> dict[str, Any]:
    planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return {
        "source_handle": _text(row.get("_source_handle")),
        "title": _text(identity.get("title") or row.get("title")),
        "doi": _text(identity.get("doi") or row.get("doi")),
        "year": _text(identity.get("year") or row.get("year")),
        "material_scope": _text(planning.get("material_scope") or row.get("material_scope")),
        "declared_content_depth": _text(planning.get("declared_content_depth") or row.get("material_depth_label")),
        "planning_summary": planning.get("planning_summary") or "",
        "facet_contributions": planning.get("facet_contributions") or [],
        "scope_interpretation_cautions": planning.get("scope_interpretation_cautions") or [],
        "broader_review_uses": planning.get("broader_review_uses") or [],
        "root_review_note": dict(row.get("root_review_note") or {}),
        "supplement_material_status": _text(row.get("supplement_material_status") or row.get("supplement_status")),
        "supplement_gap_material": row.get("supplement_gap_material") or {},
    }


def load_planning_pool(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with source.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProgressivePlanError(f"pool_jsonl_invalid:{line_number}") from exc
                if not isinstance(raw, Mapping):
                    raise ProgressivePlanError(f"pool_row_not_object:{line_number}")
                row = dict(raw)
                paper_id = _canonical_paper_id(row)
                if not paper_id:
                    raise ProgressivePlanError(f"pool_paper_id_missing:{line_number}")
                if paper_id in seen:
                    raise ProgressivePlanError(f"pool_duplicate_paper_id:{paper_id}")
                seen.add(paper_id)
                row["_paper_id"] = paper_id
                row["_source_handle"] = f"P{len(rows) + 1:04d}"
                row["_b_summary"] = _compact_b_record(row)
                rows.append(row)
    except OSError as exc:
        raise ProgressivePlanError("pool_unreadable") from exc
    if not rows:
        raise ProgressivePlanError("pool_empty")
    return rows


def _refresh_source_handles(rows: list[dict[str, Any]]) -> dict[str, str]:
    handle_to_id: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        paper_id = _canonical_paper_id(row)
        if not paper_id:
            continue
        row["_paper_id"] = paper_id
        handle = _text(row.get("_source_handle")) or f"P{index:04d}"
        row["_source_handle"] = handle
        row["_b_summary"] = _compact_b_record(row)
        handle_to_id[handle] = paper_id
    return handle_to_id


def _resolve_source_handle(value: Any, handle_to_id: Mapping[str, str]) -> str:
    if isinstance(value, Mapping):
        value = value.get("source_handle") or value.get("paper_id") or value.get("canonical_paper_id")
    normalized = _text(value)
    return str(handle_to_id.get(normalized, normalized))


def _source_values(value: Any) -> list[str]:
    if isinstance(value, (str, Mapping)):
        value = [value]
    return list(dict.fromkeys(handle for item in (value or []) if (handle := _resolve_source_handle(item, {}))))


def _resolve_planner_handles(value: Mapping[str, Any], handle_to_id: Mapping[str, str]) -> dict[str, Any]:
    """Translate short prompt handles back to canonical IDs at the local edge."""

    result = json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    for key in ("chapter_proposals", "chapters", "harmonized_chapters"):
        for row in result.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            source_values = _source_values(row.get("source_handles") or row.get("source_ids") or row.get("paper_ids"))
            row["source_ids"] = list(dict.fromkeys(_resolve_source_handle(item, handle_to_id) for item in source_values if _text(item)))
            excluded = row.get("excluded_source_handles") or row.get("excluded_source_ids") or []
            if isinstance(excluded, str):
                excluded = [excluded]
            if excluded:
                if any(isinstance(item, Mapping) for item in excluded):
                    row["source_exclusion_notes"] = excluded
                row["excluded_source_handles"] = _source_values(excluded)
                row["excluded_source_ids"] = list(dict.fromkeys(_resolve_source_handle(item, handle_to_id) for item in excluded if _text(item)))
            if row.get("directed_reads") or row.get("supplement_requests"):
                row.update(_resolve_planner_handles(row, handle_to_id))
    for key in ("directed_reads",):
        for row in result.get(key) or []:
            if isinstance(row, Mapping):
                raw_id = row.get("source_handle") or row.get("paper_id") or row.get("canonical_paper_id")
                row["paper_id"] = _resolve_source_handle(raw_id, handle_to_id)
    for row in result.get("supplement_requests") or []:
        if not isinstance(row, Mapping):
            continue
        row["known_papers"] = [dict(item) if isinstance(item, Mapping) else {"paper_id": _resolve_source_handle(item, handle_to_id)}
                               for item in row.get("known_papers") or []]
        for candidate in row.get("known_papers") or []:
            if isinstance(candidate, Mapping):
                raw_id = candidate.get("source_handle") or candidate.get("paper_id") or candidate.get("canonical_paper_id")
                candidate["paper_id"] = _resolve_source_handle(raw_id, handle_to_id)
    return result


def _planner_scope_copy(value: Mapping[str, Any], paper_to_handle: Mapping[str, str]) -> dict[str, Any]:
    """Send editorial content; full route inventories stay in the local pool."""
    result = json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    for key in ("chapter_proposals", "chapters", "harmonized_chapters"):
        for row in result.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            ids = row.get("source_ids") or row.get("paper_ids") or []
            handles = row.get("source_handles") or [paper_to_handle.get(_text(item), _text(item)) for item in ids]
            row["available_source_count"] = len(set(_source_values(handles)))
            row.pop("source_handles", None)
            excluded = row.get("excluded_source_ids") or row.get("excluded_source_handles") or []
            if isinstance(excluded, str):
                excluded = [excluded]
            if excluded:
                row["excluded_source_handles"] = list(dict.fromkeys(paper_to_handle.get(_text(item), _text(item)) for item in excluded if _text(item)))
            row.pop("source_ids", None)
            row.pop("paper_ids", None)
            row.pop("excluded_source_ids", None)
    return result


def _outline_chapter_rows(outline: Any) -> list[dict[str, Any]]:
    """Read ordinary or mildly nested ordered outline shapes."""
    if isinstance(outline, Mapping):
        candidates = outline.get("chapters") or outline.get("sections") or []
        if not candidates and isinstance(outline.get("shared_outline"), (Mapping, list)):
            return _outline_chapter_rows(outline.get("shared_outline"))
    elif isinstance(outline, list):
        candidates = outline
    else:
        candidates = []
    output: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("chapter_proposals"), Mapping):
            nested = dict(item["chapter_proposals"])
            nested.setdefault("chapter_id", item.get("chapter_id") or item.get("id"))
            output.append(nested)
        elif isinstance(item.get("chapter_proposals"), list):
            output.extend(dict(row) for row in item["chapter_proposals"] if isinstance(row, Mapping))
        else:
            output.append(dict(item))
    return output


def _compact_routing_outline(outline: Any) -> list[dict[str, str]]:
    return [
        {
            "chapter_id": _text(row.get("chapter_id") or row.get("id")),
            "title": _text(row.get("title") or row.get("chapter_title")),
            "focus": _text(row.get("focus") or row.get("purpose") or row.get("scope") or row.get("question")),
        }
        for row in _outline_chapter_rows(outline)
        if _text(row.get("chapter_id") or row.get("id"))
    ]


def _normalize_proposal_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Accept the expected chapter list and the observed nested outline wrapper."""
    output = json.loads(json.dumps(response, ensure_ascii=False, default=_json_default))
    rows = output.get("chapter_proposals")
    if isinstance(rows, Mapping):
        rows = [dict(rows)]
    if not isinstance(rows, list):
        rows = []
    if not rows:
        outline = output.get("shared_outline")
        rows = _outline_chapter_rows(outline)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        nested = row.get("chapter_proposals")
        if isinstance(nested, Mapping):
            item = {**dict(nested), **{key: value for key, value in row.items() if key != "chapter_proposals"}}
        else:
            item = dict(row)
        nested_rows = item.get("proposals")
        if nested_rows and not _text(item.get("chapter_id")):
            normalized.extend(dict(child) for child in nested_rows if isinstance(child, Mapping))
        else:
            normalized.append(item)
    output["chapter_proposals"] = normalized
    return output


def _merge_supplement_pool_updates(pool_rows: list[dict[str, Any]], tool_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Add supplement discoveries and merge their upgraded planning material into B."""

    by_id = {str(row.get("_paper_id")): row for row in pool_rows}
    changed = False
    groups: list[Mapping[str, Any]] = []
    pending: list[Any] = list(tool_result.get("supplement_results") or [])
    pending.extend(tool_result.get("results") or [])
    while pending:
        group = pending.pop(0)
        if not isinstance(group, Mapping):
            continue
        groups.append(group)
        pending.extend(group.get("results") or [])
    candidate_rows: list[Mapping[str, Any]] = []
    for group in groups:
        candidate_rows.extend(row for row in (group.get("candidate_rows") or []) if isinstance(row, Mapping))
        derived_path = Path(str(group.get("derived_pool_path") or ""))
        if derived_path.is_file():
            try:
                with derived_path.open("r", encoding="utf-8-sig") as handle:
                    candidate_rows.extend(row for line in handle if line.strip() for row in [json.loads(line)] if isinstance(row, Mapping))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
    for raw in candidate_rows:
        row = dict(raw)
        paper_id = _canonical_paper_id(row)
        if not paper_id:
            continue
        if paper_id in by_id:
            current = by_id[paper_id]
            incoming_unit = _text(row.get("supplement_source_unit_id"))
            current_unit = _text(current.get("supplement_source_unit_id"))
            incoming_version = int(row.get("supplement_version") or 0)
            current_version = int(current.get("supplement_version") or 0)
            # Each supplement's derived pool also contains unchanged base rows.
            # Do not let a later gap's copy of the base card replace an upgraded
            # card that was returned by an earlier gap.
            incoming_is_upgrade = bool(
                (incoming_unit and (not current_unit or incoming_version > current_version))
                or (isinstance(row.get("supplement_gap_material"), Mapping) and row.get("supplement_gap_material") and not current_unit)
            )
            if incoming_is_upgrade:
                for key in ("card_path", "planning_view", "material_depth_label", "supplement_version", "prior_card_path", "prior_source_unit_ids", "prior_supplement_gap_materials"):
                    if key in row:
                        current[key] = row[key]
                if incoming_unit:
                    current["supplement_source_unit_id"] = incoming_unit
            material = row.get("supplement_gap_material")
            if isinstance(material, Mapping) and material:
                prior = current.get("supplement_gap_material")
                materials = list(current.get("supplement_gap_materials") or [])
                if isinstance(prior, Mapping) and prior and prior.get("gap_id") != material.get("gap_id"):
                    if not any(isinstance(item, Mapping) and item.get("gap_id") == prior.get("gap_id") for item in materials):
                        materials.append(dict(prior))
                if not any(isinstance(item, Mapping) and item.get("gap_id") == material.get("gap_id") for item in materials):
                    materials.append(dict(material))
                current["supplement_gap_material"] = dict(material)
                current["supplement_gap_materials"] = materials
                current["supplement_material_status"] = row.get("supplement_material_status") or row.get("supplement_status") or "ready"
            if not current_unit and incoming_unit:
                current["supplement_source_unit_id"] = incoming_unit
        else:
            row["_paper_id"] = paper_id
            row["_source_handle"] = ""
            row["_b_summary"] = {}
            pool_rows.append(row)
            by_id[paper_id] = row
        changed = True
    if changed:
        _refresh_source_handles(pool_rows)
    return pool_rows


def qwen_local_token_counter(tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH) -> Callable[[bytes, Sequence[Mapping[str, Any]]], int]:
    """Return a local Qwen-family tokenizer counter without downloading assets."""

    path = Path(tokenizer_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise ProgressivePlanError(f"local_tokenizer_missing:{path}")
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise ProgressivePlanError("tokenizers_package_missing") from exc

    local = threading.local()

    def count(_request_bytes: bytes, messages: Sequence[Mapping[str, Any]]) -> int:
        tokenizer = getattr(local, "tokenizer", None)
        if tokenizer is None:
            tokenizer = Tokenizer.from_file(str(path))
            local.tokenizer = tokenizer
        text = "\n".join(
            f"{_text(message.get('role'))}\n{_text(message.get('content'))}"
            for message in messages
            if isinstance(message, Mapping)
        )
        return len(tokenizer.encode(text).ids)

    return count


def _planner_instructions(stage: str, *, planning_revision: bool = False) -> str:
    common = (
        "你是学术综述规划编辑。目标是形成有论证主线的长篇综述，规划内容用中文撰写，论文原题和专名可保留原文；不要写成问答清单。"
        "章节组织和比较尺度由研究问题、学科语境与现有材料决定，不预设任何单一研究范式为所有主题的共同模板；理论、概念、方法论和设计研究也应按其实际论证结构组织。"
        "以用户研究问题为中心，提出贯穿全文的组织视角和综合判断，再让不同来源承担解释、比较、例证、背景或发展等职责；不要按论文逐篇复述，也不要让某个局部缺口取代全篇主线。综合判断必须由材料中的联系、差异或演进支撑。"
        "根据研究问题和材料本身判断相关性；材料中的推荐语可能夸大用途，应以来源实际的研究对象或材料、研究设置、方法、比较、结果或论证、验证方式和边界为准。"
        "保留每个来源真实的研究类型、对象、设置、条件、方法、结果和限制。综述的综合、假设或方案不是已观察到的结果。"
        "相关时，综述中报告的原始研究与直接来源享有同等实质使用权。保留报告综述的来源元数据；原始研究身份已解析时，写作者可以直接引用原始研究，不因材料来自专家综述就要求逐句降格或反复标注‘综述转述’，也不要声称读过原始全文或重新获取它。"
        "保留每个报告综述自己的 R# 命名空间；有 source_handle 就使用它，否则原样保留 supplied paper_id 或 reference。短 handle 到规范身份的映射由本地程序完成。证据足够时使用广泛材料，但不要为了凑数量硬塞来源。"
        "区分关联与因果证据、机制或解释、迁移或应用、验证和不确定性；不要把相关关系、作者提出的解释或同一组材料内的支持误写成因果机制、跨设置迁移或独立验证。"
        "有限补充检索没有找到匹配研究，不等于该研究不存在，也不等于整个领域缺乏相关研究或不存在更优路线；未被满足的需求只约束本次可写的结论范围，不得升级为领域层面的缺失判断，也不应让同一缺口反复主导多个章节。只描述现有材料支持的范围，并纠正前面规划中的过度表述。路由摘要与原始 interpretation_limits 冲突时，以原始限制为准。综述题名中的对象或应用也不自动成为其中每项研究的对象和设置。"
        "研究条件必须进入 substantive_point、thesis、synthesis 和 transition 本身；决定含义的研究对象与研究设置随主张、案例和展开关系一起陈述，而不是全部堆进独立的 limitation 字段；不能靠单独的 limitation 字段修补前文过度断言，也不要求每句都填条件表或给所有判断统一加“可能”。对不同对象、设置或比较尺度的材料，保留其不可直接等同之处，不把它们排成脱离条件的效果排名。"
        "章节标题也用中文。Return JSON only."
    )
    stage_specific = {
        "provisional_scope": (
            "Read every item in candidate_pool. It is the complete B pool, not a sample. Screen all items semantically and "
            "build a source-informed theme inventory from the evidence summaries before drafting the provisional scope. "
            "材料充分时追求充分覆盖：背景、具体案例、比较与研究发展材料都得到安排（材料池规模见 pool_row_count）；这是覆盖质量目标，不是计数门槛，绝不为数量塞入无关论文，也不因池小而放弃覆盖。 "
            "Propose a provisional shared scope, central thesis, chapter outline, cross-chapter boundaries, and concise tool "
            "requests. Do not rely on a small prefix or require a per-paper rejection explanation; grouped non-use reasons are fine. "
            "Return keys: review_title, central_question, provisional_scope, material_theme_inventory, provisional_outline "
            "(chapters with chapter_id/title/purpose/question), cross_chapter_rules, grouped_screening_notes, supplement_requests, "
            "directed_reads. Every source request must name source_handle; gaps use gap_id. supplement_requests entries need gap_id, gap_question, "
            "success_criteria, chapter_ids, intended_use (background/mechanism/comparison/quantification/application_outcome/study_design), targeted_queries, optional reuse_plan_facet_ids, known_papers, reviewed_references. targeted_queries are 1-2 compact English keyword queries (object + specific relation) or self-contained English semantic statements, never generic questions without the domain object. "
            "directed_reads entries need paper_id, chapter_ids, questions, required_outputs, knowledge_gap, and a reason."
        ),
        "level1_outline": (
            "Finalize the first shared outline using the provisional scope and the actual level-1 supplement and directed "
            "reading results. Explain how successful, partial, failed, or unavailable tool outputs changed chapter scope. "
            "Adapt where evidence is missing; do not request the same missing item repeatedly. Return shared_scope, "
            "shared_outline (ordered chapters), review_argument, source_selection_principles, and unresolved_limits."
        ),
        "source_routing": (
            "本次 candidate_batch 是完整 B 池按顺序分批后的一个批次，不是抽样。逐篇阅读本批次每一篇的 B 规划卡，"
            "将每篇都路由到 L1 共享提纲中适合的章节。判断时检查论文实际研究类型、对象、条件、材料深度与范围解释提醒；"
            "不要只依据宽泛或夸大的用途推荐语。A 卡留给后续章节细化。一个来源可进入多个章节；确实不相关时 chapter_ids 为空并简短说明。"
            "每篇必须返回 source_handle、paper_title、chapter_ids、specific_usable_material。specific_usable_material 用中文写约"
            "80–150 字，指出具体可用事实或比较角度及其在本综述中的用途；不相关来源只需简短解释。不得只挑少数种子文献，"
            "也不得为了覆盖率强行安排无关材料。返回包含 source_routes 数组的 JSON 对象，每个输入 source_handle 恰好出现一次，不增造编号。"
        ),
        "chapter_proposals": (
            "当前调用只负责 chapter 指定的一章；shared_level1_outline 用来理解全篇分工。只返回该章的提案。"
            "source_routing 已由程序完整保留，不要重复抄写全量 source_handles；只在具体案例、比较或排除项中引用必要编号。"
            "把输出集中于论述结构、研究差异、衔接和有价值的缺口。"
            "Use the complete source_routing ledger, which covers every source in the full B pool. Do not discard routed sources "
            "merely because they were not deep-read; the route ledger contains the source-specific A/B facts needed for broad planning. "
            "The L1 outline is an initial direction, not a binding template: correct its thesis, scope, chapter titles or emphasis where "
            "the full routed evidence shows an overclaim, missing context, or better organizing logic, while retaining chapter_id values "
            "where possible so tool requests remain attached. Preserve the broad set of useful source handles assigned by routing; do not "
            "collapse chapters to a few deep-read seed papers. When material supports it, plan a full-length review with broad coverage "
            "across background, concrete cases, comparisons, and developments (the pool size is in candidate_pool_row_count); this is a "
            "coverage aspiration, never a count gate or reason to include irrelevant sources, nor a reason to shrink to a few core papers. "
            "Draft a substantive proposal for each "
            "chapter, including relevant source handles, chapter boundaries, ordered themes, concrete cases/comparisons, "
            "cross-paper synthesis, conditions and limitations, transition logic, and any genuinely necessary tool needs. "
            "Do not silently drop a routed source: when an individual source is genuinely out of scope, name it in "
            "excluded_source_handles with a concise reason; otherwise retain it as writer-available material. "
            "Return chapter_proposals (each with chapter_id, title, purpose, scope, optional excluded_source_handles, substantive_threads, "
            "supplement_requests, directed_reads) and screened_sources with non-use reasons. A source may support multiple "
            "chapters when its evidence actually does so."
        ),
        "harmonize_scope": (
            "论文的完整章节分配由本地程序继承，不要在输出中重抄各章 source_handles/source_ids 清单；"
            "只输出必要的具体研究引用、明确排除项和结构调整。"
            "Act as the central review editor. Reconcile chapter proposals before detailed expansion. Set one shared scope, "
            "ordered outline, chapter boundaries, concepts, terminology, and transitions. Remove overlap and resolve "
            "contradictions between proposals while preserving their broad useful source assignments; do not replace routed literature "
            "with only deep-read seeds. Remove a routed source only when it is explicitly named in excluded_source_handles with a reason; "
            "otherwise inherit it into the coordinated chapter source list. Merge and deduplicate supplement gaps and directed-read tasks by source_handle "
            "while preserving all distinct questions and chapter_ids. Preserve valuable level-1 tool results and do not "
            "reacquire review-reported original full texts. Return shared_outline, chapters (with chapter_id and coordinated "
            "scope), supplement_requests, directed_reads, and harmonization_notes."
        ),
        "finalize_chapter_scope": (
            "本地程序保留此前的论文分配，输出不必重复完整编号清单；集中改好结构与论述任务。"
            "这一阶段只确定每章的范围与主要论述线索，每章约300–600字；具体段落、案例组与大量引用留给下一步章节细化。"
            "Finalize the coordinated chapter scopes after reviewing actual level-2 supplement and directed-reading returns, "
            "including failures, partial results, newly added sources, and upgraded planning material. Update source assignments "
            "and unit emphasis where evidence warrants. Keep the already harmonized boundaries and shared transitions coherent. "
            "Retain routed assignments unless explicitly naming a source_handle in excluded_source_handles with a concise reason. "
            "Do not issue new tool requests or repeat missing-material demands. Return shared_outline, chapters (chapter_id, "
            "title, purpose, scope, substantive_threads), and final_scope_notes."
        ),
        "chapter_details": (
            "Prepare this chapter's writer-ready plan from the coordinated shared scope and supplied A/B/deep materials. "
            "The chapter object is your only writing assignment. The shared_outline is a map of OTHER chapters as well, "
            "not a list to reproduce as this chapter's units. Keep this chapter within its purpose and scope; briefly signpost "
            "topics owned by other chapters instead of developing them again. An introduction establishes context, the review's "
            "problem and organizing perspective; it must not become a miniature full review. Treat the outline as an editorial "
            "assignment, not a factual source: correct factual or terminology mistakes using the supplied A/B and reading material. "
            "Develop the chapter across the supplied collection, not only a few deeply read examples. Organize useful studies "
            "into concrete case and comparison groups attached to the actual content units; identify what each group contributes. "
            "The writer should receive enough selected cases, contrasts and background sources for a substantial review chapter. "
            "Do not do new research. Return a chapter_plan with a clear thesis, reader objective, ordered substantive units, "
            "specific cases and paper references, cross-paper synthesis, conditions and limitations, and transitions. Every "
            "unit must state (1) its substantive point, (2) the ordered development, (3) concrete studies or cases with "
            "source_handle only (the local program supplies bibliographic identity) together with the research object and "
            "setting that determine what the finding means, (4) what findings agree or conflict and why, (5) evidence "
            "conditions and limits kept with the claims they qualify rather than parked in a separate list, and (6) its "
            "transition. Within each substantial unit, provide paragraph_briefs: an ordered list of "
            "paragraph tasks, each with its specific point, development (the actual comparison or reasoning), and source_handles. "
            "A unit may span several distinct paragraphs; do not reduce a rich theme to 'introduce, discuss, summarize'. "
            "Let useful distinctions in the material determine the number of paragraphs, not a fixed quota. "
            "Keep direct-source references distinct from review-reported references."
        ),
        "chapter_need_analysis": (
            "Inspect each finalized chapter scope for concrete unresolved evidence needs that would materially improve its writer plan. "
            "Use the supplied source-specific material, not source identifiers alone, to decide what is actually missing. Return only high-value, chapter-owned supplement_requests and directed_reads; keep each question narrow and attach chapter_ids. Each supplement needs gap_id, gap_question, success_criteria, intended_use (background/mechanism/comparison/quantification/application_outcome/study_design) and 1-2 targeted_queries (query_type keyword or question, query_text in English). State the domain object and specific relation in every query. Prioritize the few gaps that change the argument or supply a missing comparison; avoid demands for every desirable detail. "
            "Do not repeat a settled level-1 or level-2 request, and return empty arrays when the supplied material is sufficient."
        ),
        "whole_plan_improvement": (
            "Make one light whole-plan improvement pass. Identify only high-value changes to flow, duplication, scope "
            "consistency, terminology, and transitions. Coordinate by actual content: for each important concept, "
            "mechanism or method, say which chapter is its primary place of explanation and what a re-appearance in "
            "another chapter adds for the reader. Judge duplication by explanatory role, not by sentence similarity or "
            "repeated citations: the same paper may legitimately support several chapters with different uses, so do not "
            "remove or reassign content merely because a source or a phrasing recurs. A locally unmet retrieval need only "
            "bounds what the affected chapter can currently conclude; it must not become a field-level absence claim in "
            "any chapter. Do not replace chapter evidence, invent citations, demand new "
            "research, or reopen settled tool gaps. Return concise improvement_notes, cross_chapter_adjustments, "
            "updated_chapter_plans only for small scalar fields such as thesis, title, or reader objective, and small "
            "shared_outline_adjustments. Do not return full chapter plans or units: if unit structure needs revision, "
            "return a concise chapter-level feedback item naming the chapter and the reason, so a separate full chapter "
            "revision can use the complete evidence. Identify every edit by chapter_id and describe the actual replacement "
            "or development needed; the chapter reviser will implement it. When shared guidance changes, optionally return complete finalized_shared_scope and "
            "finalized_harmonization_notes replacements. Do not return or invent citation policy; the program's current "
            "citation_rules remain authoritative."
        ),
        "affected_chapter_revision": (
            "Turn the supplied whole-plan improvement notes into concrete writer-plan edits for the named chapters. "
            "Use the supplied source_materials (the chapter's cited A/B evidence) and related tool_materials to correct "
            "factual, study-type, citation, condition, or limitation mistakes. Treat chapter_feedback as an editorial "
            "request, not as evidence; do not repeat an unsupported claim from it. Return a complete replacement plan "
            "including every existing substantive unit, even when only one unit needs correction. "
            "For broad units that still leave the writer to invent the argument, add ordered paragraph_briefs with point, "
            "development and source_handles. Use the supplied studies to give a concrete comparison, mechanism or example "
            "for each paragraph task, rather than just listing topics. Existing material can support a richer explanation "
            "without new research. Do not let one unanswered retrieval need dominate the review's whole intellectual framing. "
            "Preserve each chapter's evidence, source handles, conditions and limitations, and change only the requested "
            "flow, duplication, scope, terminology, or transition issue. Return chapter_updates, with one item per target "
            "containing chapter_id and a complete updated_plan. Do not add sources, invent findings, or return notes without "
            "an updated_plan."
        ),
        "case_groups": (
            "为已经协调好的写作单元挑选可用文献案例。source_materials 提供本批每篇候选来源的实际材料"
            "（A 概括、B 综述规划，以及已有的精读、补充或本地片段）；阅读全部 source_routing 与 source_materials，"
            "再对照 unit_catalog，挑选材料确实支持本单元的对象、结果、条件或有用对照。"
            "整篇综述的广度目标是主题、背景、发展与代表案例得到充分覆盖（当前材料池规模见 pool_sources，已有案例计入覆盖）；"
            "这是覆盖目标不是数量门槛：不重复添加已有来源，不为数量塞入无关文献，也不把“只用少数核心论文”当统一规则。"
            "只为具体单元补材料，不改章节结构。"
            "对照已有 cases/supporting_studies 的具体贡献，新论文需补充不同结果、条件、方法、发展阶段或有用对照；仅重复相同概括时留在备选池，additions可为空。"
            "每篇写约30–60字说明这篇材料能帮助本单元解释什么，可以提出拟议综合；"
            "但不得改写或虚构论文的方法、结果与结论——具体案例由章节负责人对照材料确认。"
            "推荐用途不能把类比、方案或其他对象的结果说成本问题的直接实证；material_available 为假的来源没有实际内容，不得凭编号编造用途。"
            "当前调用只负责一个 chapter_id；完整 source_routing 可能已按章节截取，不能据此虚构遗漏来源。"
            "返回 JSON 对象：additions 数组，每项 unit_key、studies 数组（source_handle、contribution）。"
        ),
    }
    stage_text = stage_specific[stage]
    if planning_revision and stage == "case_groups":
        stage_text = stage_text.replace(
            "studies 数组（source_handle、contribution）",
            "studies 数组（source_handle、proposed_use；兼容 contribution 字段名）",
        )
        stage_text = stage_text.replace(
            "每篇写约30–60字说明这篇材料能帮助本单元解释什么，可以提出拟议综合；",
            "每篇写 proposed_use：约30–60字说明这篇材料能帮助本单元解释什么（拟议用途），可以提出拟议综合；",
        )
    if planning_revision:
        stage_text = stage_text.replace("跨研究比较和衔接", "有材料依据的论证关系和衔接")
        stage_text = stage_text.replace("what findings agree or conflict and why", "material-supported relations, conditions or limits")
        stage_text = stage_text.replace("finding or argument, comparison, mechanism or example", "finding or argument, mechanism, concept, development or example")
        stage_text = stage_text.replace("a concrete comparison, mechanism or example", "a concrete argument, mechanism, concept or example")
        stage_text = stage_text.replace(
            "Preserve each chapter's evidence, source handles, conditions and limitations, and change only the requested "
            "flow, duplication, scope, terminology, or transition issue.",
            "Preserve each chapter's evidence, source handles, conditions and limitations; where the supplied material shows "
            "that the thesis, scope, terminology, judgment, or unit structure needs correction, implement that owner-level update "
            "instead of treating a short scalar replacement as authoritative.",
        )
        role_directives = {
            "chapter_details": (
                "章节负责人在掌握 A/B/精读材料后建立有材料依据的科学认识、thesis、reader_objective 和具体展开关系。"
                "决定含义的研究对象与研究设置随主张、案例和展开关系一起陈述；limitations 只收尚未随判断说明的剩余边界，"
                "不把条件全堆进去，不逐句填条件表，也不给所有判断统一加“可能”。"
                "工具未回答的需求只约束当前可写结论，不得写成领域缺失或不存在更优路线，也不让同一缺口主导本章结构。"
                "不要求每个单元都比较研究结果，也不要求每次比较都解释 why；材料若只支持概念关系、方法前提、机制、发展、背景、"
                "例证或边界，就按该实际功能组织。只有材料确有可比对象和依据时才比较，并明确比较尺度；没有依据时不得补造差异原因。"
                "相互独立的维度可以并存；除非材料明确支持，不把它们写成互斥且穷尽的二分路径。"
            ),
            "affected_chapter_revision": (
                "章节负责人可以依据 supplied source_materials 和 late material 修正原有 thesis、reader_objective、判断和单位结构；"
                "这些更新必须同时保留原判断与新增材料的边界，不把编辑意见或单一标签当作证据。按材料实际功能组织，不强制比较或解释差异原因。"
                "决定含义的对象与设置随修正后的主张一起保留；未被满足的需求仍只约束可写结论，不升级为领域判断。"
                "相互独立的维度可以并存；除非材料明确支持，不把它们写成互斥且穷尽的二分路径。"
                "case_suggestions 是选材层的拟议用途，不是论文已有结论：对照该来源的实际材料，把成立的建议展开成"
                "具体案例（对象、设置、结果、条件）写进相应单元的 supporting_studies，或明确不用；"
                "材料不含的方法、结果或结论不得写入，material_available 为假的建议保持为待选，不采纳其内容。"
            ),
            "whole_plan_improvement": (
                "全局协调只检查范围、章节分工、衔接和材料影响；对重要概念、机制或方法写明主讲章与再现章各自增加的解释，"
                "按解释职责判断重复——允许同一论文跨章按不同用途复用，不按句子相似或引用重复删内容。"
                "发现实质 thesis/判断变化时输出具体 chapter feedback 交章节负责人落实，协调结论要落到必要章节的更新，不是只列一张建议表；"
                "不要用短 scalar 或 chapter_argument 直接替代章节科学认识。跨章调整保持有界。"
            ),
            "case_groups": (
                "案例扩展只做选材：阅读所附 source_materials 的实际材料，返回来源指针、单元归属和拟议用途（proposed_use）；"
                "案例添加者不能创作或覆盖 A/B、精读或事实内容，最终案例由章节负责人对照材料形成。"
            ),
        }
        directive = role_directives.get(stage, (
            "本模式只在显式 opt-in 时启用。章节负责人建立有材料依据的认识；其他角色只能执行其职责并回传具体问题。"
        ))
        stage_text += "\n\n【章节论证模式】" + directive
    return common + "\n\n" + stage_text


def _compact_reading_material(raw: Any) -> Any:
    """Keep extracted content once and only the bibliography it actually uses."""
    if not isinstance(raw, Mapping):
        return raw
    result = dict(raw)
    questions = result.get("question_material")
    if questions:
        result.pop("content", None)
        result.pop("open_questions", None)
    used: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "reference_ids" and isinstance(item, list):
                    used.update(str(ref) for ref in item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(questions or result.get("content") or {})
    if isinstance(result.get("references"), list):
        result["references"] = [ref for ref in result["references"] if isinstance(ref, Mapping) and str(ref.get("reference_id")) in used]
    return result


def _candidate_card_material(candidate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return the saved A, B and identity-facing material for one candidate.

    Candidate navigation is allowed to expose a source that was not selected by
    the current chapter.  Reading the existing card here keeps that operation
    local and preserves the same A/B distinction used by the regular chapter
    payload.
    """

    card = _card_for_candidate(candidate)
    a = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
    b = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
    planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
    identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
    return dict(a), dict(b), {
        "title": _text(identity.get("title") or candidate.get("title")),
        "doi": _text(identity.get("doi") or candidate.get("doi")),
        "year": _text(identity.get("year") or candidate.get("year")),
        "material_depth": _text(
            (candidate.get("_b_summary") or {}).get("declared_content_depth")
            if isinstance(candidate.get("_b_summary"), Mapping)
            else planning.get("declared_content_depth")
        ),
    }


def build_local_material_payload(
    candidate: Mapping[str, Any],
    *,
    index: Any | None = None,
    index_path: str | Path | None = None,
    question: str = "",
    hits: Sequence[Any] = (),
    deep_material: Mapping[str, Any] | None = None,
    passages_per_paper: int = 2,
    passage_chars: int = 1200,
) -> dict[str, Any]:
    """Build a bounded, local-only material record for a candidate source.

    The record deliberately contains the saved A/B card and any focused local
    passages supplied by ``PlanningMaterialIndex``.  A caller may also pass a
    previously saved directed-reading result as ``deep_material``.  This is an
    input-construction helper: it never contacts a provider and never upgrades
    an abstract/card into full text.
    """

    a, b, identity = _candidate_card_material(candidate)
    paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
    source_handle = _text(candidate.get("_source_handle"))
    material: dict[str, Any] = {
        "source_handle": source_handle,
        "paper_id": paper_id,
        "title": identity["title"],
        "doi": identity["doi"],
        "year": identity["year"],
        "material_depth": identity["material_depth"],
        "study_summary_A": a,
        "review_planning_B": b,
        "external_calls": 0,
    }
    # Supplement results are persisted on pool rows under the gap-specific
    # names. Keep them in candidate fallback payloads so a selected source
    # does not lose the only substantive material it has.
    if candidate.get("supplement_gap_material"):
        material["supplement_gap_material"] = candidate.get("supplement_gap_material")
        material["supplement_material"] = candidate.get("supplement_gap_material")
    if candidate.get("supplement_gap_materials"):
        material["supplement_gap_materials"] = candidate.get("supplement_gap_materials")
        material["supplement_materials"] = candidate.get("supplement_gap_materials")
    if deep_material:
        material["deep_read_material"] = _compact_reading_material(deep_material)

    bounded_hits: list[dict[str, Any]] = []
    for raw in list(hits)[: max(0, int(passages_per_paper))]:
        if hasattr(raw, "to_dict") and callable(raw.to_dict):
            row = dict(raw.to_dict())
        elif isinstance(raw, Mapping):
            row = dict(raw)
        else:
            continue
        if _text(row.get("paper_id")) and _text(row.get("paper_id")) != paper_id:
            continue
        text = _text(row.get("text"))
        if passage_chars > 0 and len(text) > passage_chars:
            text = text[:passage_chars].rstrip() + "…"
        row["text"] = text
        if isinstance(row.get("best_sentence"), str) and passage_chars > 0:
            row["best_sentence"] = row["best_sentence"][:passage_chars]
        bounded_hits.append(row)
    if bounded_hits:
        material["local_passages"] = {
            "reading_mode": "focused_local_search",
            "question": question,
            "passages": bounded_hits,
            "external_calls": 0,
        }
    if deep_material and isinstance(material.get("deep_read_material"), Mapping):
        material["deep_read_material"] = {
            **dict(material["deep_read_material"]),
            "reading_mode": "focused_local_search",
        }
    return material


def build_candidate_navigation(
    *,
    chapter: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    source_routes: Sequence[Mapping[str, Any]] = (),
    research_question: str = "",
    index_path: str | Path | None = None,
    candidate_limit: int = 12,
    passages_per_paper: int = 2,
    passage_chars: int = 1200,
    deep_material_by_paper: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Expose bounded candidate navigation and local material access.

    The chapter model receives identities and reasons for the candidates that
    are relevant to this chapter, including relevant sources outside its old
    ``source_ids``.  Only top local hits receive passage content; the complete
    pool is retained by the program and remains reachable through the recorded
    local index and candidate handles.
    """

    if isinstance(candidates, Mapping):
        rows = [row for row in candidates.values() if isinstance(row, Mapping)]
    else:
        rows = [row for row in candidates if isinstance(row, Mapping)]
    by_id = {_text(row.get("_paper_id") or _canonical_paper_id(row)): row for row in rows if _text(row.get("_paper_id") or _canonical_paper_id(row))}
    by_handle = {_text(row.get("_source_handle")): row for row in rows if _text(row.get("_source_handle"))}
    chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
    source_ids: set[str] = set()
    for key in ("source_ids", "paper_ids", "source_handles"):
        values = chapter.get(key) or []
        if isinstance(values, str):
            values = [values]
        if isinstance(values, Sequence):
            source_ids.update(_text(value) for value in values if _text(value))
    assigned_ids = {
        _text(row.get("_paper_id") or _canonical_paper_id(row))
        for row in rows
        if _text(row.get("_paper_id") or _canonical_paper_id(row)) in source_ids
        or _text(row.get("_source_handle")) in source_ids
    }
    route_by_handle: dict[str, dict[str, Any]] = {}
    for raw in source_routes:
        if not isinstance(raw, Mapping):
            continue
        handle = _text(raw.get("source_handle") or raw.get("source_id"))
        if handle and handle not in route_by_handle:
            route_by_handle[handle] = dict(raw)
    route_ids: set[str] = set()
    for handle, route in route_by_handle.items():
        chapter_ids = route.get("chapter_ids") or []
        if isinstance(chapter_ids, str):
            chapter_ids = [chapter_ids]
        if chapter_id and chapter_id in {_text(item) for item in chapter_ids} and handle in by_handle:
            route_ids.add(_text(by_handle[handle].get("_paper_id") or _canonical_paper_id(by_handle[handle])))

    parts = [research_question, _text(chapter.get("title")), _text(chapter.get("purpose")), _text(chapter.get("scope"))]
    for key in ("substantive_threads", "themes", "focus", "material_questions"):
        value = chapter.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parts.extend(_text(item.get("point") if isinstance(item, Mapping) else item) for item in value)
        elif value:
            parts.append(_text(value))
    question = " ".join(item for item in parts if item).strip()[:1200]

    hits_by_paper: dict[str, list[Any]] = {}
    card_matches: dict[str, dict[str, Any]] = {}
    search_meta: dict[str, Any] = {"status": "not_requested", "external_calls": 0}
    if index_path:
        try:
            from .planning_material_search import PlanningMaterialIndex, paper_context, search

            with PlanningMaterialIndex(Path(index_path), readonly=True) as index:
                result = search(
                    index,
                    question or _text(chapter_id),
                    top_papers=max(1, int(candidate_limit) * 2),
                    passages_per_paper=max(1, int(passages_per_paper)),
                )
                for hit in result.hits:
                    hits_by_paper.setdefault(_text(hit.paper_id), []).append(hit)
                contexts = paper_context(index, list(hits_by_paper))
                search_meta = {
                    "status": "matched" if result.found else "empty",
                    "question": result.question,
                    "candidate_papers": result.candidate_papers,
                    "matched_papers": result.matched_papers,
                    "index_papers": result.index_papers,
                    "index_segments": result.index_segments,
                    "not_matched_reason": result.not_matched_reason,
                    "paper_contexts": [item.to_dict() for item in contexts],
                    "external_calls": 0,
                }
        except Exception as exc:
            # A missing or stale local index should be visible to the caller,
            # but it must not turn the optional path into an external search.
            search_meta = {"status": "unavailable", "error": type(exc).__name__, "external_calls": 0}

    # A planner run may have saved A/B cards before a SQLite index exists.
    # Reuse those cards as a bounded lexical navigation pass so an unassigned
    # tail source remains discoverable without a network call.  Card matches are
    # never represented as deep/full-text passages.
    if not hits_by_paper:
        try:
            from .planning_material_search import tokenize

            query_tokens = set(tokenize(question))
            for paper_id, candidate in by_id.items():
                a, b, _identity = _candidate_card_material(candidate)
                card_tokens = set(tokenize(json.dumps({"A": a, "B": b}, ensure_ascii=False, default=_json_default)))
                matched = sorted(query_tokens & card_tokens)
                if matched:
                    card_matches[paper_id] = {
                        "score": len(matched) / max(1, len(query_tokens)),
                        "match_terms": matched,
                    }
            if card_matches:
                search_meta = {
                    "status": "card_fallback",
                    "question": question,
                    "candidate_papers": len(card_matches),
                    "matched_papers": len(card_matches),
                    "index_papers": 0,
                    "index_segments": 0,
                    "external_calls": 0,
                }
        except Exception as exc:
            search_meta = {"status": "card_fallback_unavailable", "error": type(exc).__name__, "external_calls": 0}

    hit_order = sorted(
        hits_by_paper,
        key=lambda paper_id: max(
            float(getattr(hit, "score", 0.0) if not isinstance(hit, Mapping) else hit.get("score") or 0.0)
            for hit in hits_by_paper[paper_id]
        ),
        reverse=True,
    )
    card_order = sorted(card_matches, key=lambda paper_id: (-float(card_matches[paper_id]["score"]), paper_id))
    selected_ids: list[str] = []
    for paper_id in [*sorted(assigned_ids), *sorted(route_ids), *hit_order, *card_order]:
        if paper_id and paper_id in by_id and paper_id not in selected_ids:
            selected_ids.append(paper_id)
    limit = max(1, int(candidate_limit))
    # Always retain local-search hits, even if the assigned list is long.  This
    # is what makes a relevant tail candidate reachable without stuffing the
    # entire pool into the prompt.
    must_keep = [paper_id for paper_id in [*hit_order, *card_order] if paper_id in selected_ids]
    keep_ids = list(dict.fromkeys([*must_keep, *selected_ids]))[:limit]

    navigation: list[dict[str, Any]] = []
    candidate_materials: list[dict[str, Any]] = []
    for paper_id in keep_ids:
        candidate = by_id.get(paper_id)
        if not candidate:
            continue
        handle = _text(candidate.get("_source_handle"))
        route = route_by_handle.get(handle, {})
        route_chapters = route.get("chapter_ids") or []
        if isinstance(route_chapters, str):
            route_chapters = [route_chapters]
        a, b, identity = _candidate_card_material(candidate)
        hit_rows = hits_by_paper.get(paper_id, [])
        card_match = card_matches.get(paper_id) or {}
        navigation.append({
            "source_handle": handle,
            "paper_id": paper_id,
            "title": identity["title"],
            "doi": identity["doi"],
            "year": identity["year"],
            "material_depth": identity["material_depth"],
            "assignment": {
                "selected_in_chapter": paper_id in assigned_ids,
                "routed_to_chapter": paper_id in route_ids,
                "routed_chapter_ids": [_text(item) for item in route_chapters if _text(item)],
                "route_status": _text(route.get("route_status")),
            },
            "material_available": {
                "A": bool(a),
                "B": bool(b),
                "deep": bool((deep_material_by_paper or {}).get(paper_id)),
                "passages": bool(hit_rows),
                "local_index": bool(index_path),
            },
            "retrieval": {
                "source": "local_search" if (hit_rows or card_match) else ("chapter_assignment" if paper_id in assigned_ids else "source_routing"),
                "match_terms": sorted({
                    *[term for hit in hit_rows for term in (getattr(hit, "match_terms", ()) if not isinstance(hit, Mapping) else hit.get("match_terms") or [])],
                    *[str(term) for term in card_match.get("match_terms") or []],
                }),
                "score": max(
                    [float(getattr(hit, "score", 0.0) if not isinstance(hit, Mapping) else hit.get("score") or 0.0) for hit in hit_rows]
                    + [float(card_match.get("score") or 0.0), 0.0]
                ),
            },
        })
        if (hit_rows or card_match) and paper_id not in assigned_ids:
            candidate_materials.append(build_local_material_payload(
                candidate,
                hits=hit_rows,
                question=question,
                deep_material=(deep_material_by_paper or {}).get(paper_id),
                passages_per_paper=passages_per_paper,
                passage_chars=passage_chars,
            ))

    unassigned = [row for row in navigation if not row["assignment"]["selected_in_chapter"]]
    return {
        "chapter_id": chapter_id,
        "query": question,
        "candidate_count": len(rows),
        "returned_count": len(navigation),
        "unassigned_relevant_count": len(unassigned),
        "candidates": navigation,
        "candidate_materials": candidate_materials,
        "source_handles": [row["source_handle"] for row in navigation if row.get("source_handle")],
        "omitted_candidate_count": max(0, len(rows) - len(navigation)),
        "local_search": search_meta,
        "access_contract": {
            "material_fields": ["study_summary_A", "review_planning_B", "local_passages", "deep_read_material"],
            "external_calls": 0,
            "unassigned_material_is_optional": True,
        },
    }


def _messages_for(stage: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    rendered_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    planning_revision = bool(payload.get("planning_revision_mode"))
    case_field = "proposed_use" if planning_revision else "contribution"
    task_output = {
        "chapter_need_analysis": (
            '本轮只做取材任务规划，不生成或复述综述大纲。只返回 {"supplement_requests": [...], "directed_reads": [...]}。'
            "每项必须说明它具体解决哪个章节问题；已有材料能写好则返回空数组。不要返回 chapters、source_ids、论文清单或已有综述正文。"
        ),
        "case_groups": f'本轮只返回 {{"additions": [{{"unit_key": "...", "studies": [{{"source_handle": "...", "{case_field}": "..."}}]}}]}}，不复述输入。',
        "source_routing": '本轮只返回 {"source_routes": [...]}，逐篇给出 source_handle、chapter_ids、specific_usable_material，不重写大纲。',
        "chapter_details": '本轮只返回 {"chapter_plan": {...}}：thesis、reader_objective、units。每个实质单元包含 paragraph_briefs（逐段的point、development、source_handles）、案例对象（source_handle、finding、conditions，含决定含义的研究对象与设置；条件随所属判断陈述，不整体堆入 limitations）、跨研究比较和衔接。不能只列论文编号，也不能用一个宽泛展开句代替逐段任务。不要返回输入的 source_materials 或论文库存清单。',
        "affected_chapter_revision": '只返回 {"chapter_updates": [{"chapter_id": "...", "updated_plan": {...}}]}。updated_plan是完整章节，保留有效论述与案例，并落实反馈。各实质单元给出paragraph_briefs，每段写清point、development、source_handles，让写作者无需重新发明论证。',
    }.get(stage, "请用中文撰写本阶段要求的内容，只返回本阶段的JSON结果，不照抄输入字段。")
    if planning_revision and stage == "chapter_details":
        task_output = '本轮只返回 {"chapter_plan": {...}}：thesis、reader_objective、units。每个实质单元包含 paragraph_briefs（逐段的point、development、source_handles）、案例对象（source_handle、finding、conditions，含决定含义的研究对象与设置；条件随所属判断陈述，不整体堆入 limitations）、论证关系和衔接；按材料实际功能组织，不强制比较或解释差异原因。不能只列论文编号，也不能用一个宽泛展开句代替逐段任务。不要返回输入的 source_materials 或论文库存清单。'
    if planning_revision and stage == "affected_chapter_revision":
        task_output = (
            '本轮只返回 {"status":"updated"或"no_change", "chapter_updates":[{"chapter_id":"...", "updated_plan":{...}}], '
            '"unit_id_remap":{"new_unit_id":["old_unit_id"]}}。updated_plan 是完整章节计划；'
            '若材料和任务无需改变，明确返回 status=no_change 并可复用完整原计划。若拆分或合并单元，所有新单元必须有稳定 unit_id，'
            '并提供显式 unit_id_remap，不能按列表位置猜测对应关系。不要返回只有说明没有 updated_plan 的成功结果。'
        )
    return [
        {"role": "system", "content": _planner_instructions(stage, planning_revision=planning_revision)},
        {"role": "user", "content": "当前任务：" + stage + "\n" + rendered_payload + "\n\n【本轮交付】" + task_output},
    ]


def _load_planner_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        if not content.rstrip().endswith(("}", "]")):
            raise
        from json_repair import repair_json
        return repair_json(content, return_objects=True)


def _parse_planner_response(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(raw, Mapping) and "response" in raw and raw.get("_planner_call") is True:
        return dict(raw["response"]), dict(raw.get("telemetry") or {})
    if isinstance(raw, Mapping) and "content" in raw and isinstance(raw.get("content"), str):
        content = str(raw.get("content") or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        try:
            parsed = _load_planner_json(content)
        except json.JSONDecodeError as exc:
            raise ProgressivePlanError("planner_response_invalid_json") from exc
        if not isinstance(parsed, Mapping):
            raise ProgressivePlanError("planner_response_must_be_object")
        telemetry = {
            key: raw.get(key)
            for key in ("usage", "requested_model", "returned_model", "finish_reason", "complete", "call_id", "elapsed_seconds")
            if key in raw
        }
        return dict(parsed), telemetry
    if isinstance(raw, Mapping):
        return dict(raw), {}
    if isinstance(raw, str):
        content = raw.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        try:
            parsed = _load_planner_json(content)
        except json.JSONDecodeError as exc:
            raise ProgressivePlanError("planner_response_invalid_json") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed), {}
    raise ProgressivePlanError("planner_response_must_be_json_object")


class QwenProgressivePlanner:
    """Thin live adapter for the direct Model Studio Qwen client."""

    def __init__(
        self,
        *,
        model: str,
        key_file: str | Path,
        budget_ledger_path: str | Path,
        budget_limit_cny: float | None,
        output_dir: str | Path,
        tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
        timeout_seconds: float = 900.0,
        thinking_budget: int = 8_192,
        output_tokens: int = 18_000,
        chapter_model: str = DEFAULT_READER_MODEL,
    ):
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient

        self.ledger = GlobalBudgetLedger(path=Path(budget_ledger_path), limit_cny=budget_limit_cny)
        self.counter = qwen_local_token_counter(tokenizer_path)
        self.model = model
        self.output_dir = Path(output_dir)
        self.key_file = Path(key_file)
        self.timeout_seconds = timeout_seconds
        self.output_tokens = output_tokens
        self.thinking_budget = thinking_budget
        self.chapter_model = chapter_model

    def __call__(self, stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .module4.runtime import invoke_client
        from .module4.runtime import QwenDirectClient

        messages = _messages_for(stage, payload)
        call_model = self.chapter_model if stage in {"chapter_details", "case_groups"} else self.model
        output_tokens = self.output_tokens
        call_thinking_budget = 2_048 if stage in {"source_routing", "chapter_details", "case_groups"} else self.thinking_budget
        if stage == "chapter_details":
            output_tokens = min(output_tokens, 16_000)
        if stage == "whole_plan_improvement":
            output_tokens = min(output_tokens, 5_000)
            call_thinking_budget = min(call_thinking_budget, 1_024)
        elif stage == "affected_chapter_revision":
            output_tokens = min(output_tokens, 16_000)
            call_thinking_budget = min(call_thinking_budget, 1_024)
        estimated_input = int(self.counter(b"", messages) * TOKEN_MARGIN_MULTIPLIER + 0.999999) + TOKEN_FRAMING_MARGIN
        total_context_estimate = estimated_input + output_tokens + (call_thinking_budget if call_thinking_budget else 0)
        if estimated_input > MAX_INPUT_TOKENS or total_context_estimate >= 1_000_000:
            raise ProgressivePlanError(
                f"planner_context_preflight_exceeded:{stage}:input={estimated_input}:output={output_tokens}:thinking={call_thinking_budget}"
            )
        chapter = payload.get("chapter") if isinstance(payload.get("chapter"), Mapping) else {}
        identity = payload.get("call_id") or chapter.get("chapter_id") or payload.get("topic_id") or "global"
        call_id = "progressive-review:" + _safe_id(stage) + ":" + _safe_id(identity)
        client = QwenDirectClient(
            model=call_model,
            key_file=self.key_file,
            max_retries=1,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=output_tokens,
            thinking=True,
            thinking_budget=call_thinking_budget,
            json_mode=False,
            budget_ledger=self.ledger,
            raw_response_dir=self.output_dir / "raw_responses" / _safe_id(call_id),
            prompt_token_counter=self.counter,
            prompt_token_multiplier=TOKEN_MARGIN_MULTIPLIER,
            prompt_token_framing_margin=TOKEN_FRAMING_MARGIN,
        )
        raw = invoke_client(
            client,
            messages,
            model=call_model,
            max_output_tokens=output_tokens,
            thinking=True,
            thinking_budget=call_thinking_budget,
            call_id=call_id,
        )
        response, telemetry = _parse_planner_response(raw)
        telemetry["prompt_estimation"] = {
            "tokenizer": "data/tokenizers/qwen3_5_9b/tokenizer.json",
            "multiplier": TOKEN_MARGIN_MULTIPLIER,
            "framing_margin": TOKEN_FRAMING_MARGIN,
            "hosted_token_count_is_approximate": True,
        }
        return {"_planner_call": True, "response": response, "telemetry": telemetry}


def _call_record(planner: Callable[[str, Mapping[str, Any]], Any], stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    response, telemetry = _parse_planner_response(planner(stage, payload))
    return {"response": response, "telemetry": telemetry}


def _stage_response(record: Mapping[str, Any]) -> dict[str, Any]:
    response = record.get("response") if isinstance(record.get("response"), Mapping) else record
    return dict(response)


def _seed_owner_unit_ids(
    chapter_plan: Mapping[str, Any],
    arrangement: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Seed existing units with their arrangement IDs before owner revision.

    Older packets commonly omit IDs from the plan while the arrangement has
    stable IDs.  Seeding those known IDs gives an owner an explicit baseline;
    it does not infer identity for a newly split or merged unit.
    """

    seeded = json.loads(json.dumps(dict(chapter_plan), ensure_ascii=False, default=_json_default))
    units = seeded.get("units")
    if not isinstance(units, list):
        return seeded, []
    arranged_ids = [
        _text(item.get("unit_id") or item.get("id"))
        for item in (arrangement.get("units") or ())
        if isinstance(item, Mapping) and _text(item.get("unit_id") or item.get("id"))
    ]
    existing_ids: list[str] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            continue
        row = dict(unit)
        unit_id = _text(row.get("unit_id") or row.get("id"))
        if not unit_id and index < len(arranged_ids):
            unit_id = arranged_ids[index]
            row["unit_id"] = unit_id
        if unit_id:
            existing_ids.append(unit_id)
        units[index] = row
    seeded["units"] = units
    return seeded, existing_ids


def build_arrangement_issue_revision_payload(
    *,
    chapter: Mapping[str, Any],
    chapter_plan: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    arrangement: Mapping[str, Any],
    topic_id: str = "",
    research_question: str = "",
    candidate_navigation: Mapping[str, Any] | None = None,
    candidate_materials: Sequence[Mapping[str, Any]] = (),
    tool_materials: Sequence[Mapping[str, Any]] = (),
    feedback_materials: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the existing chapter-owner input from arrangement feedback.

    The arrangement result remains an expression artifact.  Its issue list is
    converted into the same ``affected_chapter_revision`` payload used by the
    post-case planner, so a caller can rebuild the current task before asking
    arrangement or writing to proceed again.
    """

    chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
    seeded_plan, existing_unit_ids = _seed_owner_unit_ids(chapter_plan, arrangement)
    issues = []
    for raw in arrangement.get("issues") or ():
        if not isinstance(raw, Mapping):
            continue
        issue = dict(raw)
        issue.setdefault("chapter_id", chapter_id)
        handles = issue.get("source_handles")
        if isinstance(handles, str):
            issue["source_handles"] = [handles]
        issues.append(issue)
    return {
        "topic_id": _text(topic_id),
        "research_question": _text(research_question),
        "planning_revision_mode": True,
        "call_id": f"arrangement-issue-revision-{_safe_id(chapter_id)}",
        "chapter_id": chapter_id,
        "chapter": dict(chapter),
        "chapter_plan": seeded_plan,
        "chapter_feedback": issues,
        "arrangement_issues": issues,
        "source_materials": [dict(item) for item in source_materials if isinstance(item, Mapping)],
        "candidate_navigation": dict(candidate_navigation or {}),
        "candidate_materials": [dict(item) for item in candidate_materials if isinstance(item, Mapping)],
        "tool_materials": [dict(item) for item in tool_materials if isinstance(item, Mapping)],
        "feedback_materials": [dict(item) for item in feedback_materials if isinstance(item, Mapping)],
        "unit_identity_contract": {
            "existing_unit_ids": existing_unit_ids,
            "new_unit_requires_explicit_unit_id": True,
            "split_or_merge_requires_unit_id_remap": True,
            "unit_id_remap_shape": "{new_unit_id: [old_unit_id, ...]}",
        },
        "required_behavior": {
            "return_complete_updated_plan": True,
            "review_original_and_incremental_material_together": True,
            "preserve_source_handles_and_limits": True,
            "treat_arrangement_issue_as_owner_request": True,
            "allow_no_material_change": True,
            "do_not_add_evidence": True,
        },
    }


def _feedback_action(issue: Mapping[str, Any]) -> str:
    """Normalize the small action vocabulary accepted by the feedback CLI."""

    raw = _text(issue.get("action") or issue.get("route") or issue.get("kind")).casefold()
    compact = raw.replace("-", "_").replace(" ", "_")
    if compact in {"local_backfill", "local_material_backfill", "backfill", "local"}:
        return "local_backfill"
    if compact in {"directed_read", "directed_reading", "deep_read", "deep_reading"}:
        return "directed_read"
    if compact in {"supplement", "supplement_read", "external_supplement", "retrieval"}:
        return "supplement"
    if compact in {"chapter_owner", "owner", "affected_chapter_revision", "chapter_revision", "omit", "exclude", "drop", ""}:
        return "chapter_owner"
    return compact


def _feedback_signature(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _merge_feedback_source_materials(
    packet: Mapping[str, Any],
    incoming: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Merge authoritative local/tool material by handle without copying claims."""

    rows: dict[str, dict[str, Any]] = {
        _text(item.get("source_handle")): dict(item)
        for item in (packet.get("source_materials") or ())
        if isinstance(item, Mapping) and _text(item.get("source_handle"))
    }
    before = _feedback_signature({key: _material_content(value) for key, value in rows.items()})
    for raw in incoming:
        if not isinstance(raw, Mapping):
            continue
        handle = _text(raw.get("source_handle"))
        if not handle:
            continue
        current = rows.get(handle, {})
        # A returned row is authoritative only for the fields it actually
        # supplied.  Old cases/findings are never copied into a new unit here.
        rows[handle] = {**current, **dict(raw)}
    after = _feedback_signature({key: _material_content(value) for key, value in rows.items()})
    return list(rows.values()), before != after


def _owner_unit_id_remap(value: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Read an explicit owner supplied new-unit -> old-unit map."""

    if not isinstance(value, Mapping):
        return {}
    raw = value.get("unit_id_remap") or value.get("unit_remap") or value.get("unit_id_mapping")
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, list[str]] = {}
    for new_id, old_ids in raw.items():
        key = _text(new_id)
        if not key:
            continue
        if isinstance(old_ids, str):
            old_ids = [old_ids]
        if isinstance(old_ids, Sequence):
            output[key] = list(dict.fromkeys(_text(item) for item in old_ids if _text(item)))
    return output


def _validate_owner_plan_update(
    old_plan: Mapping[str, Any],
    updated_plan: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    *,
    owner_response: Mapping[str, Any] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Validate structural changes without guessing identity by list position."""

    errors: list[str] = []
    old_units = [item for item in (old_plan.get("units") or ()) if isinstance(item, Mapping)]
    new_units = [item for item in (updated_plan.get("units") or ()) if isinstance(item, Mapping)]
    old_ids = [_text(item.get("unit_id") or item.get("id")) for item in old_units]
    new_ids = [_text(item.get("unit_id") or item.get("id")) for item in new_units]
    old_ids = [item for item in old_ids if item]
    new_ids = [item for item in new_ids if item]
    remap = _owner_unit_id_remap(owner_response) or _owner_unit_id_remap(updated_plan)
    structural_change = len(old_units) != len(new_units) or (old_ids and new_ids and old_ids != new_ids)
    if structural_change:
        if len(new_ids) != len(new_units) or not remap:
            errors.append("structural_unit_change_requires_explicit_unit_ids_and_remap")
        else:
            allowed_old = set(old_ids)
            if not allowed_old:
                errors.append("structural_unit_change_old_units_missing_stable_ids")
            for new_id, old_refs in remap.items():
                if new_id not in set(new_ids) or any(old_id not in allowed_old for old_id in old_refs):
                    errors.append("unit_id_remap_references_unknown_unit")
    available = {
        _text(item.get("source_handle"))
        for item in source_materials
        if isinstance(item, Mapping) and _text(item.get("source_handle"))
    }
    for unit in new_units:
        handles: set[str] = set()
        for key in ("source_handle", "source_handles"):
            value = unit.get(key)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, Sequence):
                handles.update(_text(item) for item in value if _text(item))
        for key in ("cases", "supporting_studies", "concrete_studies", "cases_and_sources", "cases_and_references"):
            for item in unit.get(key) or ():
                if not isinstance(item, Mapping):
                    continue
                handle = _text(item.get("source_handle"))
                if handle:
                    handles.add(handle)
        missing = sorted(handle for handle in handles if re.fullmatch(r"P\d{3,}", handle) and handle not in available)
        if missing:
            errors.append("updated_unit_sources_unavailable:" + ",".join(missing))
    return remap, list(dict.fromkeys(errors))


def _owner_response_plan(response: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract one complete plan from the supported owner response shapes."""

    for entry in ProgressiveReviewPlanner._improvement_entries(response):
        candidate = ProgressiveReviewPlanner._improvement_plan(entry)
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    candidate = response.get("updated_plan")
    return dict(candidate) if isinstance(candidate, Mapping) and candidate else None


def _classify_owner_response(
    old_plan: Mapping[str, Any],
    response: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any] | None, dict[str, list[str]], list[str]]:
    """Validate owner output before a caller treats a provider call as complete."""

    response_status = _text(response.get("status") or response.get("revision_status")).casefold()
    remap: dict[str, list[str]] = {}
    errors: list[str] = []
    updated_plan = _owner_response_plan(response)
    if response_status in {"failed", "error", "partial", "unresolved", "incomplete"}:
        return "unresolved", None, remap, ["owner_response_status:" + response_status]
    if updated_plan is not None:
        remap, errors = _validate_owner_plan_update(
            old_plan, updated_plan, source_materials, owner_response=response,
        )
        if errors:
            return "unresolved", None, remap, errors
        if response_status in {"no_change", "unchanged", "no-change", "reused"}:
            return "no_change", updated_plan, remap, []
        return "updated", updated_plan, remap, []
    if response_status in {"no_change", "unchanged", "no-change", "reused"}:
        return "no_change", dict(old_plan), remap, []
    return "unresolved", None, remap, ["owner_response_missing_complete_plan"]


def _canonicalize_feedback_materials(
    packet: Mapping[str, Any],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach local handles to raw supplement/deep material before owner use."""

    rows = [dict(item) for item in (packet.get("source_materials") or ()) if isinstance(item, Mapping)]
    rows.extend(dict(item) for item in (packet.get("candidate_materials") or ()) if isinstance(item, Mapping))
    rows.extend({**dict(item), "source_handle": handle} for handle, item in
                (packet.get("source_identity_map") or {}).items() if isinstance(item, Mapping))
    by_paper = {
        _text(item.get("paper_id") or item.get("canonical_paper_id")): _text(item.get("source_handle"))
        for item in rows
        if _text(item.get("paper_id") or item.get("canonical_paper_id")) and _text(item.get("source_handle"))
    }
    used = {
        int(match.group(1))
        for match in (re.fullmatch(r"P(\d+)", _text(item.get("source_handle"))) for item in rows)
        if match
    }
    next_number = max(used or {0}) + 1
    output: list[dict[str, Any]] = []
    for raw in incoming:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        identity = row.get("record_identity") if isinstance(row.get("record_identity"), Mapping) else {}
        paper_id = _text(row.get("paper_id") or row.get("canonical_paper_id") or identity.get("paper_id") or identity.get("canonical_paper_id"))
        handle = _text(row.get("source_handle")) or by_paper.get(paper_id, "")
        if not handle and paper_id:
            handle = f"P{next_number:04d}"
            next_number += 1
            by_paper[paper_id] = handle
        if handle:
            row["source_handle"] = handle
        if paper_id and not _text(row.get("paper_id")):
            row["paper_id"] = paper_id
        output.append(row)
    return output


def run_feedback_loop(
    *,
    packet_path: str | Path,
    arrangement_path: str | Path,
    arrangement: Mapping[str, Any],
    owner_planner: Callable[[str, Mapping[str, Any]], Any],
    arrangement_runner: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Mapping[str, Any]],
    writer_runner: Callable[[Mapping[str, Any], Mapping[str, Any], Path], Mapping[str, Any]],
    output_dir: str | Path,
    feedback_runner: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any], Path], Mapping[str, Any]] | None = None,
    resume: bool = True,
    execution_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume one arrangement feedback envelope and rebuild downstream inputs.

    The callbacks are the existing arrangement and writer entry points supplied
    by the CLI; this function only sequences them and persists the authoritative
    owner plan.  Empty feedback reuses the current packet and downstream
    artifacts without issuing a new model call.
    """

    packet_file = Path(packet_path)
    arrangement_file = Path(arrangement_path)
    packet = _read_json(packet_file)
    if not isinstance(packet, Mapping):
        raise ProgressivePlanError("feedback_packet_must_be_object")
    arrangement_value = dict(arrangement)
    issues = [dict(item) for item in (arrangement_value.get("issues") or ()) if isinstance(item, Mapping)]
    if not issues:
        return {
            "status": "reused",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": [],
        }
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    active_path = target_dir / "FEEDBACK_ACTIVE.json"
    previous_pointer: Mapping[str, Any] = {}
    if active_path.is_file():
        try:
            loaded_pointer = _read_json(active_path)
            if isinstance(loaded_pointer, Mapping):
                previous_pointer = loaded_pointer
        except ProgressivePlanError:
            previous_pointer = {}
    # Probe only local card/A/B content before deciding whether to call a tool
    # runner.  This keeps identical retries from repeating paid actions while
    # still invalidating the cache when a same-handle card changed.
    probe_packet = dict(packet)
    previous_artifact = Path(str(previous_pointer.get("artifact_dir") or ""))
    previous_packet_path = previous_artifact / "UPDATED_WRITER_PACKET.json"
    if previous_packet_path.is_file():
        try:
            previous_packet = _read_json(previous_packet_path)
        except ProgressivePlanError:
            previous_packet = {}
        if isinstance(previous_packet, Mapping):
            merged_previous, _ = _merge_feedback_source_materials(probe_packet, previous_packet.get("source_materials") or [])
            probe_packet["source_materials"] = merged_previous
            if isinstance(previous_packet.get("tool_materials"), list):
                probe_packet["tool_materials"] = list(previous_packet.get("tool_materials") or [])
            if isinstance(previous_packet.get("feedback_materials"), list):
                probe_packet["feedback_materials"] = list(previous_packet.get("feedback_materials") or [])
    probe_sources = [dict(item) for item in (probe_packet.get("source_materials") or ()) if isinstance(item, Mapping)]
    if probe_sources:
        refreshed_probe = _refresh_local_material_snapshots([{"source_materials": probe_sources}])[0]
        probe_packet["source_materials"] = refreshed_probe.get("source_materials") or probe_sources
    # Downstream packets always serialize these optional lists, while the
    # original packet may omit them.  Treat absent and empty as the same probe
    # input so a completed no-tool run can resume without a duplicate call.
    for optional_key in ("tool_materials", "feedback_materials"):
        if not probe_packet.get(optional_key):
            probe_packet.pop(optional_key, None)
    owner_issues = [item for item in issues if _feedback_action(item) == "chapter_owner"]
    action_issues = [item for item in issues if _feedback_action(item) != "chapter_owner"]
    resume_signature = _feedback_signature({
        "packet": probe_packet,
        "issues": issues,
        "execution_context": dict(execution_context or {}),
    })
    # Keep this probe identity separate from the downstream artifact identity.
    # A completed run must be reusable when its local inputs are unchanged;
    # changing a saved card changes this probe and therefore reopens the loop.
    resume_probe_signature = resume_signature
    previous_status = _text(previous_pointer.get("status")).casefold()
    reusable_statuses = {"complete", "completed", "updated", "updated_material", "reused", "no_change"}
    if resume and previous_pointer.get("resume_signature") == resume_signature and previous_status in reusable_statuses:
        previous_dir = Path(str(previous_pointer.get("artifact_dir") or ""))
        cached_outputs = (
            previous_dir / "UPDATED_WRITER_PACKET.json",
            previous_dir / "CHAPTER_ARRANGEMENT.json",
            previous_dir / "WRITTEN_RESULT.json",
        )
        if all(path.is_file() for path in cached_outputs):
            return {
                "status": "reused",
                "updated_packet": str(cached_outputs[0]),
                "arrangement": str(cached_outputs[1]),
                "writer": str(cached_outputs[2]),
                "issues": issues,
                "input_signature": previous_pointer.get("input_signature") or resume_signature,
                "resume_signature": resume_signature,
                "reuse_reason": "identical_feedback_and_material_inputs",
            }
        if previous_status in {"reused", "no_change"}:
            return {
                "status": "reused",
                "updated_packet": str(packet_file),
                "arrangement": str(arrangement_file),
                "writer": "",
                "issues": issues,
                "resume_signature": resume_signature,
                "reuse_reason": "owner_no_change_and_material_inputs_unchanged",
            }
    _atomic_json(active_path, {
        "resume_signature": resume_signature,
        "status": "in_progress",
        "artifact_dir": str(target_dir / (resume_signature + ".staging")),
    })
    staging_dir = target_dir / (resume_signature + ".staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    working_packet = dict(packet)
    action_result: dict[str, Any] = {}
    material_changed = False
    tool_materials: list[dict[str, Any]] = []
    feedback_materials: list[dict[str, Any]] = []
    if action_issues:
        if feedback_runner is None:
            _atomic_json(active_path, {
                "resume_signature": resume_probe_signature,
                "status": "partial",
                "artifact_dir": str(staging_dir),
                "unhandled_actions": [_feedback_action(item) for item in action_issues],
            })
            return {
                "status": "partial",
                "updated_packet": str(packet_file),
                "arrangement": str(arrangement_file),
                "writer": "",
                "issues": issues,
                "unhandled_actions": [_feedback_action(item) for item in action_issues],
            }
        raw_action_result = feedback_runner(action_issues, packet, staging_dir)
        action_result = dict(raw_action_result or {}) if isinstance(raw_action_result, Mapping) else {
            "status": "partial", "error": "feedback_runner_return_not_object",
        }
        incoming = _canonicalize_feedback_materials(
            working_packet, action_result.get("source_materials") or (),
        )
        merged_sources, material_changed = _merge_feedback_source_materials(working_packet, incoming)
        if incoming:
            working_packet["source_materials"] = merged_sources
        tool_materials = _canonicalize_feedback_materials(
            working_packet, action_result.get("tool_materials") or (),
        )
        feedback_materials = _canonicalize_feedback_materials(
            working_packet, action_result.get("feedback_materials") or (),
        )
        before_tool_materials = packet.get("tool_materials") or []
        before_feedback_materials = packet.get("feedback_materials") or []
        if tool_materials:
            working_packet["tool_materials"] = [
                *[dict(item) for item in (working_packet.get("tool_materials") or ()) if isinstance(item, Mapping)],
                *tool_materials,
            ]
        if feedback_materials:
            working_packet["feedback_materials"] = [
                *[dict(item) for item in (working_packet.get("feedback_materials") or ()) if isinstance(item, Mapping)],
                *feedback_materials,
            ]
        material_changed = material_changed or _feedback_signature({
            "tool_materials": before_tool_materials,
            "feedback_materials": before_feedback_materials,
        }) != _feedback_signature({
            "tool_materials": working_packet.get("tool_materials") or [],
            "feedback_materials": working_packet.get("feedback_materials") or [],
        })
        owner_issues.extend(dict(item) for item in (action_result.get("owner_feedback") or ()) if isinstance(item, Mapping))
        if material_changed and not owner_issues:
            owner_issues.append({
                "action": "chapter_owner",
                "problem": "Review the updated local material for this chapter and retain or revise the plan with its actual conditions and limits.",
                "source_handles": [
                    _text(item.get("source_handle")) for item in incoming
                    if _text(item.get("source_handle"))
                ],
            })
    if not owner_issues:
        unresolved_actions = bool(
            action_issues and (
                action_result.get("status") not in {"fulfilled", "complete", "completed", "reused", "local_material_ready"}
                or action_result.get("unmet_actions")
            )
        )
        _atomic_json(active_path, {
            "resume_signature": resume_probe_signature,
            "status": "partial" if unresolved_actions else "reused",
            "artifact_dir": str(staging_dir),
        })
        return {
            "status": "partial" if unresolved_actions else "reused",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "action_result": action_result,
        }
    chapter = working_packet.get("chapter") if isinstance(working_packet.get("chapter"), Mapping) else {}
    chapter_plan = working_packet.get("chapter_plan") if isinstance(working_packet.get("chapter_plan"), Mapping) else {}
    # The signature includes fresh local/tool material, so a same-handle card
    # change invalidates a previous loop even when the thesis is unchanged.
    input_signature = _feedback_signature({
        "packet": working_packet,
        "issues": owner_issues,
        "actions": action_issues,
        "action_status": action_result.get("status"),
        "material_changed": material_changed,
        "tool_materials": tool_materials,
        "feedback_materials": feedback_materials,
        "execution_context": dict(execution_context or {}),
    })
    artifact_dir = target_dir / input_signature
    artifact_dir.mkdir(parents=True, exist_ok=True)
    outputs = (
        artifact_dir / "UPDATED_WRITER_PACKET.json",
        artifact_dir / "CHAPTER_ARRANGEMENT.json",
        artifact_dir / "WRITTEN_RESULT.json",
    )
    _atomic_json(active_path, {
        "resume_signature": resume_probe_signature,
        "input_signature": input_signature,
        "artifact_dir": str(artifact_dir),
        "status": "in_progress",
    })

    def assert_active() -> None:
        current = _read_json(active_path)
        if not isinstance(current, Mapping) or current.get("input_signature") != input_signature or current.get("artifact_dir") != str(artifact_dir):
            raise ProgressivePlanError("feedback_result_stale_active_artifact")

    config = ProgressivePlannerConfig(
        topic_id="feedback-loop",
        pool_path=packet_file,
        plan_path=packet_file,
        output_dir=artifact_dir,
        planning_revision_enabled=True,
        shared_deep_read_budget=0,
        chapter_workers=1,
    )
    planner = ProgressiveReviewPlanner(config, planner=owner_planner)
    owner_result = planner.revise_from_arrangement_issues(
        chapter=chapter,
        chapter_plan=chapter_plan,
        source_materials=working_packet.get("source_materials") or [],
        arrangement={**arrangement_value, "issues": owner_issues},
        research_question=_text(working_packet.get("research_question")),
        candidate_navigation=working_packet.get("candidate_navigation") if isinstance(working_packet.get("candidate_navigation"), Mapping) else None,
        candidate_materials=working_packet.get("candidate_materials") or [],
        tool_materials=tool_materials,
        feedback_materials=feedback_materials,
    )
    updated_plan = owner_result.get("updated_plan")
    owner_status = _text(owner_result.get("status"))
    if not isinstance(updated_plan, Mapping):
        assert_active()
        _atomic_json(active_path, {
            "input_signature": input_signature, "status": "partial",
            "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir),
            "owner_status": owner_status, "structural_errors": owner_result.get("structural_errors") or [],
        })
        return {
            "status": "partial",
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "owner_status": owner_status or "unresolved",
            "owner_payload": owner_result.get("owner_payload") or {},
            "structural_errors": owner_result.get("structural_errors") or [],
            "action_result": action_result,
        }
    unresolved_actions = bool(
        action_issues and (
            action_result.get("status") not in {"fulfilled", "complete", "completed", "reused", "local_material_ready"}
            or action_result.get("unmet_actions")
        )
    )
    if owner_status == "no_change" and not material_changed:
        assert_active()
        status = "partial" if unresolved_actions else "reused"
        _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir), "status": status, "owner_status": "no_change"})
        return {
            "status": status,
            "updated_packet": str(packet_file),
            "arrangement": str(arrangement_file),
            "writer": "",
            "issues": issues,
            "owner_status": "no_change",
            "action_result": action_result,
        }
    assert_active()
    updated_packet = dict(working_packet)
    updated_packet["chapter_plan"] = dict(updated_plan)
    updated_packet["feedback_issues"] = [dict(item) for item in issues]
    updated_packet["feedback_action_result"] = action_result
    updated_packet["feedback_materials"] = feedback_materials
    updated_packet["unit_id_remap"] = owner_result.get("unit_id_remap") or {}
    updated_packet_path = artifact_dir / "UPDATED_WRITER_PACKET.json"
    _atomic_json(updated_packet_path, updated_packet)
    assert_active()
    rebuilt_arrangement = dict(arrangement_runner(updated_packet, {**arrangement_value, "issues": owner_issues}, artifact_dir))
    rebuilt_arrangement_path = artifact_dir / "CHAPTER_ARRANGEMENT.json"
    _atomic_json(rebuilt_arrangement_path, rebuilt_arrangement)
    if not rebuilt_arrangement.get("units"):
        assert_active()
        _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir), "status": "partial", "owner_status": owner_status, "arrangement_status": "partial"})
        return {
            "status": "partial",
            "updated_packet": str(updated_packet_path),
            "arrangement": str(rebuilt_arrangement_path),
            "writer": "",
            "issues": issues,
            "owner_status": owner_status,
            "action_result": action_result,
        }
    assert_active()
    written = dict(writer_runner(updated_packet, rebuilt_arrangement, artifact_dir))
    writer_path = artifact_dir / "WRITTEN_RESULT.json"
    _atomic_json(writer_path, written)
    body = written.get("body_markdown")
    if isinstance(body, str):
        assert_active()
        (artifact_dir / "WRITTEN_BODY.md").write_text(body.rstrip() + "\n", encoding="utf-8")
    # Consume the REAL writer result shape: a body being present never means
    # the problem is resolved.  An explicit incomplete flag, a length cutoff,
    # new writer-issued issues, or a partial multi-unit dispatch all keep the
    # run partial and surface the pending work in the final report.
    written_issues = [
        dict(item) for item in (written.get("issues") or [])
        if isinstance(item, Mapping)
    ]
    writer_complete = written.get("complete")
    writer_completion = _text(written.get("completion_status"))
    pending_units = [
        _text(item) for item in (written.get("affected_units") or [])
        if _text(item)
    ] if _text(written.get("status")).casefold() == "partial" else []
    unresolved_writer = (
        writer_complete is False
        or writer_completion in {"partial_length", "partial", "unresolved"}
        or bool(written_issues)
        or bool(pending_units)
    )
    final_status = "partial" if (
        action_result.get("status") in {"partial", "unmet", "failed", "external_research_required"}
        or _text(written.get("status")).casefold() in {"partial", "unresolved", "failed"}
        or unresolved_writer
        or (not isinstance(written.get("body_markdown"), str) and not written.get("body_path"))
    ) else ("updated_material" if material_changed and owner_status == "no_change" else owner_status or "updated")
    assert_active()
    _atomic_json(active_path, {"input_signature": input_signature, "resume_signature": resume_probe_signature, "artifact_dir": str(artifact_dir), "status": final_status, "owner_status": owner_status})
    return {
        "status": final_status,
        "updated_packet": str(updated_packet_path),
        "arrangement": str(rebuilt_arrangement_path),
        "writer": str(writer_path),
        "issues": [dict(item) for item in issues if isinstance(item, Mapping)],
        # Honest bookkeeping from the real writer result, for the caller's
        # final report; empty lists mean nothing is pending.
        "writer_complete": None if writer_complete is None else bool(writer_complete),
        "writer_completion": writer_completion,
        "writer_issues": written_issues,
        "pending_units": pending_units,
        "owner_payload": owner_result.get("owner_payload") or {},
        "action_result": action_result,
        "input_signature": input_signature,
        "resume_signature": resume_probe_signature,
        "unit_id_remap": owner_result.get("unit_id_remap") or {},
    }


def _merge_directed_tasks(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in tasks:
        paper_id = _text(raw.get("paper_id") or raw.get("canonical_paper_id"))
        if not paper_id:
            continue
        row = merged.setdefault(paper_id, {
            "paper_id": paper_id,
            "chapter_ids": [],
            "questions": [],
            "required_outputs": [],
            "knowledge_gaps": [],
            "reasons": [],
            "priority": raw.get("priority", 0),
        })
        owners = raw.get("chapter_ids") or []
        if isinstance(owners, str):
            owners = [owners]
        if raw.get("chapter_id"):
            owners = [*owners, raw["chapter_id"]]
        row["chapter_ids"] = list(dict.fromkeys([*row["chapter_ids"], *[str(x) for x in owners if _text(x)]]))
        for key in ("questions", "required_outputs"):
            values = raw.get(key) or ((raw.get("question") or raw.get("focus")) if key == "questions" else None) or []
            if isinstance(values, (str, Mapping)):
                values = [values]
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                existing = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default) for item in row[key]}
                for item in values:
                    normalized = dict(item) if isinstance(item, Mapping) else ({("question" if key == "questions" else "description"): str(item)} if _text(item) else None)
                    if normalized is not None:
                        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=_json_default)
                        if encoded not in existing:
                            row[key].append(normalized)
                            existing.add(encoded)
        for key, value in (("knowledge_gaps", raw.get("knowledge_gap")), ("reasons", raw.get("reason") or raw.get("expected_information_gain"))):
            if _text(value) and _text(value) not in row[key]:
                row[key].append(_text(value))
        for key in ("knowledge_gaps", "reasons"):
            values = raw.get(key) or []
            if isinstance(values, str):
                values = [values]
            row[key] = list(dict.fromkeys([*row[key], *[_text(value) for value in values if _text(value)]]))
        try:
            row["priority"] = max(float(row.get("priority") or 0), float(raw.get("priority") or 0))
        except (TypeError, ValueError):
            pass
    return list(merged.values())


def _normalize_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(gaps, start=1):
        gap_id = _safe_id(raw.get("gap_id") or raw.get("id"), f"gap_{index:02d}")
        if gap_id in seen:
            continue
        seen.add(gap_id)
        question = _text(raw.get("gap_question") or raw.get("question"))
        if not question:
            continue
        queries = []
        raw_queries = raw.get("targeted_queries") or raw.get("queries") or []
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        for item in raw_queries:
            if isinstance(item, Mapping):
                query = _text(item.get("query_text") or item.get("query") or item.get("text"))
                kind = _text(item.get("query_type") or item.get("type") or "keyword").casefold()
                facet_id = _text(item.get("facet_id") or "F1")
            else:
                query = _text(item)
                kind = "keyword"
                facet_id = "F1"
            if query and kind in {"keyword", "question"}:
                queries.append({"query_text": query, "query_type": kind, "facet_id": facet_id})
        raw_criteria = raw.get("success_criteria") or []
        if isinstance(raw_criteria, str):
            raw_criteria = [raw_criteria]
        reuse_facets = raw.get("reuse_plan_facet_ids") or raw.get("optional_reuse_plan_facet_ids") or []
        if isinstance(reuse_facets, str):
            reuse_facets = [reuse_facets]
        criteria = [str(x).strip() for x in raw_criteria if _text(x)]
        round_specs = [dict(item) for item in (raw.get("round_specs") or ()) if isinstance(item, Mapping)]
        if not round_specs and queries:
            round_specs = [{"round": 1, "targeted_queries": queries[:3]}]
        normalized.append({
            "gap_id": gap_id,
            "gap_question": question,
            "success_criteria": criteria,
            "targeted_queries": queries[:3],
            "round_specs": round_specs or ([{"round": 1, "targeted_queries": queries[:3]}] if queries else []),
            "reuse_plan_facet_ids": list(dict.fromkeys(_text(x) for x in reuse_facets if _text(x)))[:3],
            "known_papers": [dict(x) if isinstance(x, Mapping) else {"paper_id": str(x)} for x in (raw.get("known_papers") or []) if isinstance(x, Mapping) or _text(x)],
            "reviewed_references": [dict(x) for x in (raw.get("reviewed_references") or []) if isinstance(x, Mapping)],
            "chapter_ids": list(dict.fromkeys(_text(x) for x in (raw.get("chapter_ids") or []) if _text(x))),
            "priority": raw.get("priority", 0),
            **{key: raw[key] for key in ("intended_use", "user_scope", "required_concepts", "known_paper_handles", "comparison_object", "need_id") if key in raw},
        })
        if not normalized[-1]["success_criteria"]:
            normalized[-1]["success_criteria"] = ["Return directly relevant evidence or state why the gap remains unresolved."]
        if "intended_use" in normalized[-1]:
            # A mixed label describes one task, not an unsupported tool route.
            # Quantitative and application-oriented reading retains the more capable reader.
            aliases = {"clinical_effect": "application_outcome"}
            uses = {
                aliases.get(use, use)
                for use in re.split(r"[^a-z_]+", _text(raw.get("intended_use")).casefold())
            }
            normalized[-1]["intended_use"] = next((use for use in (
                "quantification", "application_outcome", "comparison", "mechanism", "study_design", "background"
            ) if use in uses), "mechanism")
    return normalized


def _all_directed_ids(record: Mapping[str, Any]) -> set[str]:
    output: set[str] = {_text(value) for value in record.get("consumed_paper_ids") or []}
    for item in record.get("directed_results") or []:
        if isinstance(item, Mapping):
            for paper_id in item.get("consumed_paper_ids", item.get("attempted_paper_ids") or []):
                output.add(_text(paper_id))
    return {item for item in output if item}


def _blocked_directed_ids(record: Mapping[str, Any]) -> set[str]:
    output: set[str] = set()
    for item in record.get("directed_results") or []:
        if isinstance(item, Mapping):
            output.update(_text(value) for value in item.get("blocked_paper_ids") or [])
    return {item for item in output if item}


class ProgressiveReviewPlanner:
    def __init__(
        self,
        config: ProgressivePlannerConfig,
        *,
        planner: Callable[[str, Mapping[str, Any]], Any],
        supplement_runner: Callable[..., Mapping[str, Any]] | None = None,
        directed_reader: Callable[..., Mapping[str, Any]] | None = None,
        prior_readings: Sequence[Mapping[str, Any]] = (),
        tool_materials_by_chapter: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        retrieval_loop_runner: Callable[..., Mapping[str, Any]] | None = None,
    ):
        if not _text(config.topic_id):
            raise ProgressivePlanError("topic_id_required")
        if config.shared_deep_read_budget < 0:
            raise ProgressivePlanError("shared_deep_read_budget_must_be_nonnegative")
        self.config = config
        self.planner = planner
        self.supplement_runner = supplement_runner
        self.directed_reader = directed_reader
        self.prior_readings = [dict(row) for row in prior_readings if isinstance(row, Mapping)]
        # Material gathered by the shared retrieval queue (work orders 03/04) is
        # handed to the chapter expansion of the chapter it was gathered for.
        self.tool_materials_by_chapter = {
            str(key): [dict(item) for item in value if isinstance(item, Mapping)]
            for key, value in (tool_materials_by_chapter or {}).items()
        }
        # The CLI supplies the production adaptive queue.  Keeping this
        # injectable preserves the offline planner fixtures and makes the
        # queue boundary observable without contacting a provider.
        self.retrieval_loop_runner = retrieval_loop_runner
        self._read_materials: dict[str, dict[str, Any]] = {}

    def _stage(self, name: str, fn: Callable[[], Any], *, resume: bool, state: dict[str, Any], cache_inputs: Any = None) -> Any:
        path = self.config.output_dir / "stages" / (name + ".json")
        if resume and path.is_file():
            previous = (state.get("stage_inputs") or {}).get(name)
            if cache_inputs is None or previous == cache_inputs:
                return _read_json(path)
        state.update({"status": "in_progress", "current_stage": name})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        result = fn()
        _atomic_json(path, result)
        if cache_inputs is not None:
            stage_inputs = dict(state.get("stage_inputs") or {})
            stage_inputs[name] = cache_inputs
            state["stage_inputs"] = stage_inputs
        completed = list(state.get("completed_stages") or [])
        if name not in completed:
            completed.append(name)
        state.update({"status": "in_progress", "current_stage": "", "completed_stages": completed})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return result

    def revise_from_arrangement_issues(
        self,
        *,
        chapter: Mapping[str, Any],
        chapter_plan: Mapping[str, Any],
        source_materials: Sequence[Mapping[str, Any]],
        arrangement: Mapping[str, Any],
        research_question: str = "",
        candidate_navigation: Mapping[str, Any] | None = None,
        candidate_materials: Sequence[Mapping[str, Any]] = (),
        tool_materials: Sequence[Mapping[str, Any]] = (),
        feedback_materials: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Consume arrangement issues through the existing chapter-owner role.

        Empty feedback reuses the current plan.  Non-empty feedback invokes
        ``affected_chapter_revision`` and returns the rebuilt plan together
        with the exact owner payload for the next arrangement/writing step.
        """

        payload = build_arrangement_issue_revision_payload(
            chapter=chapter,
            chapter_plan=chapter_plan,
            source_materials=source_materials,
            arrangement=arrangement,
            topic_id=self.config.topic_id,
            research_question=research_question,
            candidate_navigation=candidate_navigation,
            candidate_materials=candidate_materials,
            tool_materials=tool_materials,
            feedback_materials=feedback_materials,
        )
        issues = payload["arrangement_issues"]
        if not issues:
            return {
                "status": "reused",
                "issues": [],
                "owner_payload": payload,
                "updated_plan": dict(chapter_plan),
                "unit_id_remap": {},
                "rebuild_arrangement_input": {"chapter": dict(chapter), "chapter_plan": dict(chapter_plan),
                                               "source_materials": [dict(item) for item in source_materials if isinstance(item, Mapping)]},
            }
        record = _call_record(self.planner, "affected_chapter_revision", payload)
        response = _stage_response(record)
        owner_input_plan = payload.get("chapter_plan") if isinstance(payload.get("chapter_plan"), Mapping) else chapter_plan
        status, updated_plan, unit_id_remap, structural_errors = _classify_owner_response(
            owner_input_plan, response, source_materials,
        )
        return {
            "status": status,
            "issues": issues,
            "owner_payload": payload,
            "owner_response": response,
            "updated_plan": updated_plan,
            "unit_id_remap": unit_id_remap,
            "structural_errors": structural_errors,
            "rebuild_arrangement_input": {
                "chapter": dict(chapter),
                "chapter_plan": updated_plan if updated_plan is not None else dict(chapter_plan),
                "source_materials": [dict(item) for item in source_materials if isinstance(item, Mapping)],
            },
        }

    def _propose_chapters(self, *, topic: str, outline: Mapping[str, Any],
                          routing: Mapping[str, Any], resume: bool) -> dict[str, Any]:
        """Plan chapter content separately; the local route ledger keeps the bibliography."""
        chapters = _outline_chapter_rows(outline.get("shared_outline"))
        root = self.config.output_dir / "stages" / "chapter_proposals"
        root.mkdir(parents=True, exist_ok=True)

        def propose(chapter: Mapping[str, Any]) -> dict[str, Any]:
            chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
            path = root / (_safe_id(chapter_id) + ".json")
            if resume and path.is_file():
                return _read_json(path)
            routes = [row for row in routing["source_routes"] if chapter_id in (row.get("chapter_ids") or [])]
            material = [{key: row[key] for key in (
                "question", "usable_content", "still_missing", "conditions", "limits", "source_handles"
            ) if key in row} for row in self.tool_materials_by_chapter.get(chapter_id, [])]
            record = _call_record(self.planner, "chapter_proposals", {
                "call_id": "chapter-proposal-" + chapter_id,
                "topic_id": self.config.topic_id, "research_question": topic,
                "chapter": dict(chapter), "shared_level1_outline": outline,
                "source_routing": routes, "chapter_tool_materials": material,
                "candidate_pool_row_count": routing.get("pool_sources"),
            })
            response = _normalize_proposal_response(_stage_response(record))
            rows = [row for row in response.get("chapter_proposals", [])
                    if isinstance(row, Mapping) and _text(row.get("chapter_id") or row.get("id")) == chapter_id]
            if not rows:
                raise ProgressivePlanError("chapter_proposal_missing:" + chapter_id)
            result = {"response": {"chapter_proposals": rows}, "telemetry": record.get("telemetry") or {}}
            _atomic_json(path, result)
            return result

        with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
            records = list(executor.map(propose, chapters))
        return {"response": {"chapter_proposals": [row for record in records
                    for row in _stage_response(record).get("chapter_proposals", [])]},
                "telemetry": {"chapter_calls": [record.get("telemetry") or {} for record in records]}}

    def _route_sources(
        self,
        pool_rows: Sequence[Mapping[str, Any]],
        *,
        shared_outline: Any,
        resume: bool,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Semantically route every full-pool source in bounded cached batches."""
        route_root = self.config.output_dir / "stages" / "source_routing"
        route_root.mkdir(parents=True, exist_ok=True)
        chapters = _outline_chapter_rows(shared_outline)
        chapter_ids = [_text(row.get("chapter_id") or row.get("id")) for row in chapters]
        chapter_ids = [item for item in chapter_ids if item]
        batches = [list(pool_rows[index:index + SOURCE_ROUTING_BATCH_SIZE]) for index in range(0, len(pool_rows), SOURCE_ROUTING_BATCH_SIZE)]
        state.update({"status": "in_progress", "current_stage": "source_routing", "source_routing_batches": len(batches)})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)

        def route_batch(index: int, rows: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
            batch_id = f"batch_{index + 1:03d}"
            cache_path = route_root / f"{batch_id}.json"
            candidate_batch = []
            for row in rows:
                note = row.get("_b_summary", {}).get("root_review_note")
                if isinstance(note, Mapping):
                    note = {key: value for key, value in note.items() if key != "paper_id"}
                candidate_batch.append({
                    "source_handle": _text(row.get("_source_handle")),
                    "paper_title": _text((row.get("_b_summary") or {}).get("title")),
                    "doi": _text((row.get("_b_summary") or {}).get("doi")),
                    "year": _text((row.get("_b_summary") or {}).get("year")),
                    "material_scope": _text((row.get("_b_summary") or {}).get("material_scope")),
                    "material_depth": _text((row.get("_b_summary") or {}).get("declared_content_depth")),
                    "B_review_planning": {
                        key: value for key, value in (row.get("_b_summary") or {}).items()
                        if key != "root_review_note"
                    },
                    "curator_note": note or {},
                    "supplement_material": row.get("supplement_gap_material") or {},
                    "other_supplement_materials": row.get("supplement_gap_materials") or [],
                })
            expected_handles = [row["source_handle"] for row in candidate_batch]
            valid_chapters = set(chapter_ids)

            def normalize_routes(candidates: Sequence[Mapping[str, Any]], raw_routes: Any) -> list[dict[str, Any]]:
                by_handle: dict[str, Mapping[str, Any]] = {}
                if isinstance(raw_routes, list):
                    for route in raw_routes:
                        if isinstance(route, Mapping):
                            handle = _text(route.get("source_handle") or route.get("source_id"))
                            if handle and handle not in by_handle:
                                by_handle[handle] = route
                output_routes = []
                for candidate in candidates:
                    handle = candidate["source_handle"]
                    route = by_handle.get(handle, {})
                    raw_chapters = route.get("chapter_ids") or []
                    if isinstance(raw_chapters, str):
                        raw_chapters = [raw_chapters]
                    assigned = list(dict.fromkeys(
                        _text(value.get("chapter_id") or value.get("id")) if isinstance(value, Mapping) else _text(value)
                        for value in raw_chapters
                    ))
                    assigned = [chapter_id for chapter_id in assigned if chapter_id and (not valid_chapters or chapter_id in valid_chapters)]
                    useful = _text(route.get("specific_usable_material") or route.get("usable_material") or route.get("substantive_use") or route.get("use"))
                    reason = _text(route.get("reason") or route.get("non_use_reason"))
                    if route and not assigned:
                        reason = reason or useful or "当前共享提纲下无明确章节用途。"
                    output_routes.append({
                        "source_handle": handle,
                        "paper_title": candidate["paper_title"],
                        "chapter_ids": assigned,
                        "specific_usable_material": useful,
                        "route_status": "assigned" if assigned else ("not_relevant" if route else "route_result_missing"),
                        "reason": reason,
                    })
                return output_routes

            def failed_routes(candidates: Sequence[Mapping[str, Any]], reason: str) -> list[dict[str, Any]]:
                return [{
                    "source_handle": row["source_handle"],
                    "paper_title": row["paper_title"],
                    "chapter_ids": [],
                    "specific_usable_material": "",
                    "route_status": "route_call_failed",
                    "reason": reason,
                } for row in candidates]

            def invoke(candidates: Sequence[Mapping[str, Any]], *, repair_attempt: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                suffix = f"-repair-{repair_attempt:03d}" if repair_attempt else ""
                payload = {
                    "call_id": f"source-routing-{batch_id}-of-{len(batches):03d}{suffix}",
                    "topic_id": self.config.topic_id,
                    "research_question": _text(state.get("topic")),
                    "shared_outline": _compact_routing_outline(shared_outline),
                    "complete_pool_size": len(pool_rows),
                    "batch_index": index + 1,
                    "batch_count": len(batches),
                    "candidate_batch": [dict(row) for row in candidates],
                    "chapter_ids": chapter_ids,
                    "output_contract": {
                        "array_key": "source_routes",
                        "one_row_per_source_handle": True,
                        "route_every_input": True,
                        "target_chinese_characters_per_useful_source": "80-150",
                        "do_not_invent_sources": True,
                    },
                }
                if repair_attempt:
                    payload["repair_for_batch"] = batch_id
                    payload["repair_attempt"] = repair_attempt
                    payload["route_only_supplied_handles"] = True
                try:
                    record = _call_record(self.planner, "source_routing", payload)
                    response = _stage_response(record)
                    raw_routes = response.get("source_routes") or response.get("routes") or []
                    return normalize_routes(candidates, raw_routes), dict(record.get("telemetry") or {})
                except Exception as exc:
                    return failed_routes(candidates, type(exc).__name__), {"error": type(exc).__name__}

            def pending_from_cache(cached: Mapping[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
                attempted = {
                    _text(handle) for handle in (cached.get("repair_attempted_handles") or []) if _text(handle)
                }
                by_handle = {
                    _text(route.get("source_handle")): route
                    for route in (cached.get("source_routes") or [])
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                }
                pending = []
                for candidate in candidate_batch:
                    handle = candidate["source_handle"]
                    route = by_handle.get(handle) or {}
                    status = _text(route.get("route_status"))
                    if status in {"assigned", "not_relevant"} or handle in attempted:
                        continue
                    pending.append(candidate)
                return pending, attempted

            def repair_cached(cached: Mapping[str, Any], pending: Sequence[Mapping[str, Any]], attempted: set[str]) -> dict[str, Any]:
                repaired, telemetry = invoke(pending, repair_attempt=1)
                by_handle = {
                    _text(route.get("source_handle")): dict(route)
                    for route in (cached.get("source_routes") or [])
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                }
                for route in repaired:
                    by_handle[route["source_handle"]] = route
                merged_routes = [by_handle[handle] for handle in expected_handles if handle in by_handle]
                saved = dict(cached)
                saved["source_handles"] = expected_handles
                saved["source_routes"] = merged_routes
                saved["repair_attempted_handles"] = sorted(attempted | {row["source_handle"] for row in pending})
                saved["repair_telemetry"] = telemetry
                _atomic_json(cache_path, saved)
                return saved

            if resume and cache_path.is_file():
                try:
                    cached = _read_json(cache_path)
                    if isinstance(cached, Mapping):
                        pending, attempted = pending_from_cache(cached)
                        if not pending:
                            return index, dict(cached)
                        return index, repair_cached(cached, pending, attempted)
                except ProgressivePlanError:
                    pass

            output_routes, telemetry = invoke(candidate_batch)
            saved = {
                "batch_id": batch_id,
                "batch_index": index + 1,
                "batch_count": len(batches),
                "source_handles": expected_handles,
                "source_routes": output_routes,
                "telemetry": telemetry,
            }
            pending, attempted = pending_from_cache(saved)
            if pending:
                saved = repair_cached(saved, pending, attempted)
            _atomic_json(cache_path, saved)
            return index, saved

        indexed: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(SOURCE_ROUTING_WORKERS, max(1, self.config.chapter_workers))) as executor:
            futures = [executor.submit(route_batch, index, batch) for index, batch in enumerate(batches)]
            for future in as_completed(futures):
                index, record = future.result()
                indexed[index] = record
        ordered_batches = [indexed[index] for index in range(len(batches))]
        routes = [route for batch in ordered_batches for route in batch.get("source_routes") or []]
        state["completed_stages"] = list(dict.fromkeys([*(state.get("completed_stages") or []), "source_routing"]))
        state.update({"status": "in_progress", "current_stage": ""})
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return {
            "pool_sources": len(pool_rows),
            "batch_size": SOURCE_ROUTING_BATCH_SIZE,
            "batch_count": len(batches),
            "routed_sources": len(routes),
            "assigned_sources": sum(bool(row.get("chapter_ids")) for row in routes),
            "batches": ordered_batches,
            "source_routes": routes,
        }

    def _tool_cycle(
        self,
        *,
        phase: str,
        supplement_requests: Sequence[Mapping[str, Any]],
        directed_requests: Sequence[Mapping[str, Any]],
        pool_rows: Sequence[Mapping[str, Any]],
        plan: Mapping[str, Any],
        prior_directed: Mapping[str, Any] | None,
        prior_tool_results: Mapping[str, Any] | None,
        source_handle_map: Mapping[str, str],
        resume: bool,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        name = phase.lower() + "_tools"

        if self.retrieval_loop_runner is not None:
            def run_adaptive_cycle() -> dict[str, Any]:
                result = self.retrieval_loop_runner(
                    phase=phase,
                    supplement_requests=_normalize_gaps(supplement_requests),
                    directed_requests=_merge_directed_tasks(directed_requests),
                    pool_rows=pool_rows,
                    plan=plan,
                    prior_tool_results=dict(prior_tool_results or {}),
                    prior_directed=dict(prior_directed or {}),
                    source_handle_map=dict(source_handle_map),
                    resume=resume,
                    output_dir=self.config.output_dir / phase.lower(),
                )
                if not isinstance(result, Mapping):
                    raise ProgressivePlanError("retrieval_loop_runner_return_not_object")
                return dict(result)

            result = self._stage(
                name, run_adaptive_cycle, resume=resume, state=state,
                cache_inputs={"supplement_requests": _normalize_gaps(supplement_requests),
                              "directed_requests": _merge_directed_tasks(directed_requests),
                              "prior_tool_results": dict(prior_tool_results or {})},
            )
            for chapter_id, rows in (result.get("tool_materials_by_chapter") or {}).items():
                existing = self.tool_materials_by_chapter.setdefault(str(chapter_id), [])
                seen = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default) for item in existing}
                for row in rows:
                    if isinstance(row, Mapping):
                        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
                        if encoded not in seen:
                            existing.append(dict(row))
                            seen.add(encoded)
            # Keep the adaptive queue's paid readings in the same local store
            # the legacy branch uses, so later phases and packet construction
            # see them without a second retrieval.
            for group in result.get("directed_results") or []:
                if isinstance(group, Mapping):
                    for material in group.get("materials") or []:
                        if isinstance(material, Mapping) and _text(material.get("paper_id")):
                            self._read_materials[_text(material.get("paper_id"))] = dict(material)
            return result

        def run_cycle() -> dict[str, Any]:
            gaps = _normalize_gaps(supplement_requests)
            merged = _merge_directed_tasks(directed_requests)
            pool_by_id = {str(row.get("_paper_id")): dict(row) for row in pool_rows}
            already_read = _all_directed_ids(prior_directed or {})
            blocked_no_reacquire = _blocked_directed_ids(prior_directed or {})
            already_read.update(_text(row.get("paper_id")) for row in self.prior_readings if _text(row.get("paper_id")))
            for row in self.prior_readings:
                paper_id = _text(row.get("paper_id"))
                if paper_id:
                    self._read_materials.setdefault(paper_id, dict(row))
            if prior_directed:
                for result in prior_directed.get("directed_results") or []:
                    if isinstance(result, Mapping):
                        for material in result.get("materials") or []:
                            if isinstance(material, Mapping) and _text(material.get("paper_id")):
                                self._read_materials[_text(material.get("paper_id"))] = dict(material)
            available = max(0, int(self.config.shared_deep_read_budget) - len(already_read))
            reused: list[dict[str, Any]] = []
            candidates: list[dict[str, Any]] = []
            for task in merged:
                paper_id = task["paper_id"]
                if paper_id in already_read:
                    reused.append({**task, "status": "reused_prior_deep_read", "material": self._read_materials.get(paper_id, {})})
                elif paper_id in blocked_no_reacquire:
                    reused.append({**task, "status": "review_reported_source_no_reacquire", "material": {}})
                else:
                    candidates.append(task)
            candidates.sort(key=lambda row: (-float(row.get("priority") or 0), row["paper_id"]))
            selected = candidates[:available]
            deferred = [{**row, "status": "deferred_global_deep_read_budget"} for row in candidates[available:]]

            context = {
                "phase": phase,
                "output_dir": self.config.output_dir / phase.lower(),
                "topic_id": self.config.topic_id,
                "plan": dict(plan),
                "pool_rows": pool_rows,
                "pool_by_id": pool_by_id,
                "shared_deep_read_budget": self.config.shared_deep_read_budget,
                "remaining_deep_read_budget": available,
                "prior_readings": self.prior_readings,
                "prior_tool_results": dict(prior_tool_results or {}),
                "source_handle_map": dict(source_handle_map),
            }
            supplement_future = None
            directed_future = None
            with ThreadPoolExecutor(max_workers=2) as executor:
                if gaps and self.supplement_runner is not None:
                    supplement_future = executor.submit(self.supplement_runner, gaps, **context)
                if selected and self.directed_reader is not None:
                    directed_future = executor.submit(self.directed_reader, selected, **context)
                supplement_result = self._collect_tool_result(supplement_future, "supplement_runner_unavailable", gaps)
                directed_result = self._collect_tool_result(directed_future, "directed_reader_unavailable", selected)
            consumed = {_text(item) for item in directed_result.get("consumed_paper_ids") or [] if _text(item)}
            for result in directed_result.get("materials") or []:
                if isinstance(result, Mapping) and _text(result.get("paper_id")):
                    self._read_materials[_text(result.get("paper_id"))] = dict(result)
            directed_result = {
                **directed_result,
                "consumed_paper_ids": sorted(consumed),
                "blocked_paper_ids": sorted(_text(item) for item in directed_result.get("blocked_paper_ids") or [] if _text(item)),
                "reused_prior_tasks": reused,
                "deferred_tasks": deferred,
                "requested_unique_papers": len(merged),
                "selected_unique_papers": len(selected),
                "shared_budget_remaining": max(0, available - len(consumed)),
            }
            return {
                "phase": phase,
                "supplement_requests": gaps,
                "directed_requests": merged,
                "supplement_results": [supplement_result] if supplement_result else [],
                "directed_results": [directed_result] if directed_result else [],
                "consumed_paper_ids": sorted(consumed),
                "budget_deferred_tasks": deferred,
            }

        return self._stage(name, run_cycle, resume=resume, state=state)

    @staticmethod
    def _collect_tool_result(future: Any, absent_reason: str, requested: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not requested:
            return {"status": "not_requested", "results": []}
        if future is None:
            return {"status": "unavailable", "reason": absent_reason, "results": []}
        try:
            result = future.result()
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__, "results": []}
        if not isinstance(result, Mapping):
            return {"status": "failed", "error": "tool_return_not_object", "results": []}
        return dict(result)

    def run(
        self,
        *,
        resume: bool = False,
        stop_after: str = "",
        editorial_feedback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stop_after not in {"", "level1", "level2"}:
            raise ProgressivePlanError("stop_after_must_be_level1_or_level2")
        root = self.config.output_dir.resolve()
        if root.exists() and any(root.iterdir()) and not resume:
            raise ProgressivePlanError("output_directory_exists_use_resume")
        root.mkdir(parents=True, exist_ok=True)
        pool_rows = load_planning_pool(self.config.pool_path)
        plan = load_original_plan(self.config.plan_path)
        topic = _text(plan.get("question_en") or plan.get("question"))
        editorial_feedback = dict(editorial_feedback or {}) if isinstance(editorial_feedback, Mapping) else {}
        source_handle_map = _refresh_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        b_pool = [dict(row["_b_summary"]) for row in pool_rows]
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        state_path = root / "RUN_STATE.json"
        if resume and state_path.is_file():
            state = dict(_read_json(state_path))
            if str(state.get("topic_id") or "") != self.config.topic_id:
                raise ProgressivePlanError("resume_topic_id_mismatch")
            if bool(state.get("planning_revision_enabled", False)) != bool(self.config.planning_revision_enabled):
                raise ProgressivePlanError("resume_planning_revision_mode_mismatch")
        else:
            state = {
                "schema_version": SCHEMA_VERSION,
                "topic_id": self.config.topic_id,
                "topic": topic,
                "pool_path": str(self.config.pool_path.resolve()),
                "plan_path": str(self.config.plan_path.resolve()),
                "pool_rows": len(pool_rows),
                "shared_deep_read_budget": self.config.shared_deep_read_budget,
                "planning_revision_enabled": bool(self.config.planning_revision_enabled),
                "completed_stages": [],
                "status": "in_progress",
            }
            _atomic_json(state_path, state)

        provisional_record = self._stage(
            "provisional_scope",
            lambda: _call_record(self.planner, "provisional_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "original_plan": plan,
                "pool_row_count": len(b_pool),
                "candidate_pool": b_pool,
                "required_behavior": {"read_all_candidates": True, "candidate_pool_is_complete": True, "do_not_force_use": True},
            }),
            resume=resume,
            state=state,
        )
        provisional = _stage_response(provisional_record)
        provisional = _resolve_planner_handles(provisional, source_handle_map)

        level1_tool_result = self._tool_cycle(
            phase="level1",
            supplement_requests=provisional.get("supplement_requests") or [],
            directed_requests=provisional.get("directed_reads") or [],
            pool_rows=pool_rows,
            plan=plan,
            prior_directed=None,
            prior_tool_results=None,
            source_handle_map=source_handle_map,
            resume=resume,
            state=state,
        )
        pool_rows = _merge_supplement_pool_updates(pool_rows, level1_tool_result)
        source_handle_map = _refresh_source_handles(pool_rows)
        self._bind_tool_material_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        b_pool = [dict(row["_b_summary"]) for row in pool_rows]
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        level1_outline_record = self._stage(
            "level1_outline",
            lambda: _call_record(self.planner, "level1_outline", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "provisional_scope": provisional,
                "actual_tool_results": self._compact_tool_feedback(level1_tool_result),
                "full_b_pool_was_semantically_screened": len(b_pool),
                "unavailable_and_failed_tools_are_scope_feedback": True,
            }),
            resume=resume,
            state=state,
        )
        level1_outline = _stage_response(level1_outline_record)
        if stop_after == "level1":
            partial = self._partial_result(topic, plan, provisional, level1_outline, level1_tool_result, "level1")
            _atomic_json(root / "PROGRESSIVE_REVIEW_PLAN.partial.json", partial)
            (root / "PROGRESSIVE_REVIEW_PLAN.partial.md").write_text(render_plan_markdown(partial), encoding="utf-8", newline="\n")
            state.update({"status": "stopped_after_level1", "current_stage": "", "output": str(root / "PROGRESSIVE_REVIEW_PLAN.partial.json")})
            _atomic_json(state_path, state)
            return partial

        routing = self._route_sources(pool_rows, shared_outline=level1_outline.get("shared_outline"), resume=resume, state=state)
        limits_by_handle = {row["_source_handle"]: row["_b_summary"].get("scope_interpretation_cautions") or [] for row in pool_rows}
        for route in routing["source_routes"]:
            route["interpretation_limits"] = limits_by_handle.get(route.get("source_handle"), [])
        _atomic_json(root / "stages" / "source_routing_summary.json", routing)
        proposals_record = self._stage(
            "chapter_proposals",
            lambda: self._propose_chapters(topic=topic, outline=level1_outline, routing=routing, resume=resume),
            resume=resume,
            state=state,
        )
        proposals = self._attach_routed_sources(_normalize_proposal_response(_stage_response(proposals_record)), routing["source_routes"], level1_outline.get("shared_outline"))
        proposals = _resolve_planner_handles(proposals, source_handle_map)
        harmonize_record = self._stage(
            "harmonized_scope",
            lambda: _call_record(self.planner, "harmonize_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_level1_outline": level1_outline,
                "chapter_proposals": _planner_scope_copy(proposals, paper_to_handle),
                "source_routing": routing["source_routes"],
                "level1_tool_results": self._compact_tool_feedback(level1_tool_result),
                "global_unique_deep_read_budget": self.config.shared_deep_read_budget,
            }),
            resume=resume,
            state=state,
        )
        harmonized = _resolve_planner_handles(_stage_response(harmonize_record), source_handle_map)
        harmonized["chapters"] = self._harmonized_chapters(harmonized, proposals)
        level2_tool_result = self._tool_cycle(
            phase="level2",
            supplement_requests=harmonized.get("supplement_requests") or [],
            directed_requests=harmonized.get("directed_reads") or [],
            pool_rows=pool_rows,
            plan=plan,
            prior_directed=level1_tool_result,
            prior_tool_results=level1_tool_result,
            source_handle_map=source_handle_map,
            resume=resume,
            state=state,
        )
        pool_rows = _merge_supplement_pool_updates(pool_rows, level2_tool_result)
        source_handle_map = _refresh_source_handles(pool_rows)
        self._bind_tool_material_source_handles(pool_rows)
        paper_to_handle = {paper_id: handle for handle, paper_id in source_handle_map.items()}
        candidate_by_id = {str(row["_paper_id"]): row for row in pool_rows}
        final_scope_record = self._stage(
            "finalize_chapter_scope",
            lambda: _call_record(self.planner, "finalize_chapter_scope", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "harmonized_shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                "harmonized_chapters": (
                    _planner_scope_copy(harmonized, paper_to_handle).get("chapters")
                    or _planner_scope_copy(harmonized, paper_to_handle).get("harmonized_chapters")
                    or _planner_scope_copy(proposals, paper_to_handle).get("chapter_proposals")
                    or []
                ),
                "level2_tool_results": self._compact_tool_feedback(level2_tool_result),
                "new_and_upgraded_sources": [dict(row["_b_summary"]) for row in pool_rows if row.get("supplement_gap_material")],
                "do_not_issue_more_tool_requests": True,
            }),
            resume=resume,
            state=state,
        )
        final_scope = _resolve_planner_handles(_stage_response(final_scope_record), source_handle_map)
        harmonized = {
            **harmonized,
            "shared_outline": final_scope.get("shared_outline") or harmonized.get("shared_outline"),
            "final_scope_notes": final_scope.get("final_scope_notes") or [],
        }
        chapters = self._harmonized_chapters(final_scope, {"chapter_proposals": harmonized.get("chapters") or proposals.get("chapter_proposals") or []})
        if stop_after == "level2":
            partial = self._partial_result(topic, plan, provisional, level1_outline, level1_tool_result, "level2", harmonized=harmonized, level2_tools=level2_tool_result)
            _atomic_json(root / "PROGRESSIVE_REVIEW_PLAN.partial.json", partial)
            (root / "PROGRESSIVE_REVIEW_PLAN.partial.md").write_text(render_plan_markdown(partial), encoding="utf-8", newline="\n")
            state.update({"status": "stopped_after_level2", "current_stage": "", "output": str(root / "PROGRESSIVE_REVIEW_PLAN.partial.json")})
            _atomic_json(state_path, state)
            return partial

        chapter_gaps, chapter_directed = _chapter_retrieval_requests(chapters)
        if self.retrieval_loop_runner is not None:
            chapter_needs_record = self._stage(
                "chapter_need_analysis",
                lambda: _call_record(self.planner, "chapter_need_analysis", {
                    "topic_id": self.config.topic_id,
                    "research_question": topic,
                    "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                    "chapters": [{key: row.get(key) for key in ("chapter_id", "title", "purpose", "scope", "substantive_threads")}
                                 for row in chapters],
                    "available_material": [{key: row.get(key) for key in ("source_handle", "chapter_ids", "specific_usable_material", "interpretation_limits")}
                                           for row in routing["source_routes"] if row.get("chapter_ids")],
                    "level1_tool_results": self._compact_tool_feedback(level1_tool_result),
                    "level2_tool_results": self._compact_tool_feedback(level2_tool_result),
                    "required_behavior": {"bounded_questions": True, "preserve_chapter_ids": True},
                }),
                resume=resume,
                state=state,
                cache_inputs={
                    "chapters": [{key: row.get(key) for key in ("chapter_id", "title", "purpose", "scope", "substantive_threads")}
                                 for row in chapters],
                    "level2_tool_results": self._compact_tool_feedback(level2_tool_result),
                },
            )
            chapter_needs = _resolve_planner_handles(_stage_response(chapter_needs_record), source_handle_map)
            raw_chapter_gaps = chapter_needs.get("supplement_requests") or chapter_needs.get("retrieval_gaps") or []
            raw_chapter_directed = chapter_needs.get("directed_reads") or chapter_needs.get("directed_read_requests") or []
            chapter_gaps.extend(_normalize_gaps([
                {
                    **dict(item),
                    "chapter_ids": list(dict.fromkeys([
                        *([item.get("chapter_id")] if item.get("chapter_id") else []),
                        *(([item.get("chapter_ids")] if isinstance(item.get("chapter_ids"), str) else list(item.get("chapter_ids") or []))),
                    ])),
                }
                for item in raw_chapter_gaps if isinstance(item, Mapping)
            ]))
            chapter_directed.extend(_merge_directed_tasks([
                {
                    **dict(item),
                    "chapter_ids": list(dict.fromkeys([
                        *([item.get("chapter_id")] if item.get("chapter_id") else []),
                        *(([item.get("chapter_ids")] if isinstance(item.get("chapter_ids"), str) else list(item.get("chapter_ids") or []))),
                    ])),
                }
                for item in raw_chapter_directed if isinstance(item, Mapping)
            ]))
        chapter_tool_result: dict[str, Any] = {"phase": "chapters", "supplement_results": [], "directed_results": []}
        if chapter_gaps or chapter_directed:
            chapter_tool_result = self._tool_cycle(
                phase="chapters",
                supplement_requests=chapter_gaps,
                directed_requests=chapter_directed,
                pool_rows=pool_rows,
                plan=plan,
                prior_directed=level2_tool_result,
                prior_tool_results=level2_tool_result,
                source_handle_map=source_handle_map,
                resume=resume,
                state=state,
            )
            pool_rows = _merge_supplement_pool_updates(pool_rows, chapter_tool_result)
            source_handle_map = _refresh_source_handles(pool_rows)
            candidate_by_id = {str(row.get("_paper_id")): row for row in pool_rows}
            self._bind_tool_material_source_handles(pool_rows)
            chapters = _attach_chapter_candidate_sources(chapters, pool_rows, chapter_tool_result)

        detail_records = self._chapter_details(
            chapters=chapters,
            shared_outline=harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
            topic=topic,
            candidates=candidate_by_id,
            level1_tools=level1_tool_result,
            level2_tools=level2_tool_result,
            chapter_tools=chapter_tool_result,
            resume=resume,
            state=state,
            tool_materials_by_chapter=self.tool_materials_by_chapter,
            candidate_pool=pool_rows,
        )
        whole_review_chapters = []
        for row in detail_records:
            chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            chapter_tool_materials = [
                _tool_material_for_prompt(item)
                for item in self.tool_materials_by_chapter.get(chapter_id, [])
                if isinstance(item, Mapping)
            ]
            whole_review_chapters.append({
                "chapter_id": chapter_id,
                "title": _text(chapter.get("title")),
                "scope": chapter.get("scope"),
                "chapter_plan": row.get("chapter_plan"),
                # The whole-plan pass gets only the A/B for handles actually
                # cited by this chapter, rather than the full routed pool.
                "source_materials": _chapter_review_source_materials(row),
                "tool_materials": chapter_tool_materials,
            })
        whole_record = ({"response": {}}
                        if self.config.planning_revision_enabled else self._stage(
            "whole_plan_improvement",
            lambda: _call_record(self.planner, "whole_plan_improvement", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                "chapters": whole_review_chapters,
                "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result}),
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
                "do_not_invent_evidence": True,
            }),
            resume=resume,
            state=state,
            cache_inputs={
                "chapters": whole_review_chapters,
                "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result}),
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
            },
        ))
        improvement = _stage_response(whole_record)
        # The light whole-plan pass often returns useful prose adjustments but
        # leaves the chapter rewrite implicit.  Give named affected chapters a
        # bounded second call so those adjustments reach the writer packet as
        # concrete plans instead of remaining telemetry.
        editorial_feedback_entries = self._editorial_feedback_entries(editorial_feedback)
        affected_ids = list(dict.fromkeys([
            *self._affected_chapter_ids(improvement),
            *[_text(item.get("chapter_id")) for item in editorial_feedback_entries if _text(item.get("chapter_id"))],
        ]))
        concrete_ids = self._concrete_improvement_ids(improvement)
        editorial_ids = {_text(item.get("chapter_id")) for item in editorial_feedback_entries if _text(item.get("chapter_id"))}
        revision_ids = [chapter_id for chapter_id in affected_ids if chapter_id not in concrete_ids or chapter_id in editorial_ids]
        if revision_ids and not self.config.planning_revision_enabled:
            detail_by_id = {
                _text((row.get("chapter") or {}).get("chapter_id")): row
                for row in detail_records
                if isinstance(row, Mapping) and isinstance(row.get("chapter"), Mapping)
            }
            revision_root = root / "stages" / "affected_chapter_revision"
            revision_root.mkdir(parents=True, exist_ok=True)
            state.update({"status": "in_progress", "current_stage": "affected_chapter_revision"})
            _atomic_json(root / "RUN_STATE.json", state)
            common_revision_payload = {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "improvement_notes": improvement.get("improvement_notes") or [],
                "cross_chapter_adjustments": improvement.get("cross_chapter_adjustments") or [],
                "editorial_feedback": editorial_feedback,
                "citation_rules": dict(CURRENT_CITATION_RULES),
                "required_behavior": {
                    "return_complete_updated_plan": True,
                    "preserve_source_handles_and_limits": True,
                    "do_not_add_evidence": True,
                },
            }
            if self.config.planning_revision_enabled:
                common_revision_payload["planning_revision_mode"] = True
            feedback_by_chapter: dict[str, list[dict[str, Any]]] = {}
            for item in self._improvement_entries(improvement):
                chapter_id = _text(item.get("chapter_id") or item.get("id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            for item in editorial_feedback_entries:
                chapter_id = _text(item.get("chapter_id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            review_chapter_by_id = {
                _text(item.get("chapter_id")): item
                for item in whole_review_chapters
                if _text(item.get("chapter_id"))
            }
            revision_inputs = {
                chapter_id: {
                    **common_revision_payload,
                    "call_id": f"affected-chapter-revision-{_safe_id(chapter_id)}",
                    "chapter_id": chapter_id,
                    "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                    "chapter_plan": (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {},
                    "chapter_feedback": feedback_by_chapter.get(chapter_id) or [],
                    "editorial_feedback_for_chapter": [
                        dict(item) for item in editorial_feedback_entries
                        if _text(item.get("chapter_id")) == chapter_id
                    ],
                    "source_materials": (review_chapter_by_id.get(chapter_id) or {}).get("source_materials") or [],
                    "tool_materials": (review_chapter_by_id.get(chapter_id) or {}).get("tool_materials") or [],
                    **({
                        "candidate_navigation": (detail_by_id.get(chapter_id) or {}).get("candidate_navigation") or {},
                        "candidate_materials": (detail_by_id.get(chapter_id) or {}).get("candidate_materials") or [],
                    } if self.config.planning_revision_enabled else {}),
                    "affected_chapter": {
                        "chapter_id": chapter_id,
                        "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                        "chapter_plan": (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {},
                    },
                }
                for chapter_id in revision_ids
                if chapter_id in detail_by_id
            }

            def revise_one(item: tuple[int, tuple[str, Mapping[str, Any]]]) -> tuple[int, dict[str, Any]]:
                index, (chapter_id, revision_payload) = item
                cache_path = revision_root / f"{_safe_id(chapter_id)}.json"
                if resume and cache_path.is_file():
                    try:
                        cached = _read_json(cache_path)
                        if (
                            isinstance(cached, Mapping)
                            and cached.get("status") == "complete"
                            and cached.get("cache_inputs") == revision_payload
                        ):
                            return index, dict(cached)
                    except ProgressivePlanError:
                        pass
                try:
                    record = _call_record(self.planner, "affected_chapter_revision", revision_payload)
                    response = _stage_response(record)
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "complete",
                        "response": response,
                        "telemetry": record.get("telemetry") or {},
                        "cache_inputs": dict(revision_payload),
                    }
                except Exception as exc:
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "failed",
                        "error": type(exc).__name__,
                        "error_detail": str(exc)[:1000],
                        "cache_inputs": dict(revision_payload),
                    }
                _atomic_json(cache_path, saved)
                return index, saved

            indexed_revisions: dict[int, dict[str, Any]] = {}
            revision_items = list(revision_inputs.items())
            with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
                futures = [executor.submit(revise_one, (index, item)) for index, item in enumerate(revision_items)]
                for future in as_completed(futures):
                    index, result = future.result()
                    indexed_revisions[index] = result
            revision_records = [indexed_revisions[index] for index in range(len(revision_items))]
            revision_response = {
                "chapter_updates": [
                    {
                        **dict(entry),
                        "chapter_id": entry.get("chapter_id") or record.get("chapter_id"),
                        "_complete_chapter_revision": True,
                    }
                    for record in revision_records
                    if record.get("status") == "complete"
                    for entry in self._improvement_entries(record.get("response") or {})
                    if isinstance(entry, Mapping)
                ],
                "chapter_revision_results": revision_records,
            }
            _atomic_json(root / "stages" / "affected_chapter_revision.json", {
                "response": revision_response,
                "chapter_ids": revision_ids,
                "cache_inputs": revision_inputs,
            })
            completed = list(state.get("completed_stages") or [])
            if "affected_chapter_revision" not in completed:
                completed.append("affected_chapter_revision")
            state.update({"completed_stages": completed, "current_stage": "", "status": "in_progress"})
            _atomic_json(root / "RUN_STATE.json", state)
            revision_entries = self._improvement_entries(revision_response)
            if revision_entries:
                improvement = dict(improvement)
                existing_updates = improvement.get("chapter_updates")
                if isinstance(existing_updates, Mapping):
                    existing_updates = [
                        ({**dict(value), "chapter_id": chapter_id} if isinstance(value, Mapping) else {"chapter_id": chapter_id, "adjustment": value})
                        for chapter_id, value in existing_updates.items()
                    ]
                elif not isinstance(existing_updates, list):
                    existing_updates = []
                improvement["chapter_updates"] = [*existing_updates, *revision_entries]
        detail_records = self._apply_improvements(detail_records, improvement)

        # Case enrichment is chapter-scoped because the full source-routing
        # ledger can be much larger than a single model context.  Every chapter
        # still gets a call, but each call receives only its bounded unit slice
        # and the routes relevant to that chapter.
        case_catalog = _case_unit_catalog(detail_records)
        chapter_ids = list(dict.fromkeys(
            _text((record.get("chapter") or {}).get("chapter_id"))
            for record in detail_records
            if isinstance(record, Mapping) and _text((record.get("chapter") or {}).get("chapter_id"))
        ))

        def run_case_groups() -> dict[str, Any]:
            case_root = self.config.output_dir / "stages" / "case_groups"
            case_root.mkdir(parents=True, exist_ok=True)
            chapter_results: list[dict[str, Any]] = []
            all_additions: list[dict[str, Any]] = []

            def merge_additions(additions: Sequence[Mapping[str, Any]]) -> None:
                """Merge batch responses without dropping distinct studies for one unit."""
                by_unit = {
                    _text(item.get("unit_key")): item
                    for item in all_additions
                    if isinstance(item, Mapping) and _text(item.get("unit_key"))
                }
                for raw in additions:
                    if not isinstance(raw, Mapping):
                        continue
                    item = dict(raw)
                    unit_key = _text(item.get("unit_key"))
                    if not unit_key:
                        continue
                    previous = by_unit.get(unit_key)
                    if previous is None:
                        all_additions.append(item)
                        by_unit[unit_key] = item
                        continue
                    existing_studies = previous.setdefault("studies", [])
                    seen_studies = {
                        (
                            "source",
                            _text(study.get("source_handle")) or _text(study.get("paper_id"))
                        ) if (_text(study.get("source_handle")) or _text(study.get("paper_id"))) else (
                            "raw",
                            json.dumps(dict(study), ensure_ascii=False, sort_keys=True, default=_json_default),
                        )
                        for study in existing_studies if isinstance(study, Mapping)
                    }
                    for study in item.get("studies") or []:
                        if not isinstance(study, Mapping):
                            continue
                        source_key = _text(study.get("source_handle")) or _text(study.get("paper_id"))
                        key = (
                            ("source", source_key)
                            if source_key else
                            ("raw", json.dumps(dict(study), ensure_ascii=False, sort_keys=True, default=_json_default))
                        )
                        if key not in seen_studies:
                            existing_studies.append(dict(study))
                            seen_studies.add(key)

            for chapter_id in chapter_ids:
                unit_rows = [row for row in case_catalog if row.get("chapter_id") == chapter_id]
                relevant_routes = [
                    {
                        key: route.get(key)
                        for key in ("source_handle", "paper_title", "chapter_ids", "specific_usable_material", "interpretation_limits")
                        if key in route
                    }
                    for route in routing["source_routes"]
                    if isinstance(route, Mapping) and chapter_id in (route.get("chapter_ids") or [])
                ]
                route_batches = [
                    relevant_routes[index:index + CASE_GROUP_ROUTE_BATCH_SIZE]
                    for index in range(0, len(relevant_routes), CASE_GROUP_ROUTE_BATCH_SIZE)
                ] or [[]]
                chapter_additions: list[dict[str, Any]] = []
                chapter_errors: list[str] = []
                chapter_telemetry: list[dict[str, Any]] = []
                route_handles = [_text(route.get("source_handle")) for route in relevant_routes]

                def update_unit_catalog(additions: Sequence[Mapping[str, Any]]) -> None:
                    """Carry accepted case sources into later batches for this chapter."""
                    by_key = {
                        _text(row.get("unit_key")): row
                        for row in unit_rows
                        if isinstance(row, Mapping) and _text(row.get("unit_key"))
                    }
                    for addition in additions:
                        target = by_key.get(_text(addition.get("unit_key"))) if isinstance(addition, Mapping) else None
                        if not isinstance(target, dict):
                            continue
                        unit = target.get("unit")
                        if not isinstance(unit, dict):
                            unit = {}
                            target["unit"] = unit
                        handles = [
                            _text(value) for value in (unit.get("source_handles") or []) if _text(value)
                        ]
                        prior_key = "case_suggestions" if self.config.planning_revision_enabled else "supporting_studies"
                        prior_studies = unit.setdefault(prior_key, [])
                        for study in addition.get("studies") or []:
                            if not isinstance(study, Mapping):
                                continue
                            handle = _text(study.get("source_handle"))
                            if handle and handle not in handles:
                                handles.append(handle)
                            if handle and not any(_text(item.get("source_handle")) == handle for item in prior_studies):
                                prior_studies.append(dict(study))
                        if handles:
                            unit["source_handles"] = list(dict.fromkeys(handles))

                for batch_index, route_batch in enumerate(route_batches, start=1):
                    batch_id = f"{_safe_id(chapter_id)}__batch_{batch_index:03d}_of_{len(route_batches):03d}"
                    cache_path = case_root / f"{batch_id}.json"
                    batch_handles = [_text(route.get("source_handle")) for route in route_batch]
                    batch_unit_rows = [
                        {**dict(row), "unit": dict(row.get("unit") or {})}
                        for row in unit_rows
                    ]
                    unit_source_signature = [
                        {
                            "unit_key": _text(row.get("unit_key")),
                            "source_handles": [
                                _text(value)
                                for value in ((row.get("unit") or {}).get("source_handles") or [])
                                if _text(value)
                            ],
                        }
                        for row in batch_unit_rows
                    ]
                    batch_material_rows = _case_selection_material_rows(
                        batch_handles, detail_records, pool_rows, self._read_materials)
                    material_signature = hashlib.sha256(json.dumps(
                        batch_material_rows, ensure_ascii=False, sort_keys=True, default=_json_default
                    ).encode("utf-8")).hexdigest()[:16]
                    # The batch consumes the unit TASKS (points, briefs,
                    # cases) and the prompt contract, not just the handle
                    # list: a changed task or contract must not reuse an old
                    # answer.
                    task_signature = _case_unit_task_signature(batch_unit_rows, context={
                        "topic_id": self.config.topic_id,
                        "research_question": topic,
                        "source_routing": route_batch,
                        "source_routing_total": len(relevant_routes),
                        "batch_count": len(route_batches),
                        "pool_sources": len(pool_rows),
                        "review_sources_in_unit_catalog": len(set(re.findall(r"\bP\d{4,}\b", json.dumps(case_catalog, ensure_ascii=False)))),
                        "planning_revision_mode": self.config.planning_revision_enabled,
                    })
                    cached: Mapping[str, Any] | None = None
                    if resume and cache_path.is_file():
                        try:
                            candidate = _read_json(cache_path)
                            if (
                                isinstance(candidate, Mapping)
                                and candidate.get("status") == "complete"
                                and _text(candidate.get("chapter_id")) == chapter_id
                                and int(candidate.get("batch_index") or 0) == batch_index
                                and list(candidate.get("route_handles") or []) == batch_handles
                                and list(candidate.get("unit_source_signature") or []) == unit_source_signature
                                and _text(candidate.get("material_signature")) == material_signature
                                and _text(candidate.get("task_signature")) == task_signature
                                and _text(candidate.get("prompt_contract")) == CASE_GROUPS_PROMPT_CONTRACT
                            ):
                                cached = candidate
                        except (ProgressivePlanError, TypeError, ValueError):
                            cached = None
                    if cached is not None:
                        additions = [
                            dict(item) for item in cached.get("additions") or []
                            if isinstance(item, Mapping) and _text(item.get("unit_key")).startswith(chapter_id + ":")
                        ]
                        chapter_additions.extend(additions)
                        chapter_telemetry.append(dict(cached.get("telemetry") or {}))
                        update_unit_catalog(additions)
                        merge_additions(additions)
                        continue

                    payload = {
                        "call_id": f"case-groups-{_safe_id(chapter_id)}-batch-{batch_index:03d}-of-{len(route_batches):03d}",
                        "topic_id": self.config.topic_id,
                        "research_question": topic,
                        "chapter_id": chapter_id,
                        "unit_catalog": batch_unit_rows,
                        "source_routing": route_batch,
                        "source_routing_total": len(relevant_routes),
                        "source_routing_batch_index": batch_index,
                        "source_routing_batch_count": len(route_batches),
                        "review_sources_in_unit_catalog": len(set(re.findall(r"\bP\d{4,}\b", json.dumps(case_catalog, ensure_ascii=False)))),
                        "pool_sources": len(pool_rows),
                        "breadth_rule": {
                            "goal": "整篇综述对主题、背景、发展与代表案例的充分覆盖",
                            "basis": "当前材料池规模与各单元实际材料",
                            "not_a_quota": "不为数量塞入无关文献，也不把只用少数核心论文当统一规则",
                        },
                        "output_contract": {
                            "chapter_id": chapter_id,
                            "bounded_unit_count": len(unit_rows),
                            "bounded_route_count": len(route_batch),
                            "total_relevant_route_count": len(relevant_routes),
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "one_addition_per_relevant_unit": True,
                        },
                    }
                    # Selection support: the real material of exactly this
                    # batch's routed candidates, so the case model judges from
                    # content instead of inventing experiments for handles.
                    if batch_material_rows:
                        payload["source_materials"] = batch_material_rows
                    if self.config.planning_revision_enabled:
                        payload["planning_revision_mode"] = True
                    try:
                        response_record = _call_record(self.planner, "case_groups", payload)
                        response = _stage_response(response_record)
                        additions = [
                            dict(item) for item in response.get("additions") or []
                            if isinstance(item, Mapping) and _text(item.get("unit_key")).startswith(chapter_id + ":")
                        ]
                        _atomic_json(cache_path, {
                            "status": "complete",
                            "chapter_id": chapter_id,
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "route_handles": batch_handles,
                            "unit_source_signature": unit_source_signature,
                            "material_signature": material_signature,
                            "task_signature": task_signature,
                            "prompt_contract": CASE_GROUPS_PROMPT_CONTRACT,
                            "additions": additions,
                            "telemetry": response_record.get("telemetry") or {},
                        })
                        chapter_additions.extend(additions)
                        chapter_telemetry.append(response_record.get("telemetry") or {})
                        update_unit_catalog(additions)
                        merge_additions(additions)
                    except Exception as exc:
                        error = type(exc).__name__
                        chapter_errors.append(error)
                        _atomic_json(cache_path, {
                            "status": "failed",
                            "chapter_id": chapter_id,
                            "batch_index": batch_index,
                            "batch_count": len(route_batches),
                            "route_handles": batch_handles,
                            "unit_source_signature": unit_source_signature,
                            "material_signature": material_signature,
                            "task_signature": task_signature,
                            "prompt_contract": CASE_GROUPS_PROMPT_CONTRACT,
                            "error": error,
                        })
                result = {
                    "chapter_id": chapter_id,
                    "additions": chapter_additions,
                    "route_count": len(relevant_routes),
                    "route_batches": len(route_batches),
                    "processed_route_handles": route_handles,
                    "telemetry": chapter_telemetry[0] if len(chapter_telemetry) == 1 else chapter_telemetry,
                }
                if chapter_errors:
                    result["errors"] = chapter_errors
                    result["error"] = chapter_errors[0]
                chapter_results.append(result)
            return {
                "additions": all_additions,
                "chapter_results": chapter_results,
                "bounded_per_chapter": True,
                "status": "partial" if any(item.get("error") for item in chapter_results) else "complete",
            }

        case_record = self._stage(
            # Batch caches inside run_case_groups decide which successful calls
            # can be reused.  The stage wrapper must execute the collector on
            # resume so failed batches get retried without repeating successes.
            "case_groups", run_case_groups, resume=False, state=state,
            cache_inputs={
                "unit_catalog": case_catalog,
                "chapter_ids": chapter_ids,
                "source_routing": [
                    {
                        key: route.get(key)
                        for key in ("source_handle", "chapter_ids", "specific_usable_material", "interpretation_limits")
                        if key in route
                    }
                    for route in routing["source_routes"]
                    if isinstance(route, Mapping) and _text(route.get("source_handle"))
                ],
            },
        )
        baseline_detail_records = detail_records
        detail_records = _attach_case_groups(
            detail_records, _stage_response(case_record),
            planning_revision=self.config.planning_revision_enabled,
            candidate_rows=pool_rows,
        )
        if self.config.planning_revision_enabled:
            detail_records, improvement, whole_review_chapters = self._post_case_review(
                root=root,
                topic=topic,
                harmonized=harmonized,
                level1_outline=level1_outline,
                detail_records=detail_records,
                baseline_detail_records=baseline_detail_records,
                case_record=case_record,
                level1_tool_result=level1_tool_result,
                level2_tool_result=level2_tool_result,
                chapter_tool_result=chapter_tool_result,
                editorial_feedback=editorial_feedback,
                original_plan=plan,
                material_theme_inventory=provisional.get("material_theme_inventory") or provisional.get("theme_inventory") or [],
                resume=resume,
                state=state,
            )
        plan_output = self._assemble_final(
            topic=topic,
            plan=plan,
            pool_rows=pool_rows,
            provisional=provisional,
            level1_outline=level1_outline,
            harmonized=harmonized,
            chapters=chapters,
            chapter_records=detail_records,
            level1_tools=level1_tool_result,
            level2_tools=level2_tool_result,
            improvement=improvement,
            chapter_tools=chapter_tool_result,
        )
        plan_output["case_enrichment"] = _stage_response(case_record)
        if plan_output["case_enrichment"].get("status") in {"partial", "not_run_budget"}:
            plan_output["status"] = "initial_draft"
        self._write_final_outputs(plan_output)
        state.update({"status": plan_output["status"], "current_stage": "", "output": str(root / "DETAILED_REVIEW_PLAN.json"), "completed_chapters": len(detail_records)})
        _atomic_json(state_path, state)
        return plan_output

    @staticmethod
    def _compact_tool_feedback(value: Mapping[str, Any]) -> dict[str, Any]:
        def compact(result: Any) -> Any:
            if not isinstance(result, Mapping):
                return result
            keys = (
                "status", "error", "reason", "phase", "gap_id", "gap_question", "substantive_gap_status",
                "fulfillment_judgment", "source_units", "citation_anchors", "materials", "papers",
                 "consumed_paper_ids", "reused_prior_tasks", "deferred_tasks", "requested_unique_papers",
                 "selected_unique_papers", "shared_budget_remaining", "results", "output_dir", "outcome",
                 "local_triage", "retrieval_loop", "owner_content", "tool_materials_by_chapter",
            )
            out = {key: result[key] for key in keys if key in result}
            if isinstance(result.get("materials"), list):
                out["materials"] = [_compact_reading_material(row) for row in result["materials"]]
                # Per-paper results repeat the same complete reading artifacts.
                out["results"] = [{key: row[key] for key in ("paper_id", "status", "reason", "error") if key in row}
                                  for row in result.get("results") or [] if isinstance(row, Mapping)]
            elif isinstance(result.get("results"), list):
                out["results"] = [compact(row) for row in result["results"]]
            if isinstance(result.get("source_units"), list):
                out["source_units"] = [{key: row[key] for key in ("record_identity", "material_depth", "status", "fulfillment_judgment") if key in row}
                                       for row in result["source_units"] if isinstance(row, Mapping)]
            return out
        output: dict[str, Any] = {}
        for key in ("supplement_results", "directed_results"):
            raw = value.get(key)
            if isinstance(raw, list):
                output[key] = [compact(item) for item in raw]
        if "phase" in value:
            output["phase"] = value["phase"]
        if "consumed_paper_ids" in value:
            output["consumed_paper_ids"] = list(value.get("consumed_paper_ids") or [])
        return output

    def _post_case_review(
        self,
        *,
        root: Path,
        topic: str,
        harmonized: Mapping[str, Any],
        level1_outline: Mapping[str, Any],
        detail_records: Sequence[Mapping[str, Any]],
        baseline_detail_records: Sequence[Mapping[str, Any]] | None = None,
        case_record: Mapping[str, Any],
        level1_tool_result: Mapping[str, Any],
        level2_tool_result: Mapping[str, Any],
        chapter_tool_result: Mapping[str, Any],
        editorial_feedback: Mapping[str, Any],
        original_plan: Mapping[str, Any] | None = None,
        material_theme_inventory: Sequence[Mapping[str, Any]] | Sequence[str] = (),
        resume: bool,
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        """Run the opt-in global/revision pass after case enrichment.

        The ordinary mode keeps its historical order.  This narrow path gives
        late case material to the same whole-plan and chapter-owner roles, then
        returns updated plans for final arrangement; it does not create a
        second planner or a writer-side scientific authority.
        """

        case_response = _stage_response(case_record)
        # Re-read authoritative local cards after case work.  The case model
        # contributes pointers and uses; it cannot author or overwrite A/B.
        detail_records = _refresh_local_material_snapshots(detail_records)
        by_chapter_material: dict[str, list[dict[str, Any]]] = {}
        source_materials_by_chapter: dict[str, dict[str, dict[str, Any]]] = {}
        for record in detail_records:
            chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if not chapter_id:
                continue
            source_materials_by_chapter[chapter_id] = {
                _text(item.get("source_handle")): dict(item)
                for item in (record.get("source_materials") or [])
                if isinstance(item, Mapping) and _text(item.get("source_handle"))
            }
        baseline_materials_by_chapter: dict[str, dict[str, dict[str, Any]]] = {}
        for record in (baseline_detail_records or detail_records):
            chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if not chapter_id:
                continue
            baseline_materials_by_chapter[chapter_id] = {
                _text(item.get("source_handle")): dict(item)
                for item in (record.get("source_materials") or [])
                if isinstance(item, Mapping) and _text(item.get("source_handle"))
            }
        # Compare actual material regardless of whether the case editor chose
        # to mention it. Its labels and repeated contribution text are not a
        # gate on a chapter owner's access to changed content.
        case_notes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for addition in case_response.get("additions") or []:
            if not isinstance(addition, Mapping):
                continue
            unit_key = _text(addition.get("unit_key"))
            chapter_id = unit_key.split(":", 1)[0] if ":" in unit_key else _text(addition.get("chapter_id"))
            for study in addition.get("studies") or []:
                if isinstance(study, Mapping):
                    case_notes.setdefault((chapter_id, _text(study.get("source_handle"))), []).append(dict(study))
        for chapter_id, current_sources in source_materials_by_chapter.items():
            prior_sources = baseline_materials_by_chapter.get(chapter_id, {})
            for handle, current_source in current_sources.items():
                current_content = _material_content(current_source)
                prior_content = _material_content(prior_sources.get(handle))
                if current_content == prior_content:
                    continue
                by_chapter_material.setdefault(chapter_id, []).append({
                    "source_handle": handle,
                    "studies": case_notes.get((chapter_id, handle), []),
                    "source_materials": [dict(current_source)],
                    "previous_material": prior_content,
                    "content_signature": _material_content_signature(current_content),
                })

        whole_review_chapters: list[dict[str, Any]] = []
        for row in detail_records:
            chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            whole_review_chapters.append({
                "chapter_id": chapter_id,
                "title": _text(chapter.get("title")),
                "scope": chapter.get("scope"),
                "chapter_plan": row.get("chapter_plan"),
                "source_materials": _chapter_review_source_materials(row),
                "tool_materials": [
                    _tool_material_for_prompt(item)
                    for item in self.tool_materials_by_chapter.get(chapter_id, [])
                    if isinstance(item, Mapping)
                ],
                "late_material_changes": by_chapter_material.get(chapter_id, []),
            })
        whole_inputs = {
            "chapters": whole_review_chapters,
            "original_plan": dict(original_plan or {}),
            "material_theme_inventory": [
                dict(item) if isinstance(item, Mapping) else _text(item)
                for item in (material_theme_inventory or ())
            ],
            "useful_unassigned_navigation": [
                {
                    key: item.get(key)
                    for key in ("source_handle", "paper_id", "title", "material_depth", "assignment", "material_available", "retrieval")
                    if key in item
                }
                for row in detail_records
                for item in ((row.get("candidate_navigation") or {}).get("candidates") or ())
                if isinstance(item, Mapping) and not bool((item.get("assignment") or {}).get("selected_in_chapter"))
            ],
            # Same paper serving several chapters is legitimate when the uses
            # differ; the coordinator sees the actual overlap and judges by
            # role instead of mechanically de-duplicating citations.
            "cross_chapter_source_uses": _cross_chapter_source_uses(detail_records),
            "tool_results_summary": self._compact_tool_feedback({"level1": level1_tool_result, "level2": level2_tool_result}),
            "tool_feedback_scope": {
                "unanswered_need_means": "当前池未取得该内容，仅约束受影响章节本次可写结论；不构成领域缺失或不存在更优路线的证据",
            },
            "editorial_feedback": editorial_feedback,
            "citation_rules": dict(CURRENT_CITATION_RULES),
            "late_material_changes": by_chapter_material,
            "planning_revision_mode": True,
        }
        whole_record = self._stage(
            "whole_plan_improvement",
            lambda: _call_record(self.planner, "whole_plan_improvement", {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": harmonized.get("shared_outline") or level1_outline.get("shared_outline") or {},
                **whole_inputs,
                "do_not_invent_evidence": True,
                "required_behavior": {
                    "review_late_material_content": True,
                    "ordinary_or_supplement_label_does_not_skip_review": True,
                    "return_only_bounded_cross_chapter_changes": True,
                },
            }),
            resume=resume,
            state=state,
            cache_inputs=whole_inputs,
        )
        improvement = _stage_response(whole_record)
        improvement = dict(improvement)
        improvement["late_material_changes"] = by_chapter_material
        # In the opt-in path the whole-plan response is feedback for the
        # chapter owner.  Keeping its scalar suggestions in ``improvement``
        # is useful for the audit packet, but applying them before the owner
        # returns a complete plan would let a failed/no-change owner call
        # silently rewrite the authoritative chapter.
        global_feedback_entries = self._improvement_entries(improvement)
        if global_feedback_entries:
            improvement["owner_revision_feedback"] = [dict(item) for item in global_feedback_entries]
        editorial_entries = self._editorial_feedback_entries(editorial_feedback)
        # Pending case suggestions are a new use of a paper for this chapter,
        # even when the underlying A/B material is unchanged; the owner must
        # judge the proposal against the real content.
        case_suggestions_by_chapter: dict[str, list[dict[str, Any]]] = {}
        for record in detail_records:
            chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if not chapter_id:
                continue
            for index, unit in enumerate(_chapter_units(record.get("chapter_plan") or {})):
                if not isinstance(unit, Mapping):
                    continue
                pending = [dict(item) for item in (unit.get("case_suggestions") or [])
                           if isinstance(item, Mapping)]
                if pending:
                    case_suggestions_by_chapter.setdefault(chapter_id, []).append({
                        "unit_key": f"{chapter_id}:{index + 1}",
                        "unit_point": _text(unit.get("substantive_point") or unit.get("point")),
                        "studies": pending,
                    })
        affected_ids = list(dict.fromkeys([
            *self._affected_chapter_ids(improvement),
            *by_chapter_material,
            *case_suggestions_by_chapter,
            *[_text(item.get("chapter_id")) for item in editorial_entries if _text(item.get("chapter_id"))],
        ]))
        # In the opt-in path every affected chapter is returned to its owner.
        # A short scalar replacement is still a scientific change when it
        # touches thesis/scope; late material is never bypassed by a label.
        revision_ids = affected_ids
        reviewed_chapter_ids: set[str] = set()

        if revision_ids:
            detail_by_id = {
                _text((row.get("chapter") or {}).get("chapter_id")): row
                for row in detail_records
                if isinstance(row, Mapping) and isinstance(row.get("chapter"), Mapping)
            }
            revision_root = root / "stages" / "affected_chapter_revision"
            revision_root.mkdir(parents=True, exist_ok=True)
            state.update({"status": "in_progress", "current_stage": "affected_chapter_revision"})
            _atomic_json(root / "RUN_STATE.json", state)
            feedback_by_chapter: dict[str, list[dict[str, Any]]] = {}
            for item in self._improvement_entries(improvement):
                chapter_id = _text(item.get("chapter_id") or item.get("id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            for item in editorial_entries:
                chapter_id = _text(item.get("chapter_id"))
                if chapter_id:
                    feedback_by_chapter.setdefault(chapter_id, []).append(dict(item))
            review_chapter_by_id = {
                _text(item.get("chapter_id")): item
                for item in whole_review_chapters
                if _text(item.get("chapter_id"))
            }
            revision_inputs = {
                chapter_id: {
                    "topic_id": self.config.topic_id,
                    "research_question": topic,
                    "planning_revision_mode": True,
                    "call_id": f"affected-chapter-revision-{_safe_id(chapter_id)}",
                    "chapter_id": chapter_id,
                    "chapter": (detail_by_id.get(chapter_id) or {}).get("chapter") or {},
                    "chapter_plan": _seed_owner_unit_ids(
                        (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {}, {}
                    )[0],
                    "chapter_feedback": feedback_by_chapter.get(chapter_id) or [],
                    "editorial_feedback_for_chapter": [
                        dict(item) for item in editorial_entries
                        if _text(item.get("chapter_id")) == chapter_id
                    ],
                    # Case-layer selection proposals: uses the owner should
                    # adopt as concrete cases from the real material, or
                    # reject.  A proposal is never the paper's own finding.
                    "case_suggestions": case_suggestions_by_chapter.get(chapter_id) or [],
                    # The owner reviews the plan against the material it must
                    # correct, including compacted paid deep reads; the
                    # whole-plan coordinator keeps its compact shape.
                    "source_materials": _chapter_review_source_materials(
                        detail_by_id.get(chapter_id) or {}, include_deep_read=True),
                    "late_material_changes": by_chapter_material.get(chapter_id, []),
                    "tool_materials": (review_chapter_by_id.get(chapter_id) or {}).get("tool_materials") or [],
                    "candidate_navigation": (detail_by_id.get(chapter_id) or {}).get("candidate_navigation") or {},
                    "candidate_materials": (detail_by_id.get(chapter_id) or {}).get("candidate_materials") or [],
                    "unit_identity_contract": {
                        "existing_unit_ids": _seed_owner_unit_ids(
                            (detail_by_id.get(chapter_id) or {}).get("chapter_plan") or {}, {}
                        )[1],
                        "new_unit_requires_explicit_unit_id": True,
                        "split_or_merge_requires_unit_id_remap": True,
                        "unit_id_remap_shape": "{new_unit_id: [old_unit_id, ...]}",
                    },
                    "required_behavior": {
                        "return_complete_updated_plan": True,
                        "review_original_and_incremental_material_together": True,
                        "preserve_source_handles_and_limits": True,
                        "allow_no_material_change": True,
                        "do_not_add_evidence": True,
                    },
                }
                for chapter_id in revision_ids
                if chapter_id in detail_by_id
            }

            def revise_one(item: tuple[int, tuple[str, Mapping[str, Any]]]) -> tuple[int, dict[str, Any]]:
                index, (chapter_id, revision_payload) = item
                cache_path = revision_root / f"{_safe_id(chapter_id)}.json"
                if resume and cache_path.is_file():
                    try:
                        cached = _read_json(cache_path)
                        if (
                            cached.get("status") == "complete"
                            and cached.get("owner_status") in {"updated", "no_change"}
                            and cached.get("cache_inputs") == revision_payload
                        ):
                            return index, dict(cached)
                    except (ProgressivePlanError, AttributeError):
                        pass
                try:
                    record = _call_record(self.planner, "affected_chapter_revision", revision_payload)
                    response = _stage_response(record)
                    owner_status, updated_plan, unit_id_remap, structural_errors = _classify_owner_response(
                        revision_payload.get("chapter_plan") or {},
                        response,
                        revision_payload.get("source_materials") or [],
                    )
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "complete" if owner_status in {"updated", "no_change"} else "partial",
                        "owner_status": owner_status,
                        "response": response,
                        "updated_plan": updated_plan,
                        "unit_id_remap": unit_id_remap,
                        "structural_errors": structural_errors,
                        "telemetry": record.get("telemetry") or {},
                        "cache_inputs": dict(revision_payload),
                    }
                except Exception as exc:
                    saved = {
                        "chapter_id": chapter_id,
                        "status": "failed",
                        "owner_status": "unresolved",
                        "error": type(exc).__name__,
                        "error_detail": str(exc)[:1000],
                        "cache_inputs": dict(revision_payload),
                    }
                _atomic_json(cache_path, saved)
                return index, saved

            revision_items = list(revision_inputs.items())
            indexed: dict[int, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
                futures = [executor.submit(revise_one, (index, item)) for index, item in enumerate(revision_items)]
                for future in as_completed(futures):
                    index, result = future.result()
                    indexed[index] = result
            revision_records = [indexed[index] for index in range(len(revision_items))]
            reviewed_chapter_ids.update(
                str(item.get("chapter_id"))
                for item in revision_records
                if item.get("status") == "complete"
            )
            revision_entries = [
                {"chapter_id": record.get("chapter_id"), "updated_plan": record["updated_plan"],
                 "unit_id_remap": record.get("unit_id_remap") or {}, "_complete_chapter_revision": True}
                for record in revision_records
                if record.get("status") == "complete" and isinstance(record.get("updated_plan"), Mapping)
            ]
            revision_response = {"chapter_updates": revision_entries, "chapter_revision_results": revision_records}
            _atomic_json(root / "stages" / "affected_chapter_revision.json", {
                "response": revision_response, "chapter_ids": revision_ids, "cache_inputs": revision_inputs,
            })
            improvement["chapter_updates"] = [dict(item) for item in revision_entries]
            improvement["chapter_revision_results"] = [dict(item) for item in revision_records]
            improvement["owner_revision_unresolved"] = [
                str(item.get("chapter_id"))
                for item in revision_records
                if item.get("owner_status") not in {"updated", "no_change"}
            ]

        # Only a successful owner response can update the plan in this mode.
        # Whole-plan scalar entries remain visible as feedback above, while
        # explicit editorial acceptance can be handled by a caller before it
        # reaches this apply step.  This prevents failed/no-change owner
        # revisions from falling back to the original global suggestion.
        if self.config.planning_revision_enabled:
            apply_improvement = {
                "chapter_updates": [
                    dict(item) for item in (improvement.get("chapter_updates") or [])
                    if isinstance(item, Mapping) and item.get("_complete_chapter_revision") is True
                ],
            }
        else:
            apply_improvement = improvement
        final_records = self._apply_improvements(detail_records, apply_improvement)
        # A completed owner review has judged every pending case suggestion:
        # adopted ones live in the owner's plan, rejected ones are simply not
        # there.  The proposals move to a packet-level audit trail so they do
        # not re-trigger the owner and never read as established content.
        final_records = _archive_reviewed_case_suggestions(final_records, reviewed_chapter_ids)
        return final_records, improvement, whole_review_chapters

    @staticmethod
    def _attach_routed_sources(
        proposals: Mapping[str, Any],
        source_routes: Sequence[Mapping[str, Any]],
        shared_outline: Any,
    ) -> dict[str, Any]:
        """Make every semantically routed source survive chapter proposal editing."""
        result = json.loads(json.dumps(proposals, ensure_ascii=False, default=_json_default))
        rows = result.get("chapter_proposals") or []
        if isinstance(rows, Mapping):
            rows = [dict(rows)]
        if not isinstance(rows, list):
            rows = []
        route_handles: dict[str, list[str]] = {}
        for route in source_routes:
            if not isinstance(route, Mapping):
                continue
            handle = _text(route.get("source_handle"))
            for chapter_id in route.get("chapter_ids") or []:
                chapter_id = _text(chapter_id)
                if handle and chapter_id:
                    route_handles.setdefault(chapter_id, []).append(handle)
        existing_by_id = {
            _text(row.get("chapter_id") or row.get("id")): dict(row)
            for row in rows if isinstance(row, Mapping) and _text(row.get("chapter_id") or row.get("id"))
        }
        for chapter in _outline_chapter_rows(shared_outline):
            chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
            if chapter_id and chapter_id not in existing_by_id:
                existing_by_id[chapter_id] = {
                    "chapter_id": chapter_id,
                    "title": _text(chapter.get("title") or chapter.get("chapter_title") or chapter_id),
                    "purpose": _text(chapter.get("purpose") or chapter.get("scope")),
                    "proposal_completion_note": "Expanded from the full-pool source-routing ledger.",
                }
        output_rows: list[dict[str, Any]] = []
        for chapter_id, row in existing_by_id.items():
            existing_handles = row.get("source_handles") or []
            if isinstance(existing_handles, str):
                existing_handles = [existing_handles]
            routed = route_handles.get(chapter_id, [])
            excluded_rows = row.get("excluded_source_handles") or []
            if any(isinstance(item, Mapping) for item in excluded_rows):
                row["source_exclusion_notes"] = excluded_rows
            row["excluded_source_handles"] = _source_values(excluded_rows)
            excluded = set(row["excluded_source_handles"])
            row["source_handles"] = [handle for handle in dict.fromkeys([*[_text(item) for item in existing_handles if _text(item)], *routed]) if handle not in excluded]
            output_rows.append(row)
        result["chapter_proposals"] = output_rows
        return result

    @staticmethod
    def _harmonized_chapters(harmonized: Mapping[str, Any], proposals: Mapping[str, Any]) -> list[dict[str, Any]]:
        proposal_rows = proposals.get("chapter_proposals") or proposals.get("chapters") or []
        source_rows = harmonized.get("chapters") or harmonized.get("harmonized_chapters") or []
        by_id = {str(row.get("chapter_id") or ""): dict(row) for row in source_rows if isinstance(row, Mapping)}
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in proposal_rows:
            if not isinstance(raw, Mapping):
                continue
            proposal = dict(raw)
            chapter_id = _text(proposal.get("chapter_id") or proposal.get("id"))
            if not chapter_id or chapter_id in seen:
                continue
            seen.add(chapter_id)
            shared = by_id.get(chapter_id)
            if shared:
                merged = {**proposal, **shared}
                proposal_sources = proposal.get("source_ids") or proposal.get("paper_ids") or []
                shared_sources = shared.get("source_ids") or shared.get("paper_ids") or []
                merged["source_ids"] = list(dict.fromkeys(
                    [_text(item) for item in [*proposal_sources, *shared_sources] if _text(item)]
                ))
                proposal_handles = proposal.get("source_handles") or []
                shared_handles = shared.get("source_handles") or []
                merged["source_handles"] = list(dict.fromkeys(
                    [_text(item) for item in [*proposal_handles, *shared_handles] if _text(item)]
                ))
            else:
                merged = proposal
            excluded_ids = set(_source_values(merged.get("excluded_source_ids")))
            excluded_handles = set(_source_values(merged.get("excluded_source_handles")))
            merged["source_ids"] = [item for item in merged.get("source_ids") or [] if item not in excluded_ids]
            merged["source_handles"] = [item for item in merged.get("source_handles") or [] if item not in excluded_handles]
            merged["chapter_id"] = chapter_id
            merged["title"] = _text(merged.get("title") or merged.get("chapter_title") or chapter_id)
            merged["source_ids"] = list(dict.fromkeys(_text(x) for x in (merged.get("source_ids") or merged.get("paper_ids") or []) if _text(x)))
            output.append(merged)
        if not output:
            raise ProgressivePlanError("chapter_proposals_empty")
        return output

    @staticmethod
    def _result_paper_materials(tool_result: Mapping[str, Any]) -> list[dict[str, Any]]:
        papers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("supplement_results", "directed_results"):
            pending = list(tool_result.get(key) or [])
            while pending:
                group = pending.pop(0)
                if not isinstance(group, Mapping):
                    continue
                for item in group.get("materials") or group.get("papers") or []:
                    if isinstance(item, Mapping):
                        paper_id = _text(item.get("paper_id") or item.get("canonical_paper_id"))
                        if paper_id and paper_id in seen:
                            continue
                        if paper_id:
                            seen.add(paper_id)
                        papers.append(dict(item))
                pending.extend(group.get("results") or [])
        return papers

    def _chapter_details(
        self,
        *,
        chapters: Sequence[Mapping[str, Any]],
        shared_outline: Any,
        topic: str,
        candidates: Mapping[str, Mapping[str, Any]],
        level1_tools: Mapping[str, Any],
        level2_tools: Mapping[str, Any],
        resume: bool,
        state: dict[str, Any],
        chapter_tools: Mapping[str, Any] | None = None,
        tool_materials_by_chapter: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        candidate_pool: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        root = self.config.output_dir / "stages" / "chapters"
        root.mkdir(parents=True, exist_ok=True)
        routing_path = self.config.output_dir / "stages" / "source_routing_summary.json"
        routing_rows = (_read_json(routing_path).get("source_routes") or []) if routing_path.is_file() else []
        route_by_handle = {row.get("source_handle"): row for row in routing_rows}
        l1_materials = self._result_paper_materials(level1_tools)
        l2_materials = self._result_paper_materials(level2_tools)
        # Chapter-phase directed reads are real paid readings for this
        # chapter's own sources; harvesting only level1/level2 left them out of
        # every packet's deep_read_material.
        chapter_phase_materials = self._result_paper_materials(chapter_tools or {})
        all_materials = {str(row.get("paper_id")): row for row in [*l1_materials, *l2_materials, *chapter_phase_materials] if _text(row.get("paper_id"))}
        for row in self.prior_readings:
            if _text(row.get("paper_id")):
                all_materials.setdefault(_text(row.get("paper_id")), dict(row))
        for paper_id, row in self._read_materials.items():
            all_materials.setdefault(paper_id, row)

        def nested_supplement_groups(groups: Any):
            pending = list(groups or [])
            while pending:
                group = pending.pop(0)
                if not isinstance(group, Mapping):
                    continue
                pending.extend(group.get("results") or [])
                if group.get("gap_id") or group.get("fulfillment_judgment"):
                    yield group

        def chapter_payload(chapter: Mapping[str, Any], index: int) -> tuple[str, dict[str, Any]]:
            chapter_id = _text(chapter.get("chapter_id"))
            source_ids = chapter.get("source_ids") or []
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            source_materials = []
            for source_id in source_ids:
                candidate = candidates.get(str(source_id))
                if candidate is None:
                    material = all_materials.get(str(source_id))
                    if material:
                        source_materials.append({"paper_id": str(source_id), "source_role": "supplement_or_deep_read", "material": material})
                    continue
                card = _card_for_candidate(candidate)
                a = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
                review_planning = card.get("review_planning") if isinstance(card.get("review_planning"), Mapping) else {}
                planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
                b_record = candidate.get("_b_summary") if isinstance(candidate.get("_b_summary"), Mapping) else {}
                source_materials.append({
                    "source_handle": _text(candidate.get("_source_handle")),
                    "paper_id": str(source_id),
                    "card_path": _text(candidate.get("card_path")),
                    "title": _text((planning.get("paper_identity") or {}).get("title") if isinstance(planning.get("paper_identity"), Mapping) else candidate.get("title")),
                    "doi": _text((planning.get("paper_identity") or {}).get("doi") if isinstance(planning.get("paper_identity"), Mapping) else candidate.get("doi")),
                    "year": _text((planning.get("paper_identity") or {}).get("year") if isinstance(planning.get("paper_identity"), Mapping) else candidate.get("year")),
                    "material_depth": _text(b_record.get("declared_content_depth")),
                    "study_summary_A": dict(a),
                    "review_planning_B": dict(review_planning or {
                        "planning_summary": planning.get("planning_summary"),
                        "facet_contributions": planning.get("facet_contributions"),
                        "scope_interpretation_cautions": planning.get("scope_interpretation_cautions"),
                    }),
                    "supplement_gap_material": candidate.get("supplement_gap_material") or {},
                    "supplement_gap_materials": candidate.get("supplement_gap_materials") or [],
                    "deep_read_material": all_materials.get(str(source_id), {}),
                })
            related_ids = set(str(item) for item in source_ids)
            related_tools = []
            for result in (level1_tools, level2_tools, chapter_tools or {}):
                for group in nested_supplement_groups(result.get("supplement_results")):
                    chapters_for_gap = set(str(item) for item in group.get("chapter_ids") or [])
                    if chapters_for_gap and chapter_id not in chapters_for_gap:
                        continue
                    judgment = group.get("fulfillment_judgment")
                    if isinstance(judgment, Mapping):
                        judgment = {
                            key: judgment[key]
                            for key in (
                                "status", "outcome", "substantive_gap_status", "material_ready",
                                "useful_material", "remaining_gap", "outline_action", "evidence", "limitations",
                            )
                            if key in judgment
                        }
                    related_tools.append({
                        "gap_id": _text(group.get("gap_id")),
                        "chapter_ids": list(group.get("chapter_ids") or []),
                        "status": _text(group.get("status")),
                        "substantive_gap_status": _text(group.get("substantive_gap_status")),
                        "outcome": _text(group.get("outcome")),
                        "fulfillment_judgment": judgment if isinstance(judgment, Mapping) else {},
                    })
                for group in result.get("directed_results") or []:
                    if not isinstance(group, Mapping):
                        continue
                    for material in group.get("materials") or []:
                        if isinstance(material, Mapping) and _text(material.get("paper_id")) in related_ids:
                            related_tools.append({"paper_id": _text(material.get("paper_id")), "material": dict(material)})
            payload = {
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": shared_outline,
                "chapter": dict(chapter),
                "position": {"index": index + 1, "count": len(chapters)},
                "next_chapter_title": _text(chapters[index + 1].get("title")) if index + 1 < len(chapters) else "",
                "previous_chapter_title": _text(chapters[index - 1].get("title")) if index > 0 else "",
                "source_materials": source_materials,
                "relevant_tool_feedback": related_tools,
                "citation_rules": dict(CURRENT_CITATION_RULES),
            }
            if self.config.planning_revision_enabled:
                navigation = build_candidate_navigation(
                    chapter=chapter,
                    candidates=candidate_pool or candidates,
                    source_routes=routing_rows,
                    research_question=topic,
                    index_path=self.config.local_material_index_path,
                    candidate_limit=self.config.planning_revision_candidate_limit,
                    passages_per_paper=self.config.planning_revision_passages_per_paper,
                    passage_chars=self.config.planning_revision_passage_chars,
                    deep_material_by_paper=all_materials,
                )
                payload["candidate_navigation"] = navigation
                payload["candidate_materials"] = list(navigation.get("candidate_materials") or [])
            return chapter_id, payload

        def build_one(item: tuple[int, Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
            index, chapter = item
            chapter_id, payload = chapter_payload(chapter, index)
            cached = root / (_safe_id(chapter_id) + ".json")
            if resume and cached.is_file():
                cached_packet = dict(_read_json(cached))
                expected_materials = [_tool_material_for_prompt(item) for item in (tool_materials_by_chapter or {}).get(chapter_id, [])]
                if (
                    cached_packet.get("_adaptive_input_materials") == expected_materials
                    and cached_packet.get("source_materials") == payload["source_materials"]
                    and (not self.config.planning_revision_enabled or (
                        cached_packet.get("candidate_navigation") == payload.get("candidate_navigation")
                        and cached_packet.get("candidate_materials") == payload.get("candidate_materials")
                    ))
                ):
                    return index, cached_packet
            def projected_source(item: Mapping[str, Any]) -> dict[str, Any]:
                route = route_by_handle.get(item.get("source_handle"))
                if self.config.planning_revision_enabled:
                    # The chapter owner's research understanding comes from the
                    # real A/B material of its assigned sources.  The routing
                    # note stays available as separate selection advice and
                    # never replaces the source's own account.
                    row = {
                        "source_handle": item.get("source_handle"),
                        "title": item.get("title"),
                        "material_depth": item.get("material_depth"),
                        "study_summary_A": item.get("study_summary_A") or {},
                        "planning_material": item.get("review_planning_B"),
                        "supplement_material": item.get("supplement_gap_material") or {},
                        "supplement_materials": [
                            dict(supplement) for supplement in (item.get("supplement_gap_materials") or [])
                            if isinstance(supplement, Mapping)
                        ],
                        "deep_read_material": _compact_reading_material(item.get("deep_read_material") or {}),
                    }
                    if isinstance(route, Mapping) and route:
                        row["routing_note"] = {
                            key: route[key]
                            for key in ("chapter_ids", "specific_usable_material", "interpretation_limits", "reason")
                            if key in route
                        }
                    return row
                return {
                    "source_handle": item.get("source_handle"), "title": item.get("title"),
                    "material_depth": item.get("material_depth"),
                    "planning_material": route or item.get("review_planning_B"),
                    "supplement_material": item.get("supplement_gap_material") or {},
                    "deep_read_material": _compact_reading_material(item.get("deep_read_material") or {}),
                }

            model_payload = {**payload, "source_materials": [
                projected_source(item)
                for item in payload["source_materials"]
            ], "relevant_tool_feedback": [item for item in payload["relevant_tool_feedback"] if not item.get("material")]}
            model_payload["chapter"] = {key: value for key, value in chapter.items()
                                        if key not in {"source_ids", "paper_ids", "source_handles"}}
            if self.config.planning_revision_enabled:
                model_payload["candidate_navigation"] = payload.get("candidate_navigation") or {}
                model_payload["candidate_materials"] = payload.get("candidate_materials") or []
                model_payload["planning_revision_mode"] = True
                # An unanswered retrieval need bounds what this chapter may
                # currently conclude; it is never evidence that the field
                # lacks the research.  Stated next to the tool feedback it
                # qualifies, so the owner reads it as scope, not as absence.
                model_payload["tool_feedback_scope"] = {
                    "unanswered_need_means": "当前池未取得该内容，仅约束本次可写结论；不构成领域缺失或不存在更优路线的证据",
                }
                model_payload["required_behavior"] = {
                    "inspect_relevant_unassigned_candidates": True,
                    "use_candidate_materials_when_relevant": True,
                    "do_not_treat_local_passages_as_deep_read": True,
                    "do_not_force_use": True,
                }
            # Work order 05: material gathered by the shared retrieval queue for
            # this chapter must be part of the chapter's own input, so the plan
            # can change because of it instead of only gaining a citation.
            chapter_tool_materials = [
                _tool_material_for_prompt(item)
                for item in (tool_materials_by_chapter or {}).get(_text(chapter.get("chapter_id")), [])
            ]
            if chapter_tool_materials:
                model_payload["new_tool_materials"] = chapter_tool_materials
                model_payload["required_behavior"] = {
                    **(model_payload.get("required_behavior") or {}),
                    "use_new_tool_materials": True,
                    "state_what_changed": True,
                    "keep_conditions_and_limits": True,
                    "do_not_only_append_citations": True,
                }
            record = _call_record(self.planner, "chapter_details", model_payload)
            response = _stage_response(record)
            chapter_plan = response.get("chapter_plan") if isinstance(response.get("chapter_plan"), Mapping) else response
            packet_source_materials = [dict(item) for item in payload["source_materials"]]
            existing_handles = {_text(item.get("source_handle")) for item in packet_source_materials}
            for item in payload.get("candidate_materials") or []:
                if not isinstance(item, Mapping):
                    continue
                handle = _text(item.get("source_handle"))
                if handle and handle not in existing_handles:
                    packet_source_materials.append({**dict(item), "source_role": "candidate_navigation"})
                    existing_handles.add(handle)
            packet = {
                "schema_version": SCHEMA_VERSION,
                "topic_id": self.config.topic_id,
                "research_question": topic,
                "shared_outline": shared_outline,
                "chapter": dict(chapter),
                "position": payload["position"],
                "adjacent_chapters": {"previous": payload["previous_chapter_title"], "next": payload["next_chapter_title"]},
                "chapter_plan": dict(chapter_plan),
                "source_materials": packet_source_materials,
                "source_identity_map": {
                    _text(item.get("source_handle")): {
                        "paper_id": _text(item.get("paper_id")),
                        "title": _text(item.get("title")),
                        "doi": _text(item.get("doi")),
                        "year": _text(item.get("year")),
                    }
                    for item in packet_source_materials
                    if _text(item.get("source_handle"))
                },
                "relevant_tool_feedback": payload["relevant_tool_feedback"],
                **({
                    "candidate_navigation": payload.get("candidate_navigation") or {},
                    "candidate_materials": payload.get("candidate_materials") or [],
                } if self.config.planning_revision_enabled else {}),
                "citation_namespace_rule": dict(CURRENT_CITATION_RULES),
                "writer_handoff": {
                    "research_already_done": True,
                    "do_not_research_again": True,
                    "cite_only_supplied_source_handles": True,
                    "preserve_study_types_conditions_and_limitations": True,
                    "write_connected_review_prose_not_question_answer_blocks": True,
                },
                "planner_telemetry": record.get("telemetry") or {},
                "_adaptive_input_materials": chapter_tool_materials,
            }
            _atomic_json(cached, packet)
            md = render_writer_packet_markdown(packet)
            (cached.with_suffix(".md")).write_text(md, encoding="utf-8", newline="\n")
            return index, packet

        indexed: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.chapter_workers)) as executor:
            futures = [executor.submit(build_one, (index, chapter)) for index, chapter in enumerate(chapters)]
            for future in as_completed(futures):
                index, packet = future.result()
                indexed[index] = packet
        records = [indexed[index] for index in sorted(indexed)]
        state["completed_chapters"] = [str(packet.get("chapter", {}).get("chapter_id")) for packet in records]
        _atomic_json(self.config.output_dir / "RUN_STATE.json", state)
        return records

    @staticmethod
    def _partial_result(topic: str, plan: Mapping[str, Any], provisional: Mapping[str, Any], level1_outline: Mapping[str, Any], level1_tools: Mapping[str, Any], level: str, *, harmonized: Mapping[str, Any] | None = None, level2_tools: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "partial",
            "completed_through": level,
            "research_question": topic,
            "review_title": _text(provisional.get("review_title") or topic),
            "original_plan": dict(plan),
            "provisional_scope": dict(provisional),
            "shared_outline": (harmonized or {}).get("shared_outline") or level1_outline.get("shared_outline") or {},
            "level1_tool_results": level1_tools,
            "level2_tool_results": dict(level2_tools or {}),
        }

    @staticmethod
    def _source_index(chapters: Sequence[Mapping[str, Any]], pool_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        assigned: dict[str, list[str]] = {}
        for chapter in chapters:
            for paper_id in chapter.get("source_ids") or []:
                assigned.setdefault(str(paper_id), []).append(_text(chapter.get("chapter_id")))
        b_by_id = {str(row.get("_paper_id")): row.get("_b_summary") or {} for row in pool_rows}
        return {
            paper_id: {
                "paper_id": paper_id,
                "title": _text((b_by_id.get(paper_id) or {}).get("title")),
                "chapter_ids": list(dict.fromkeys(chapter_ids)),
                "selected": bool(chapter_ids),
                "selection_status": "assigned_to_chapter" if chapter_ids else "screened_not_selected",
            }
            for paper_id, chapter_ids in {**{key: assigned.get(key, []) for key in b_by_id}, **{key: value for key, value in assigned.items() if key not in b_by_id}}.items()
        }

    @staticmethod
    def _source_identity_map(pool_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """Keep the compact planner handles connected to canonical identities."""
        output: dict[str, dict[str, Any]] = {}
        for row in pool_rows:
            handle = _text(row.get("_source_handle"))
            paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
            if not handle or not paper_id:
                continue
            planning = row.get("planning_view") if isinstance(row.get("planning_view"), Mapping) else {}
            identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
            output[handle] = {
                "source_handle": handle,
                "paper_id": paper_id,
                "title": _text(identity.get("title") or row.get("title")),
                "doi": _text(identity.get("doi") or row.get("doi")),
                "year": _text(identity.get("year") or row.get("year")),
                "card_path": _text(row.get("card_path")),
            }
        return output

    @staticmethod
    def _improvement_entries(improvement: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Normalize the several model response shapes used for chapter edits."""
        entries: list[dict[str, Any]] = []
        for key in ("updated_chapter_plans", "chapter_updates", "affected_chapters", "cross_chapter_adjustments"):
            raw = improvement.get(key)
            if isinstance(raw, Mapping):
                raw = [
                    ({**dict(value), "chapter_id": key_id} if isinstance(value, Mapping) else {"chapter_id": key_id, "adjustment": value})
                    for key_id, value in raw.items()
                ]
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                continue
            for item in raw:
                if isinstance(item, Mapping):
                    entries.append(dict(item))
        return entries

    @staticmethod
    def _improvement_plan(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return a chapter plan payload, including direct plan-shaped entries."""
        for key in ("chapter_plan", "updated_plan", "updated_chapter_plan", "changes"):
            plan = item.get(key)
            if isinstance(plan, Mapping):
                return plan
        if any(key in item for key in ("units", "substantive_units", "ordered_substantive_units")):
            return item
        return None

    @staticmethod
    def _contains_unit_structure(value: Mapping[str, Any]) -> bool:
        """Unit lists require a complete chapter rewrite, never a shallow patch."""
        if value.get("_complete_chapter_revision") is True:
            return False
        if any(key in value for key in ("units", "substantive_units", "ordered_substantive_units")):
            return True
        target_paths = value.get("target_paths") or []
        if isinstance(target_paths, str):
            target_paths = [target_paths]
        if any(
            ".units" in _text(path)
            or "units[" in _text(path)
            or _text(path).endswith("units")
            for path in target_paths
        ):
            return True
        for key in ("chapter_plan", "updated_plan", "updated_chapter_plan", "changes"):
            nested = value.get(key)
            if isinstance(nested, Mapping) and ProgressiveReviewPlanner._contains_unit_structure(nested):
                return True
        return False

    @staticmethod
    def _editorial_feedback_entries(feedback: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """Normalize chapter-scoped human/editorial corrections for revision."""
        if not isinstance(feedback, Mapping):
            return []
        entries: list[dict[str, Any]] = []

        def add(raw: Any, chapter_hint: str = "") -> None:
            if isinstance(raw, Mapping):
                item = dict(raw)
                chapter_id = _text(item.get("chapter_id") or item.get("id") or chapter_hint)
                if chapter_id:
                    item["chapter_id"] = chapter_id
                    entries.append(item)
                return
            if isinstance(raw, str) and chapter_hint:
                entries.append({"chapter_id": chapter_hint, "feedback": raw})
                return
            if isinstance(raw, list):
                for item in raw:
                    add(item, chapter_hint)

        for key in ("edits", "chapter_updates", "updated_chapter_plans"):
            raw = feedback.get(key)
            if isinstance(raw, Mapping):
                for chapter_id, value in raw.items():
                    add(value, _text(chapter_id))
            else:
                add(raw)
        for key in ("chapters", "chapter_feedback"):
            raw = feedback.get(key)
            if isinstance(raw, Mapping):
                for chapter_id, value in raw.items():
                    if isinstance(value, list):
                        for item in value:
                            add(item, _text(chapter_id))
                    else:
                        add(value, _text(chapter_id))
            else:
                add(raw)
        if feedback.get("chapter_id"):
            add(feedback)
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for item in entries:
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default)
            if encoded not in seen:
                output.append(item)
                seen.add(encoded)
        return output

    @classmethod
    def _affected_chapter_ids(cls, improvement: Mapping[str, Any]) -> list[str]:
        ids: list[str] = []
        for key in ("affected_chapter_ids", "chapter_ids"):
            values = improvement.get(key) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, Sequence):
                ids.extend(_text(value) for value in values if _text(value))
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id") or item.get("id"))
            if chapter_id:
                ids.append(chapter_id)
            nested = item.get("chapter_ids") or item.get("affected_chapter_ids") or []
            if isinstance(nested, str):
                nested = [nested]
            if isinstance(nested, Sequence):
                ids.extend(_text(value) for value in nested if _text(value))
        return list(dict.fromkeys(ids))

    @classmethod
    def _concrete_improvement_ids(cls, improvement: Mapping[str, Any]) -> set[str]:
        concrete: set[str] = set()
        requires_revision: set[str] = set()
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id") or item.get("id"))
            plan = cls._improvement_plan(item)
            if not chapter_id:
                continue
            if cls._contains_unit_structure(item):
                requires_revision.add(chapter_id)
            elif isinstance(plan, Mapping) and plan:
                concrete.add(chapter_id)
        return concrete - requires_revision

    @classmethod
    def _apply_improvements(cls, chapters: Sequence[Mapping[str, Any]], improvement: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Apply only concrete whole-plan edits targeted to a known chapter."""
        edits: dict[str, tuple[Mapping[str, Any], bool]] = {}
        for item in cls._improvement_entries(improvement):
            chapter_id = _text(item.get("chapter_id"))
            patch = item.get("changes")
            plan = cls._improvement_plan(item)
            # A unit list is a complete chapter structure.  The corresponding
            # entry remains feedback for affected_chapter_revision and must not
            # replace the existing units during this light pass.
            if chapter_id and isinstance(plan, Mapping) and not cls._contains_unit_structure(item):
                edits[chapter_id] = (plan, isinstance(patch, Mapping) and plan is patch)
        output: list[dict[str, Any]] = []
        for row in chapters:
            packet = dict(row)
            chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
            chapter_id = _text(chapter.get("chapter_id"))
            if chapter_id in edits:
                plan, is_patch = edits[chapter_id]
                existing = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
                packet["chapter_plan"] = {**dict(existing), **dict(plan)}
                packet["whole_plan_edit_applied"] = True
            output.append(packet)
        return output

    def _bind_tool_material_source_handles(self, pool_rows: Sequence[Mapping[str, Any]]) -> None:
        """Resolve supplement identities to program-managed pool handles."""
        by_paper = {_text(row.get("_paper_id")): _text(row.get("_source_handle"))
                    for row in pool_rows if _text(row.get("_paper_id")) and _text(row.get("_source_handle"))}
        by_unit = {_text(row.get("supplement_source_unit_id")): _text(row.get("_source_handle"))
                   for row in pool_rows if _text(row.get("supplement_source_unit_id")) and _text(row.get("_source_handle"))}
        for rows in self.tool_materials_by_chapter.values():
            for material in rows:
                if not isinstance(material, Mapping):
                    continue
                sources = material.get("sources")
                if not isinstance(sources, list):
                    continue
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    identity = source.get("record_identity") if isinstance(source.get("record_identity"), Mapping) else {}
                    for key in ("paper_id", "canonical_paper_id", "title", "doi", "year"):
                        if not source.get(key) and identity.get(key):
                            source[key] = identity[key]
                    current = _text(source.get("source_handle"))
                    paper_id = _text(source.get("paper_id") or source.get("canonical_paper_id"))
                    unit_id = _text(source.get("source_unit_id") or source.get("supplement_source_unit_id"))
                    source["source_handle"] = by_paper.get(paper_id) or by_unit.get(unit_id) or current

    def _assemble_final(self, *, topic: str, plan: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]], provisional: Mapping[str, Any], level1_outline: Mapping[str, Any], harmonized: Mapping[str, Any], chapters: Sequence[Mapping[str, Any]], chapter_records: Sequence[Mapping[str, Any]], level1_tools: Mapping[str, Any], level2_tools: Mapping[str, Any], improvement: Mapping[str, Any], chapter_tools: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Writer handoff follows the final chapter plans, not superseded proposal text.
        chapter_records = json.loads(json.dumps(chapter_records, ensure_ascii=False, default=_json_default))
        all_tool_materials = [
            item for rows in self.tool_materials_by_chapter.values()
            for item in rows if isinstance(item, Mapping)
        ]
        if all_tool_materials:
            chapter_records = merge_tool_materials_into_packets(chapter_records, all_tool_materials)
        identity_map = self._source_identity_map(pool_rows)
        for packet in chapter_records:
            packet_map = dict(packet.get("source_identity_map") or {})
            for source in [*(packet.get("source_materials") or []),
                           *[source for item in packet.get("tool_materials") or []
                             for source in (item.get("sources") or []) if isinstance(source, Mapping)]]:
                if not isinstance(source, Mapping):
                    continue
                handle = _text(source.get("source_handle"))
                if not handle:
                    continue
                packet_map.setdefault(handle, {
                    "source_handle": handle,
                    "paper_id": _text(source.get("paper_id")),
                    "title": _text(source.get("title")),
                    "doi": _text(source.get("doi")),
                    "year": _text(source.get("year")),
                })
                if handle in identity_map:
                    packet_map[handle] = dict(identity_map[handle])
            packet["source_identity_map"] = packet_map
        shared_outline = []
        for packet in chapter_records:
            packet.pop("_adaptive_input_materials", None)
            detail = packet.get("chapter_plan") or {}
            chapter = packet["chapter"]
            chapter["title"] = detail.get("title") or chapter.get("title")
            chapter["purpose"] = detail.get("reader_objective") or chapter.get("purpose")
            chapter["scope"] = detail.get("thesis") or chapter.get("scope")
            chapter["substantive_threads"] = [unit.get("substantive_point") or unit.get("point") for unit in _chapter_units(detail) if isinstance(unit, Mapping)]
            shared_outline.append({key: chapter.get(key) for key in ("chapter_id", "title", "purpose", "scope")})
        for packet in chapter_records:
            packet["shared_outline"] = shared_outline
        source_index = self._source_index(chapters, pool_rows)
        unique_selected = {paper_id for row in source_index.values() if row.get("selected") for paper_id in [row.get("paper_id")]}
        all_deep_ids = list(dict.fromkeys([*_all_directed_ids(level1_tools), *_all_directed_ids(level2_tools), *_all_directed_ids(chapter_tools or {})]))
        finalized_shared_scope = improvement.get("finalized_shared_scope")
        if not isinstance(finalized_shared_scope, Mapping):
            finalized_shared_scope = improvement.get("final_shared_scope")
        if not isinstance(finalized_shared_scope, Mapping):
            finalized_shared_scope = harmonized.get("shared_scope") or level1_outline.get("shared_scope") or {}
        if not isinstance(finalized_shared_scope, Mapping):
            finalized_shared_scope = {"statement": _text(finalized_shared_scope)} if _text(finalized_shared_scope) else {}
        finalized_notes = improvement.get("finalized_harmonization_notes")
        if finalized_notes is None:
            finalized_notes = improvement.get("harmonization_notes")
        if finalized_notes is None:
            finalized_notes = harmonized.get("harmonization_notes") or []
        if isinstance(finalized_notes, (str, bytes)):
            finalized_notes = [str(finalized_notes)]
        elif not isinstance(finalized_notes, list):
            finalized_notes = list(finalized_notes or []) if isinstance(finalized_notes, Sequence) else [finalized_notes]
        for packet in chapter_records:
            # The current program contract is authoritative even when an old
            # cached packet carries stale editorial instructions.
            packet["citation_namespace_rule"] = dict(CURRENT_CITATION_RULES)
        final = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "topic_id": self.config.topic_id,
            "research_question": topic,
            "review_title": _text(provisional.get("review_title") or harmonized.get("review_title") or plan.get("review_title") or topic),
            "original_plan": dict(plan),
            "provisional_scope": dict(provisional),
            "shared_scope": dict(finalized_shared_scope),
            "shared_outline": shared_outline,
            "harmonization_notes": finalized_notes,
            "citation_rules": dict(CURRENT_CITATION_RULES),
            "chapters": [dict(row) for row in chapter_records],
            "candidate_screening": {
                "pool_rows_read": len(pool_rows),
                "unique_sources_assigned": len(unique_selected),
                "candidate_rows": list(source_index.values()),
                "reference_count_is_not_a_gate": True,
            },
            "source_identity_map": self._source_identity_map(pool_rows),
            "planning_tool_results": {"level1": level1_tools, "level2": level2_tools, "chapters": dict(chapter_tools or {})},
            "deep_read_budget": {
                "configured_unique_paper_limit": self.config.shared_deep_read_budget,
                "unique_papers_consumed": len(set(all_deep_ids)),
                "paper_ids": all_deep_ids,
            },
            "whole_plan_improvement": dict(improvement),
            "writer_packets": [
                {
                    "chapter_id": _text(row.get("chapter", {}).get("chapter_id")),
                    "json_path": f"writer_packets/{_safe_id(row.get('chapter', {}).get('chapter_id'))}.json",
                    "markdown_path": f"writer_packets/{_safe_id(row.get('chapter', {}).get('chapter_id'))}.md",
                    "source_material_count": len(row.get("source_materials") or []),
                }
                for row in chapter_records
            ],
        }
        unresolved_owner = [
            _text(item) for item in (improvement.get("owner_revision_unresolved") or []) if _text(item)
        ]
        if unresolved_owner:
            final["status"] = "partial"
            final["owner_revision_unresolved"] = list(dict.fromkeys(unresolved_owner))
        return final

    def _write_final_outputs(self, plan_output: Mapping[str, Any]) -> None:
        root = self.config.output_dir
        packets = plan_output.get("chapters") or []
        packet_root = root / "writer_packets"
        packet_root.mkdir(parents=True, exist_ok=True)
        for packet in packets:
            packet["chapter_plan"] = _supply_local_citation_identity(packet.get("chapter_plan") or {}, packet.get("source_identity_map") or {})
            chapter_id = _safe_id((packet.get("chapter") or {}).get("chapter_id"))
            _atomic_json(packet_root / f"{chapter_id}.json", packet)
            (packet_root / f"{chapter_id}.md").write_text(render_writer_packet_markdown(packet), encoding="utf-8", newline="\n")
        _atomic_json(root / "DETAILED_REVIEW_PLAN.json", plan_output)
        (root / "DETAILED_REVIEW_PLAN.md").write_text(render_plan_markdown(plan_output), encoding="utf-8", newline="\n")


def _chapter_units(plan: Mapping[str, Any]) -> list[Any]:
    return next((plan[key] for key in ("units", "substantive_units", "ordered_substantive_units") if isinstance(plan.get(key), list)), [])


def _unit_study_records(value: Any) -> list[dict[str, Any]]:
    """Read study entries across the planner's different case field names.

    Pending case-layer suggestions (``case_suggestions``) are proposals, not
    established studies, so they are excluded from study records; the owner
    review decides which of them become concrete cases.
    """
    studies: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("source_handle") or value.get("paper_id"):
            studies.append(dict(value))
        else:
            for key, child in value.items():
                if key == "case_suggestions":
                    continue
                studies.extend(_unit_study_records(child))
    elif isinstance(value, list):
        for child in value:
            studies.extend(_unit_study_records(child))
    elif isinstance(value, str) and re.fullmatch(r"P\d{4,}", value.strip()):
        studies.append({"source_handle": value.strip()})
    return studies


def _case_unit_catalog(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        units = _chapter_units(record.get("chapter_plan") or {})
        for index, unit in enumerate(units):
            if not isinstance(unit, Mapping):
                unit = {"point": _text(unit)}
            compact = {
                key: unit[key] for key in (
                    "unit_id", "title", "point", "substantive_point", "question", "evidence_need",
                    "comparison", "conditions", "limits", "source_handles", "cases", "supporting_studies", "paragraph_briefs",
                ) if key in unit
            }
            compact.pop("supporting_studies", None)
            compact["cases"] = _unit_study_records(unit)
            compact["source_handles"] = list(dict.fromkeys([
                *(unit.get("source_handles") or []),
                *[_text(case.get("source_handle")) for case in compact["cases"] if case.get("source_handle")],
            ]))
            catalog.append({"unit_key": f"{chapter_id}:{index + 1}", "chapter_id": chapter_id,
                            "chapter_title": _text(chapter.get("title")), "unit": compact})
    return catalog


_CASE_MATERIAL_STRING_LIMIT = 1200


def _clip_case_material_strings(value: Any, limit: int = _CASE_MATERIAL_STRING_LIMIT) -> Any:
    """Bound each free-text string in a case-selection material row."""

    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_clip_case_material_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _clip_case_material_strings(item, limit) for key, item in value.items()}
    return value


# Bump when the case_groups prompt or output contract changes, so a cached
# batch answered under an older contract is not reused silently.
CASE_GROUPS_PROMPT_CONTRACT = "case_groups.review_v2_03"


def _case_unit_task_signature(
    batch_unit_rows: Sequence[Mapping[str, Any]], *, context: Mapping[str, Any] | None = None,
) -> str:
    """Content signature of the unit tasks a case batch actually consumes.

    Covers points, briefs, cases and every other unit field the payload
    sends — a task edit under the same source list must invalidate a cached
    batch answer.
    """

    return hashlib.sha256(json.dumps(
        {"units": list(batch_unit_rows), "context": dict(context or {})},
        ensure_ascii=False, sort_keys=True, default=_json_default
    ).encode("utf-8")).hexdigest()[:16]


def _case_selection_material_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Compact one source row for the case selection model."""

    compact = {
        key: _clip_case_material_strings(row.get(key))
        for key in (
            "source_handle", "paper_id", "title", "doi", "year", "material_depth",
            "study_summary_A", "review_planning_B", "supplement_gap_material",
            "supplement_gap_materials", "supplement_material", "supplement_materials",
            "tool_supplement_materials", "local_passages",
        )
        if row.get(key) not in (None, "", [], {})
    }
    if row.get("deep_read_material"):
        compact["deep_read_material"] = _clip_case_material_strings(
            _compact_reading_material(row.get("deep_read_material")))
    compact["material_available"] = any(
        row.get(key) not in (None, "", [], {})
        for key in (
            "study_summary_A", "review_planning_B", "supplement_gap_material",
            "supplement_gap_materials", "supplement_material", "supplement_materials",
            "tool_supplement_materials", "local_passages", "deep_read_material",
        )
    )
    return compact


def _case_selection_material_rows(
    handles: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    pool_rows: Sequence[Mapping[str, Any]],
    read_materials: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Real material for exactly the routed candidates of one case batch.

    The case layer used to see only handles and thin routing notes, which let
    it invent experiments for papers it had never read.  Selection support now
    reuses the same material the chapter owner sees: packet source rows first,
    then bounded local card material from the pool.  Unresolved handles stay
    as explicitly unavailable candidates — nothing is fetched here.
    """

    rows_by_handle: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for source in record.get("source_materials") or ():
            if isinstance(source, Mapping):
                handle = _text(source.get("source_handle"))
                if handle and handle not in rows_by_handle:
                    rows_by_handle[handle] = source
    candidate_by_handle = {
        _text(row.get("_source_handle")): row
        for row in pool_rows
        if isinstance(row, Mapping) and _text(row.get("_source_handle"))
    }
    output: list[dict[str, Any]] = []
    for raw in handles:
        handle = _text(raw)
        if not handle:
            continue
        row = rows_by_handle.get(handle)
        if row is None:
            candidate = candidate_by_handle.get(handle)
            if candidate is not None:
                paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
                row = build_local_material_payload(
                    candidate,
                    deep_material_by_paper=read_materials.get(paper_id),
                )
        if row is None:
            output.append({"source_handle": handle, "material_available": False})
            continue
        output.append(_case_selection_material_row(row))
    return output


def _proposed_use_text(study: Mapping[str, Any]) -> str:
    """The selection layer's proposed use, from either field name."""

    return _text(study.get("proposed_use") or study.get("contribution"))


def _existing_study_texts(unit: Mapping[str, Any]) -> set[tuple[str, str]]:
    texts: set[tuple[str, str]] = set()
    for record in _unit_study_records(unit):
        handle = _text(record.get("source_handle"))
        text = _text(
            record.get("contribution")
            or record.get("macro_contribution")
            or record.get("finding")
            or record.get("use")
        )
        if handle and text:
            texts.add((handle, text))
    return texts


def _cross_chapter_source_uses(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Which cited sources serve more than one chapter, and where.

    Given to the whole-plan coordinator so cross-chapter placement is judged
    from actual use, not from citation counting: a handle listed here is a
    fact about the plan, not an instruction to remove it.
    """

    def adopted_content(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: adopted_content(child) for key, child in value.items()
                    if key not in {"case_suggestions", "case_suggestions_reviewed"}}
        if isinstance(value, (list, tuple)):
            return [adopted_content(child) for child in value]
        return value

    chapters_by_handle: dict[str, list[str]] = {}
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if not chapter_id:
            continue
        handles, _paper_ids = _source_keys_in_value(adopted_content(record.get("chapter_plan") or {}))
        for handle in sorted(handles):
            owners = chapters_by_handle.setdefault(handle, [])
            if chapter_id not in owners:
                owners.append(chapter_id)
    return [
        {"source_handle": handle, "chapter_ids": owners}
        for handle, owners in sorted(chapters_by_handle.items())
        if len(owners) > 1
    ]


def _archive_reviewed_case_suggestions(
    records: Sequence[Mapping[str, Any]],
    reviewed_chapter_ids: set[str],
) -> list[dict[str, Any]]:
    """Move owner-reviewed case suggestions out of the plan, keep them for audit.

    Chapters whose revision completed have had every pending proposal judged.
    Their ``case_suggestions`` leave the unit (so arrangement and later runs do
    not treat them as pending or as content) and land in the packet-level
    ``case_suggestions_reviewed`` trail.  Chapters whose revision did not
    complete keep their pending suggestions for the next run.
    """

    output: list[dict[str, Any]] = []
    for record in records:
        chapter = record.get("chapter") if isinstance(record.get("chapter"), Mapping) else {}
        chapter_id = _text(chapter.get("chapter_id"))
        if chapter_id not in reviewed_chapter_ids:
            output.append(dict(record))
            continue
        plan = record.get("chapter_plan") if isinstance(record.get("chapter_plan"), Mapping) else {}
        archived = [
            dict(item) for item in (record.get("case_suggestions_reviewed") or [])
            if isinstance(item, Mapping)
        ]
        units = _chapter_units(plan)
        pending: list[tuple[int, list[dict[str, Any]]]] = []
        for index, unit in enumerate(units):
            if isinstance(unit, Mapping) and unit.get("case_suggestions"):
                studies = [dict(item) for item in unit.get("case_suggestions") or []
                           if isinstance(item, Mapping)]
                pending.append((index, studies))
        if not pending:
            output.append(dict(record))
            continue
        plan_copy = json.loads(json.dumps(dict(plan), ensure_ascii=False, default=_json_default))
        copied_units = _chapter_units(plan_copy)
        for index, studies in pending:
            unit = copied_units[index]
            if isinstance(unit, Mapping):
                unit.pop("case_suggestions", None)
        for index, studies in pending:
            archived.append({"unit_key": f"{chapter_id}:{index + 1}", "studies": studies})
        row = dict(record)
        row["chapter_plan"] = plan_copy
        row["case_suggestions_reviewed"] = archived
        output.append(row)
    return output


def _chapter_retrieval_requests(chapters: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract bounded chapter-scoped requests before chapter writing."""
    gaps: list[dict[str, Any]] = []
    directed: list[dict[str, Any]] = []
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            continue
        chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
        if not chapter_id:
            continue
        for raw in chapter.get("supplement_requests") or chapter.get("retrieval_gaps") or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["chapter_ids"] = list(dict.fromkeys([chapter_id, *[str(item) for item in row.get("chapter_ids") or () if str(item).strip()]]))
            row.setdefault("gap_id", f"{chapter_id}:gap:{len(gaps) + 1}")
            gaps.append(row)
        for raw in chapter.get("directed_reads") or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["chapter_ids"] = list(dict.fromkeys([chapter_id, *[str(item) for item in row.get("chapter_ids") or () if str(item).strip()]]))
            directed.append(row)
        raw_questions = chapter.get("knowledge_gaps") or chapter.get("open_questions") or chapter.get("missing_evidence") or ()
        if isinstance(raw_questions, str):
            raw_questions = [raw_questions]
        for question in raw_questions:
            text = _text(question)
            if not text:
                continue
            gaps.append({
                "gap_id": f"{chapter_id}:question:{len(gaps) + 1}",
                "gap_question": text,
                "chapter_ids": [chapter_id],
                "targeted_queries": [{"query_text": text, "query_type": "question"}],
            })
    return gaps, directed


def _attach_chapter_candidate_sources(
    chapters: Sequence[Mapping[str, Any]],
    pool_rows: Sequence[Mapping[str, Any]],
    tool_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add successful chapter-owned supplement candidates to their chapter only."""
    owners_by_paper: dict[str, set[str]] = {}

    def add_handle(handle: Any, chapter_ids: Sequence[Any]) -> None:
        normalized_handle = _text(handle)
        if not normalized_handle:
            return
        owners = {_text(value) for value in chapter_ids if _text(value)}
        if normalized_handle and owners:
            owners_by_paper.setdefault(normalized_handle, set()).update(owners)

    def add_handles(handles: Any, chapter_ids: Sequence[Any]) -> None:
        if isinstance(handles, (str, bytes)):
            handles = [handles]
        for handle in handles or ():
            if isinstance(handle, Mapping):
                handle = handle.get("source_handle") or handle.get("paper_id") or handle.get("canonical_paper_id")
            add_handle(handle, chapter_ids)

    pending: list[Any] = list(tool_result.get("supplement_results") or [])
    pending.extend(tool_result.get("results") or [])
    while pending:
        group = pending.pop(0)
        if not isinstance(group, Mapping):
            continue
        owners = {_text(value) for value in (group.get("chapter_ids") or []) if _text(value)}
        for raw in group.get("candidate_rows") or []:
            if not isinstance(raw, Mapping):
                continue
            paper_id = _canonical_paper_id(raw)
            if paper_id and owners:
                owners_by_paper.setdefault(paper_id, set()).update(owners)
        pending.extend(group.get("results") or [])

    # Production adaptive retrieval records ownership outside candidate_rows:
    # needs carry owner chapters and new_handles, while owner_content repeats
    # the resolved handles under each chapter.  Read both shapes so a derived
    # pool can add its actual new papers to the owning chapter.
    retrieval_loop = tool_result.get("retrieval_loop") if isinstance(tool_result.get("retrieval_loop"), Mapping) else {}
    for need in retrieval_loop.get("needs") or []:
        if not isinstance(need, Mapping):
            continue
        owners = need.get("owners") or need.get("chapter_ids") or []
        add_handles(need.get("new_handles") or need.get("source_handles") or [], owners)
    owner_content = retrieval_loop.get("owner_content") or {}
    if isinstance(owner_content, Mapping):
        for need_content in owner_content.values():
            if not isinstance(need_content, Mapping):
                continue
            for chapter_id, content in need_content.items():
                if not isinstance(content, Mapping):
                    continue
                add_handles(content.get("handles") or content.get("new_handles") or [], [chapter_id])

    # Some cached loop results expose the same ownership only through the
    # chapter material writer packet.  It is already chapter-scoped, so those
    # handles are safe to associate with that chapter.
    for chapter_id, materials in (tool_result.get("tool_materials_by_chapter") or {}).items():
        for material in materials or []:
            if isinstance(material, Mapping):
                add_handles(
                    material.get("sources") or material.get("source_handles") or [],
                    [chapter_id],
                )
    rows_by_paper = {
        _text(row.get("_paper_id")): row
        for row in pool_rows
        if _text(row.get("_paper_id"))
    }
    rows_by_handle = {
        _text(row.get("_source_handle")): row
        for row in pool_rows
        if _text(row.get("_source_handle"))
    }
    # Normalize production handles and paper IDs into the same key space.
    normalized_owner_map: dict[str, set[str]] = {}
    for raw_key, owners in owners_by_paper.items():
        row = rows_by_paper.get(raw_key) or rows_by_handle.get(raw_key)
        canonical = _text(row.get("_paper_id")) if row is not None else raw_key
        if canonical:
            normalized_owner_map.setdefault(canonical, set()).update(owners)
    owners_by_paper = normalized_owner_map
    output: list[dict[str, Any]] = []
    for raw in chapters:
        chapter = dict(raw)
        chapter_id = _text(chapter.get("chapter_id") or chapter.get("id"))
        if not chapter_id:
            output.append(chapter)
            continue
        existing_ids = chapter.get("source_ids") or chapter.get("paper_ids") or []
        if isinstance(existing_ids, str):
            existing_ids = [existing_ids]
        existing_handles = chapter.get("source_handles") or []
        if isinstance(existing_handles, str):
            existing_handles = [existing_handles]
        for paper_id, owners in owners_by_paper.items():
            if chapter_id not in owners:
                continue
            row = rows_by_paper.get(paper_id)
            if row is None:
                continue
            existing_ids = [*existing_ids, paper_id]
            handle = _text(row.get("_source_handle"))
            if handle:
                existing_handles = [*existing_handles, handle]
        chapter["source_ids"] = list(dict.fromkeys(_text(value) for value in existing_ids if _text(value)))
        chapter["source_handles"] = list(dict.fromkeys(_text(value) for value in existing_handles if _text(value)))
        output.append(chapter)
    return output


def _attach_case_groups(
    records: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    *,
    planning_revision: bool = False,
    candidate_rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Consume the case layer's response into the chapter records.

    Old mode keeps the established contract: accepted studies land directly in
    ``supporting_studies`` with their contribution text.  In planning-revision
    mode the response is a selection proposal: each study becomes a
    ``case_suggestions`` entry (handle + proposed use + whether real material
    was attached), never an established case.  The program still backfills the
    selected papers' real material rows into the packet so the chapter owner
    can judge the proposal against actual content.  A handle with no material
    anywhere stays a suggestion flagged ``material_available: false``; nothing
    is adopted for it and nothing is fabricated.
    """

    result = json.loads(json.dumps(records, ensure_ascii=False, default=_json_default))
    all_sources = {source["source_handle"]: source for record in result for source in record.get("source_materials") or [] if source.get("source_handle")}
    candidate_by_handle = {
        _text(row.get("_source_handle")): row
        for row in candidate_rows or ()
        if isinstance(row, Mapping) and _text(row.get("_source_handle"))
    }
    destinations = {f"{record['chapter']['chapter_id']}:{i + 1}": (record, unit)
                    for record in result for i, unit in enumerate(_chapter_units(record.get("chapter_plan") or {})) if isinstance(unit, dict)}
    for addition in response.get("additions") or []:
        destination = destinations.get(addition.get("unit_key"))
        if not destination:
            continue
        record, unit = destination
        if planning_revision:
            suggestions = unit.setdefault("case_suggestions", [])
            suggested_seen = {
                (_text(item.get("source_handle")), _text(item.get("proposed_use")))
                for item in suggestions if isinstance(item, Mapping)
            }
            established = _existing_study_texts(unit)
            packet_handles = {item.get("source_handle") for item in record.get("source_materials") or []}
            for study in addition.get("studies") or []:
                if not isinstance(study, Mapping):
                    continue
                handle = _text(study.get("source_handle"))
                proposed_use = _proposed_use_text(study)
                if not handle:
                    continue
                key = (handle, proposed_use)
                if key in suggested_seen or key in established:
                    # An exactly repeated use is not a new use; a distinct
                    # proposal for the same paper still reaches the owner.
                    continue
                source = all_sources.get(handle)
                if source is None and handle in candidate_by_handle:
                    candidate = candidate_by_handle[handle]
                    paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
                    source = build_local_material_payload(candidate)
                suggestion = {
                    "source_handle": handle,
                    "proposed_use": proposed_use,
                    "material_available": source is not None,
                }
                if study.get("paper_id"):
                    suggestion["paper_id"] = _text(study.get("paper_id"))
                suggestions.append(suggestion)
                suggested_seen.add(key)
                if source is not None and handle not in packet_handles:
                    record["source_materials"].append(source); packet_handles.add(handle)
                    record.setdefault("source_identity_map", {})[handle] = {key2: source.get(key2) for key2 in ("paper_id", "title", "doi", "year")}
                    record["chapter"].setdefault("source_ids", []).append(source.get("paper_id"))
            continue
        studies = unit.setdefault("supporting_studies", [])
        existing = {item.get("source_handle") for item in _unit_study_records(unit)}
        packet_handles = {item.get("source_handle") for item in record.get("source_materials") or []}
        for study in addition.get("studies") or []:
            if not isinstance(study, Mapping):
                continue
            handle = study.get("source_handle")
            source = all_sources.get(handle)
            if not source or handle in existing:
                continue
            stored_study = dict(study)
            # Old mode consumes contribution through the established
            # arrangement contract. Accept a proposed_use response from a
            # stale/cache-backed caller without dropping its case text.
            if not _text(stored_study.get("contribution")) and _text(stored_study.get("proposed_use")):
                stored_study["contribution"] = _text(stored_study.get("proposed_use"))
            studies.append(stored_study); existing.add(handle)
            if handle not in packet_handles:
                record["source_materials"].append(source); packet_handles.add(handle)
                record.setdefault("source_identity_map", {})[handle] = {key: source.get(key) for key in ("paper_id", "title", "doi", "year")}
                record["chapter"].setdefault("source_ids", []).append(source["paper_id"])
    return result


def _card_for_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(candidate.get("card_path") or ""))
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _supply_local_citation_identity(value: Any, identity_map: Mapping[str, Any]) -> Any:
    if isinstance(value, list):
        return [_supply_local_citation_identity(item, identity_map) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {key: _supply_local_citation_identity(item, identity_map) for key, item in value.items()}
    handle = _text(result.get("source_handle") or result.get("reference_handle"))
    if handle and handle not in identity_map:
        handle = next((key for key, row in identity_map.items() if row.get("paper_id") == handle), handle)
    if handle in identity_map:
        if "source_handle" in result:
            result["source_handle"] = handle
        result["paper_id"] = identity_map[handle]["paper_id"]
    return result


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _snapshot_for_candidate(candidate: Mapping[str, Any]) -> Path | None:
    """Find an already-acquired snapshot through card, batch, or source-unit metadata."""
    card_path = Path(str(candidate.get("card_path") or ""))
    if not card_path.is_file():
        return None
    card = _card_for_candidate(candidate)
    material = card.get("material") if isinstance(card.get("material"), Mapping) else {}
    desired_id = _text(material.get("snapshot_id") or card.get("snapshot_id"))
    paper_id = _text(candidate.get("_paper_id") or _canonical_paper_id(candidate))
    for parent in [card_path.parent, *card_path.parents]:
        source_unit_path = parent / "SOURCE_UNIT.json"
        if source_unit_path.is_file():
            try:
                unit = _read_json(source_unit_path)
            except ProgressivePlanError:
                unit = {}
            if isinstance(unit, Mapping):
                snapshot = Path(str(unit.get("snapshot_path") or ""))
                if not snapshot.is_absolute():
                    snapshot = source_unit_path.parent / snapshot
                if snapshot.is_dir() and (not desired_id or _snapshot_id(snapshot) in {"", desired_id}):
                    return snapshot.resolve()
        batch_path = parent / "BATCH_STATE.json"
        if batch_path.is_file():
            try:
                batch = _read_json(batch_path)
            except ProgressivePlanError:
                batch = {}
            items = batch.get("items") if isinstance(batch, Mapping) else {}
            if isinstance(items, Mapping):
                for item in items.values():
                    if not isinstance(item, Mapping):
                        continue
                    if paper_id and _text(item.get("paper_id")) != paper_id:
                        continue
                    snapshot = Path(str(item.get("snapshot_dir") or ""))
                    if not snapshot.is_absolute():
                        snapshot = batch_path.parent / snapshot
                    if not snapshot.is_dir():
                        continue
                    snapshot_id = _snapshot_id(snapshot)
                    if desired_id and snapshot_id and snapshot_id != desired_id:
                        continue
                    return snapshot.resolve()
    return None


def _snapshot_id(snapshot_dir: Path) -> str:
    for name in ("manifest.json", "SNAPSHOT_MANIFEST.json", "snapshot.json"):
        path = snapshot_dir / name
        if not path.is_file():
            continue
        try:
            payload = _read_json(path)
        except ProgressivePlanError:
            continue
        if isinstance(payload, Mapping):
            nested = payload.get("snapshot") if isinstance(payload.get("snapshot"), Mapping) else {}
            return _text(payload.get("snapshot_id") or nested.get("snapshot_id"))
    return ""


def load_prior_readings(paths: Sequence[str | Path], pool_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Load explicitly supplied practical reading outputs that match this pool."""
    pool_by_id = {str(row.get("_paper_id") or _canonical_paper_id(row)): row for row in pool_rows}
    candidates: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates.extend(path.rglob("DIRECTED_READING.json"))
        elif path.is_file():
            candidates.append(path)
    output: dict[str, dict[str, Any]] = {}
    for path in candidates:
        try:
            raw = _read_json(path)
        except ProgressivePlanError:
            continue
        if not isinstance(raw, Mapping):
            continue
        artifact = raw.get("output") if isinstance(raw.get("output"), Mapping) else raw
        identity = artifact.get("paper_identity") if isinstance(artifact.get("paper_identity"), Mapping) else {}
        paper_id = _text(artifact.get("paper_id") or artifact.get("canonical_paper_id") or identity.get("canonical_paper_id"))
        if paper_id not in pool_by_id:
            continue
        pool = pool_by_id[paper_id]
        planning = pool.get("planning_view") if isinstance(pool.get("planning_view"), Mapping) else {}
        pool_identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
        expected_title = _text(pool_identity.get("title") or pool.get("title"))
        actual_title = _text(identity.get("title") or artifact.get("paper_title") or artifact.get("title"))
        if expected_title and actual_title and expected_title.casefold() != actual_title.casefold():
            continue
        value = dict(artifact)
        value["paper_id"] = paper_id
        value.setdefault("paper_title", expected_title)
        value["reused_from"] = str(path.resolve())
        output[paper_id] = value
    return list(output.values())


def make_planning_supplement_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path,
    budget_ledger_path: str | Path,
    budget_limit_cny: float,
    local_index_path: str | Path | None = None,
    allow_external: bool = True,
) -> Callable[..., Mapping[str, Any]]:
    """Build a callable adapter over the existing practical supplement path.

    Work order 02 makes this local-first: every gap is triaged against the
    already-registered local material before any provider is contacted.  A gap
    whose local material is insufficient keeps its external request for work
    order 03 instead of silently triggering a retrieval here.
    """
    def run(gaps: Sequence[Mapping[str, Any]], **context: Any) -> Mapping[str, Any]:
        if not gaps:
            return {"status": "not_requested", "results": []}
        from .material_acquisition import AcquisitionConfig, MaterialAcquirer
        from .paper_reading_card import run_paper_reading_card
        from .planning_supplement import (
            CompositeS2OpenAlexGateway, QwenCandidateSelector, run_gap_local_triage,
            run_planning_supplement, run_qwen_fulfillment_judge,
        )
        from .planning_material_triage import QwenLocalTriageJudge
        from ...s2_intelligence_gateway import S2IntelligenceGateway
        from tools.academic_backends.openalex_backend import OpenAlexBackend

        phase_root = Path(context.get("output_dir") or config.output_dir / str(context.get("phase") or "tools"))
        inputs_root = phase_root / "tool_inputs"
        base_path = inputs_root / "MERGED_PLANNING_POOL.jsonl"
        input_rows = []
        for row in context.get("pool_rows") or []:
            if isinstance(row, Mapping):
                input_rows.append({key: value for key, value in row.items() if not key.startswith("_")})
        _write_jsonl(base_path, input_rows)
        index_path = Path(local_index_path) if local_index_path else (
            config.output_dir / "planning_material_index.sqlite"
        )
        local_judge = QwenLocalTriageJudge(
            key_file=key_file, budget_ledger_path=budget_ledger_path,
            budget_limit_cny=float(budget_limit_cny), model=config.reader_model,
            timeout_seconds=config.timeout_seconds,
            raw_response_dir=phase_root / "_llm_response_cache",
        ) if Path(key_file).is_file() else None
        user_scope = _text((context.get("plan") or {}).get("research_question") or (context.get("plan") or {}).get("question_en") or (context.get("plan") or {}).get("question")) or _text(config.topic_id)
        local_triage: list[dict[str, Any]] = []
        remaining: list[Mapping[str, Any]] = []
        if context.get("skip_local_triage"):
            remaining = list(gaps)
        else:
            for gap in gaps:
                if not index_path.is_file():
                    remaining.append(gap)
                    continue
                gap_id = _safe_id(gap.get("gap_id") or f"gap_{len(local_triage) + 1:02d}")
                try:
                    triage = run_gap_local_triage(
                        {**dict(gap), "gap_id": gap_id},
                        index_path=index_path,
                        user_scope=user_scope,
                        judge=local_judge,
                        output_dir=phase_root / "local_triage" / gap_id,
                        read_local=True,
                    )
                except Exception as exc:  # local triage failure must not hide the gap
                    local_triage.append({"gap_id": gap_id, "decision": "triage_failed", "error": type(exc).__name__})
                    remaining.append(gap)
                    continue
                local_triage.append(triage)
                if triage.get("decision") == "external_research":
                    remaining.append(gap)
        if not remaining:
            return {"status": "local_material_ready", "results": [], "local_triage": local_triage}
        if not allow_external:
            return {
                "status": "external_research_required",
                "results": [],
                "local_triage": local_triage,
                "pending_gaps": [str(gap.get("gap_id") or "") for gap in remaining],
            }
        judge = run_qwen_fulfillment_judge(
            key_file=key_file,
            budget_ledger_path=budget_ledger_path,
            budget_limit_cny=budget_limit_cny,
        )
        selector = QwenCandidateSelector(
            key_file=key_file,
            budget_ledger_path=budget_ledger_path,
            budget_limit_cny=float(budget_limit_cny),
        ) if Path(key_file).is_file() else None

        def reuse_material(*, record: Mapping[str, Any], pool_rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
            paper_id = _text(record.get("canonical_paper_id") or record.get("paper_id"))
            candidate = next((row for row in pool_rows if _text(row.get("paper_id") or row.get("_paper_id")) == paper_id), None)
            if not isinstance(candidate, Mapping):
                return None
            snapshot = _snapshot_for_candidate(candidate)
            card_path = Path(str(candidate.get("card_path") or ""))
            card = _card_for_candidate(candidate) if card_path.is_file() else {}
            if snapshot is None and not card:
                return None
            return {
                "snapshot": snapshot,
                "card": card,
                "card_path": str(card_path) if card_path.is_file() else "",
                "material_depth": _text((candidate.get("_b_summary") or {}).get("declared_content_depth")) or "fulltext",
            }

        def acquirer_factory(material_root: Path):
            return MaterialAcquirer(material_root, config=AcquisitionConfig(cache_root=material_root / "cache"))

        gateway = CompositeS2OpenAlexGateway(S2IntelligenceGateway(), OpenAlexBackend())
        results: list[dict[str, Any]] = []
        for gap in remaining:
            gap_id = _safe_id(gap.get("gap_id") or f"gap_{len(results) + 1:02d}")
            request_path = inputs_root / gap_id / "REQUEST.json"
            request = {
                "schema_version": "optomind.planning_supplement.request.v1",
                "request_id": f"progressive-{_safe_id(config.topic_id)}-{_safe_id(context.get('phase'))}-{gap_id}",
                "topic_id": config.topic_id,
                "gap_id": gap_id,
                "gap_question": _text(gap.get("gap_question") or gap.get("question")) or "Resolve a scope-relevant literature gap.",
                "success_criteria": list(gap.get("success_criteria") or ["Return directly relevant material or clearly state what remains unresolved."]),
                "base_pool_path": str(base_path.resolve()),
                "plan_path": str(config.plan_path.resolve()),
                "targeted_queries": [dict(item) for item in gap.get("targeted_queries") or [] if isinstance(item, Mapping)],
                "reuse_plan_facet_ids": list(gap.get("reuse_plan_facet_ids") or []),
                "known_papers": [dict(item) for item in gap.get("known_papers") or [] if isinstance(item, Mapping)],
                "reviewed_references": [dict(item) for item in gap.get("reviewed_references") or [] if isinstance(item, Mapping)],
                "limits": {"max_candidates": 12, "max_acquisitions": 3, "per_query_limit": 12},
            }
            _atomic_json(request_path, request)
            output_dir = phase_root / "supplements" / gap_id
            try:
                result = run_planning_supplement(
                    request_path,
                    output_dir=output_dir,
                    gateway=gateway,
                    acquirer_factory=acquirer_factory,
                    card_runner=run_paper_reading_card,
                    fulfillment_judge=judge,
                    card_options={
                        "key_file": str(key_file),
                        "budget_ledger_path": str(budget_ledger_path),
                        "budget_limit_cny": float(budget_limit_cny),
                    },
                    candidate_selector=selector,
                    reuse_material=reuse_material,
                )
                index_path = output_dir / "SUPPLEMENT_INDEX.json"
                index = _read_json(index_path) if index_path.is_file() else {}
                results.append({
                    **dict(result),
                    "gap_id": gap_id,
                    "chapter_ids": list(gap.get("chapter_ids") or []),
                    "fulfillment_judgment": index.get("fulfillment_judgment") if isinstance(index, Mapping) else result.get("fulfillment_judgment"),
                    "source_units": index.get("source_units") if isinstance(index, Mapping) else [],
                    "citation_anchors": index.get("citation_anchors") if isinstance(index, Mapping) else [],
                    "substantive_gap_status": index.get("substantive_gap_status") if isinstance(index, Mapping) else result.get("substantive_gap_status"),
                    "status": result.get("status") or "completed",
                })
            except Exception as exc:
                results.append({"gap_id": gap_id, "chapter_ids": list(gap.get("chapter_ids") or []), "status": "failed", "error": type(exc).__name__})
        return {
            "status": "completed" if any(row.get("status") not in {"failed", "error"} for row in results) else "failed",
            "results": results,
            "local_triage": local_triage,
            "external_gap_ids": [str(gap.get("gap_id") or "") for gap in remaining],
        }
    return run


def _extend_planning_material_index(index_path: Path, rows: Sequence[Mapping[str, Any]], root: Path, topic_id: str) -> None:
    """Make newly acquired cards/body text available to the very next need."""
    if not rows or not index_path.is_file():
        return
    from .planning_material_search import SearchIndexConfig, PlanningMaterialIndex, build_index
    root.mkdir(parents=True, exist_ok=True)
    pool_path = root / "POOL_DELTA.jsonl"
    pool_path.write_text("\n".join(json.dumps(dict(row), ensure_ascii=False, default=_json_default) for row in rows) + "\n", encoding="utf-8")
    identity = {}
    snapshot_roots = set()
    directed_roots = set()
    with PlanningMaterialIndex(index_path, readonly=True) as old_index:
        existing = {row["paper_id"]: dict(row) for row in old_index.papers()}
    for row in rows:
        paper_id = _text(row.get("_paper_id") or _canonical_paper_id(row))
        handle = _text(row.get("_source_handle"))
        if not paper_id or not handle:
            continue
        compact = row.get("_b_summary") or _compact_b_record(row)
        identity[handle] = {"paper_id": paper_id, "card_path": row.get("card_path"),
                            **{key: compact.get(key) for key in ("title", "doi", "year")}}
        snapshot = _snapshot_for_candidate(row)
        old = existing.get(paper_id) or {}
        if snapshot is None and old.get("snapshot_path"):
            prior_path = Path(old["snapshot_path"])
            snapshot = prior_path if prior_path.is_dir() else None
        if snapshot:
            snapshot_roots.add(snapshot.parent.parent)
        if old.get("directed_reading_path"):
            directed_roots.add(Path(old["directed_reading_path"]).parent.parent)
    if not identity:
        return
    identity_path = root / "IDENTITIES.json"
    _atomic_json(identity_path, {"source_identity_map": identity})
    build_index(SearchIndexConfig(topic_id=topic_id, index_path=index_path, pool_path=pool_path,
                                  identity_map_path=identity_path, snapshot_roots=tuple(snapshot_roots), directed_roots=tuple(directed_roots)))
    with PlanningMaterialIndex(index_path) as index:
        for row in rows:
            materials = row.get("supplement_gap_materials") or [row.get("supplement_gap_material")]
            segments = [{"segment_kind": "directed_content", "section_path": ["Supplementary question reading"],
                         "text": json.dumps(item, ensure_ascii=False), "ordinal": i}
                        for i, item in enumerate(materials) if item]
            if segments:
                index.add_segments(_text(row.get("_paper_id") or _canonical_paper_id(row)), segments)
        index.commit()
        index.record_term_document_frequency()


def make_retrieval_loop_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path | None = None,
    budget_ledger_path: str | Path | None = None,
    budget_limit_cny: float | None = None,
    local_index_path: str | Path | None = None,
    allow_external: bool = True,
    supplement_runner: Callable[..., Mapping[str, Any]] | None = None,
    directed_reader: Callable[..., Mapping[str, Any]] | None = None,
    prior_readings: Sequence[Mapping[str, Any]] = (),
) -> Callable[..., Mapping[str, Any]]:
    """Adapt planner requests to the shared bounded retrieval queue.

    The queue owns the local-first decision and round bounds.  The existing
    supplement and directed-reading adapters remain the only provider-facing
    closures, which keeps the 581-card pool and the cumulative deep-read
    ledger intact while allowing the same narrow request at every level.
    """

    read_cache_path = config.output_dir / "DIRECTED_MATERIAL_CACHE.json"
    read_cache = _read_json(read_cache_path) if read_cache_path.is_file() else {}
    read_materials = dict(read_cache.get("materials") or {})
    consumed_ids = set(read_cache.get("consumed_paper_ids") or [])
    for item in prior_readings:
        paper_id = _text(item.get("paper_id"))
        if paper_id:
            read_materials[paper_id] = dict(item)
            consumed_ids.add(paper_id)

    def run(*, phase: str, supplement_requests: Sequence[Mapping[str, Any]] = (),
            directed_requests: Sequence[Mapping[str, Any]] = (), pool_rows: Sequence[Mapping[str, Any]] = (),
            plan: Mapping[str, Any] | None = None, prior_tool_results: Mapping[str, Any] | None = None,
            prior_directed: Mapping[str, Any] | None = None, source_handle_map: Mapping[str, str] | None = None,
            resume: bool = True, output_dir: str | Path | None = None, **_: Any) -> Mapping[str, Any]:
        from .planning_retrieval_loop import (
            InformationNeed, LoopConfig, need_id_for, needs_from_planner_gap_rows, run_retrieval_loop,
        )

        root = Path(output_dir or config.output_dir / str(phase).lower())
        root.mkdir(parents=True, exist_ok=True)
        topic = _text((plan or {}).get("research_question") or (plan or {}).get("question_en") or (plan or {}).get("question") or config.topic_id)
        gaps = [_normalize_gaps([row])[0] for row in supplement_requests if isinstance(row, Mapping)]
        tasks = _merge_directed_tasks(directed_requests)
        needs = needs_from_planner_gap_rows(gaps, intended_use="mechanism", user_scope=topic)
        kind_by_id: dict[str, str] = {}
        request_by_id: dict[str, Mapping[str, Any]] = {}
        for need in needs:
            kind_by_id[need.need_id] = "supplement"
            request_by_id[need.need_id] = next((row for row in gaps if _text(row.get("gap_question") or row.get("question")) == need.question), {})
        for task in tasks:
            paper_id = _text(task.get("paper_id"))
            questions = [
                _text(item.get("question") if isinstance(item, Mapping) else item)
                for item in task.get("questions") or ()
            ]
            question = "; ".join(item for item in questions if item) or "; ".join(task.get("knowledge_gaps") or task.get("reasons") or [])
            if not question:
                continue
            need_id = _text(task.get("need_id")) or need_id_for(question + (" || " + paper_id if paper_id else ""))
            round_specs = tuple(task.get("round_specs") or ())
            need = InformationNeed(
                need_id=need_id,
                question=question,
                owners=tuple(_text(item) for item in task.get("chapter_ids") or () if _text(item)),
                intended_use="study_design",
                kind="directed",
                user_scope=topic,
                success_criteria=tuple(_text(item) for item in task.get("required_outputs") or () if _text(item)),
                concepts=tuple(_text(item) for item in task.get("concepts") or () if _text(item)),
                round_specs=tuple(dict(item) for item in round_specs if isinstance(item, Mapping)),
                existing_handles=tuple(_text(item) for item in task.get("existing_handles") or () if _text(item)),
            )
            needs.append(need)
            kind_by_id[need_id] = "directed"
            request_by_id[need_id] = task

        if not needs:
            return {"phase": phase, "status": "not_requested", "supplement_results": [], "directed_results": [],
                    "retrieval_loop": {"needs": []}, "tool_materials_by_chapter": {}}

        index_path = Path(local_index_path) if local_index_path else config.output_dir / "planning_material_index.sqlite"
        judge = None
        if key_file and budget_ledger_path and budget_limit_cny is not None:
            from .planning_material_triage import QwenLocalTriageJudge
            judge = QwenLocalTriageJudge(
                key_file=key_file, budget_ledger_path=budget_ledger_path,
                budget_limit_cny=float(budget_limit_cny), model=config.reader_model,
                timeout_seconds=config.timeout_seconds,
                raw_response_dir=root / "_llm_response_cache",
            )
        working_pool = [dict(row) for row in pool_rows]
        journal_path = root / "retrieval_loop.jsonl"
        if resume and journal_path.is_file():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                recovered = entry.get("external_results") or [entry.get("external_result")]
                for result in recovered:
                    if isinstance(result, Mapping):
                        _merge_supplement_pool_updates(working_pool, {"supplement_results": [result]})
        local_results: dict[str, Mapping[str, Any]] = {}
        external_results: dict[str, list[Mapping[str, Any]]] = {}
        pool_by_id = {str(row.get("_paper_id")): dict(row) for row in working_pool if isinstance(row, Mapping)}
        paper_to_handle = {str(value): str(key) for key, value in (source_handle_map or {}).items()}

        def local_triage(*, gap: Any, round_index: int) -> Mapping[str, Any]:
            from .planning_supplement import run_gap_local_triage
            if kind_by_id.get(gap.gap_id) == "directed":
                return {"decision": "external_research", "usable_content": "", "still_missing": gap.question,
                        "external_ask": "Read the nominated paper for this task."}
            gap_row = {
                "gap_id": gap.gap_id, "gap_question": gap.question, "question": gap.question,
                "intended_use": gap.intended_use, "user_scope": gap.user_scope,
                "success_criteria": list(gap.success_criteria), "chapter_ids": list(gap.chapter_ids),
                "targeted_queries": [{"query_text": item, "query_type": "keyword"} for item in gap.concepts],
                "required_concepts": list(gap.required_concepts), "known_paper_handles": list(gap.existing_handles),
            }
            # A planner may supply a Chinese question without English query
            # terms. Keep that question for the reader, but use the existing
            # topic vocabulary to obtain local candidates instead of crashing.
            from .planning_material_search import expand_query
            if not expand_query(" ".join([gap.question, *gap.concepts, *gap.required_concepts])):
                gap_row["targeted_queries"] = [
                    {"query_text": form, "query_type": "keyword"}
                    for term in expand_query(topic) for form in term.forms
                ]
            if not index_path.is_file():
                result = {"gap_id": gap.gap_id, "decision": "external_research", "usable_content": "",
                          "still_missing": gap.question, "external_ask": gap.question}
                local_results[gap.gap_id] = result
                return result
            kwargs = {
                "index_path": index_path, "user_scope": topic, "judge": judge,
                "output_dir": root / "local_triage" / _safe_id(gap.gap_id), "read_local": True,
            }
            try:
                result = run_gap_local_triage(gap_row, **kwargs)
            except TypeError as exc:
                # Keep offline compatibility with an older injected helper;
                # production always uses the read_local-capable API.
                if "read_local" not in str(exc):
                    raise
                kwargs.pop("read_local", None)
                result = run_gap_local_triage(gap_row, **kwargs)
            local_results[gap.gap_id] = dict(result)
            return dict(result)

        def refine_queries(need: Any, round_index: int, previous_queries: Sequence[Mapping[str, Any]],
                           usable_content: str, still_missing: str) -> list[dict[str, Any]]:
            if need.kind == "directed" or not allow_external or not key_file or not budget_ledger_path or budget_limit_cny is None:
                return []
            from .module4.runtime import GlobalBudgetLedger, QwenDirectClient, invoke_client
            from .planning_material_triage import QwenLocalTriageJudge
            client = QwenDirectClient(model=config.reader_model, key_file=key_file,
                                      budget_ledger=GlobalBudgetLedger(limit_cny=budget_limit_cny, path=Path(budget_ledger_path)),
                                      max_output_tokens=1600, thinking=True, thinking_budget=1024,
                                      json_mode=config.reader_model == "qwen3.7-flash", max_retries=0,
                                      timeout_seconds=config.timeout_seconds, raw_response_dir=root / "query_refinements")
            response = invoke_client(client, [
                {"role": "system", "content": "你为综述的一个具体缺口改写检索式。根据已经找到的内容和仍缺的问题，换一个有希望的窄方向。只返回至多2条英语查询：keyword用研究对象或材料+具体关系，question用包含对象、关系和必要设置的完整语义陈述。不要把缺口说明整段拼进查询，不重复已查过的方向，不用没有研究对象的通用问句。保持综述主题与实际比较、方法或条件；找不到某种证据时不偷换对象。若现有材料足够调整写法或再查价值小，返回空列表。JSON {targeted_queries:[{query_type:keyword或question,query_text:...}],reason:...}。"},
                {"role": "user", "content": json.dumps({"review_scope": need.user_scope, "question": need.question,
                     "previous_queries": previous_queries, "useful_material": usable_content,
                     "still_missing": still_missing, "next_round": round_index}, ensure_ascii=False)},
            ], model=config.reader_model, max_output_tokens=1600, thinking=True, thinking_budget=1024,
               call_id=f"query-refinement:{need.need_id}:{round_index}")
            decoded = QwenLocalTriageJudge._decode(response.get("content"))
            return [dict(item) for item in decoded.get("targeted_queries") or [] if isinstance(item, Mapping)][:2]

        def external_closure(*, need: Any, round_index: int, queries: Sequence[Mapping[str, Any]], spec: Mapping[str, Any], **budget_context: Any) -> Mapping[str, Any]:
            request = dict(request_by_id.get(need.need_id) or {})
            context = {
                "phase": phase, "output_dir": root / "external" / _safe_id(need.need_id) / f"round_{round_index}",
                "topic_id": config.topic_id, "plan": dict(plan or {}), "pool_rows": working_pool,
                "pool_by_id": pool_by_id, "source_handle_map": dict(source_handle_map or {}),
                "prior_tool_results": dict(prior_tool_results or {}), "prior_directed": dict(prior_directed or {}),
                "shared_deep_read_budget": config.shared_deep_read_budget,
                "skip_local_triage": True,
            }
            if kind_by_id.get(need.need_id) == "supplement":
                if supplement_runner is None or not allow_external:
                    return {"status": "unmet", "usable_content": "", "still_missing": need.question}
                request["targeted_queries"] = [dict(item) for item in queries if isinstance(item, Mapping)]
                raw = dict(supplement_runner([request], **context))
                before = {str(row.get("_paper_id")): json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) for row in working_pool}
                _merge_supplement_pool_updates(working_pool, {"supplement_results": [raw]})
                pool_by_id.update({str(row.get("_paper_id")): row for row in working_pool})
                changed = [row for row in working_pool if before.get(str(row.get("_paper_id"))) != json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)]
                _extend_planning_material_index(index_path, changed, root / "index_updates" / _safe_id(need.need_id) / str(round_index), config.topic_id)
                external_results.setdefault(need.need_id, []).append(raw)
                judgment = raw.get("fulfillment_judgment") if isinstance(raw.get("fulfillment_judgment"), Mapping) else {}
                judgments: list[Mapping[str, Any]] = [judgment] if judgment else []
                pending = list(raw.get("results") or [])
                while pending:
                    group = pending.pop(0)
                    if not isinstance(group, Mapping):
                        continue
                    pending.extend(group.get("results") or [])
                    candidate = group.get("fulfillment_judgment")
                    if isinstance(candidate, Mapping):
                        judgments.append(candidate)
                useful_values: list[Any] = []
                for item in judgments:
                    values = item.get("useful_material") or item.get("auxiliary_material") or []
                    if isinstance(values, str):
                        values = [values]
                    useful_values.extend(value for value in values if str(value).strip())
                useful = raw.get("usable_content") or " ".join(str(item) for item in useful_values)
                statuses = {
                    _text(item.get("status") or item.get("substantive_gap_status"))
                    for item in judgments
                    if _text(item.get("status") or item.get("substantive_gap_status"))
                }
                if "fulfilled" in statuses:
                    status = "fulfilled"
                elif "partial" in statuses or useful:
                    status = "partial"
                else:
                    status = _text(raw.get("status"))
                remaining = "; ".join(dict.fromkeys(
                    _text(item.get("remaining_gap")) for item in judgments if _text(item.get("remaining_gap"))
                ))
                if status == "fulfilled":
                    remaining = ""
                elif not remaining and statuses - {"fulfilled"}:
                    remaining = need.question
                return {"status": status if status in {"fulfilled", "partial"} else "unmet",
                        "usable_content": str(useful), "still_missing": remaining,
                        "new_handles": [], "raw_result": raw}
            # External retrieval can be disabled while reading an already
            # downloaded paper remains available under the shared read budget.
            if directed_reader is None:
                return {"status": "unmet", "usable_content": "", "still_missing": need.question}
            paper_id = _text(request.get("paper_id"))
            if paper_id in read_materials:
                raw = {"status": "reused_prior_deep_read", "materials": [read_materials[paper_id]],
                       "consumed_paper_ids": [paper_id]}
            elif paper_id not in consumed_ids and len(consumed_ids) >= config.shared_deep_read_budget:
                return {"status": "unmet", "usable_content": "", "still_missing": need.question,
                        "outcome": "shared_deep_read_budget_exhausted"}
            else:
                raw = dict(directed_reader([request], **context))
                consumed_ids.update(raw.get("consumed_paper_ids") or [])
                for item in raw.get("materials") or []:
                    if isinstance(item, Mapping) and _text(item.get("paper_id")):
                        read_materials[_text(item.get("paper_id"))] = dict(item)
                _atomic_json(read_cache_path, {"materials": read_materials, "consumed_paper_ids": sorted(consumed_ids)})
            external_results.setdefault(need.need_id, []).append(raw)
            materials = raw.get("materials") or []
            useful = " ".join(
                json.dumps(item.get("question_material"), ensure_ascii=False)
                for item in materials if isinstance(item, Mapping) and item.get("question_material")
            )
            added = [_text(item.get("paper_id")) for item in materials if isinstance(item, Mapping) and _text(item.get("paper_id"))]
            return {"status": "fulfilled" if useful else _text(raw.get("status")) or "unmet",
                    "usable_content": useful, "new_handles": added, "raw_result": raw,
                    "consumed_paper_ids": list(raw.get("consumed_paper_ids") or [])}

        loop_config = LoopConfig(
            index_path=index_path, journal_path=root / "retrieval_loop.jsonl",
            max_rounds=3, max_empty_rounds=2, max_queries_per_round=3,
            shared_deep_read_budget=config.shared_deep_read_budget,
        )
        loop_result = run_retrieval_loop(
            needs, loop_config, local_triage=local_triage, external_closure=external_closure,
            resume=resume, refine_queries=refine_queries, prior_consumed_paper_ids=sorted(consumed_ids),
        )
        materials_by_chapter: dict[str, list[dict[str, Any]]] = {}
        supplement_results: list[dict[str, Any]] = []
        directed_results: list[dict[str, Any]] = []
        for need_state in loop_result.get("needs") or []:
            need_id = _text(need_state.get("need_id"))
            need_info = need_state.get("need") if isinstance(need_state.get("need"), Mapping) else {}
            owners = [_text(item) for item in need_info.get("owners") or need_state.get("owners") or () if _text(item)]
            local = need_state.get("local_triage") if isinstance(need_state.get("local_triage"), Mapping) else local_results.get(need_id, {})
            restored = [entry for entry in need_state.get("external_results") or [] if isinstance(entry, Mapping)]
            raw_results = [dict(item) for item in (restored or external_results.get(need_id, [])) if isinstance(item, Mapping)]
            raw = raw_results[-1] if raw_results else {}
            if raw_results:
                if kind_by_id.get(need_id) == "supplement":
                    supplement_results.extend(raw_results)
                else:
                    directed_results.extend(raw_results)
            usable = _text(need_state.get("usable_content") or local.get("usable_content"))
            writer = local.get("writer_material") if isinstance(local.get("writer_material"), Mapping) else {}
            sources = writer.get("sources") if isinstance(writer.get("sources"), list) else local.get("passages") or []
            if raw_results and kind_by_id.get(need_id) == "directed":
                directed_materials = [
                    item for result in raw_results for item in (result.get("materials") or [])
                    if isinstance(item, Mapping) and _text(item.get("paper_id"))
                ]
                sources = [
                    {"paper_id": _text(item.get("paper_id")),
                     "source_handle": paper_to_handle.get(_text(item.get("paper_id"))),
                     "title": _text(item.get("title") or item.get("paper_title"))}
                    for item in directed_materials
                ] or sources
            if raw_results and kind_by_id.get(need_id) == "supplement":
                pending = list(raw_results)
                collected_sources: list[Mapping[str, Any]] = []
                collected_values: list[str] = []
                while pending:
                    group = pending.pop(0)
                    if not isinstance(group, Mapping):
                        continue
                    pending.extend(group.get("results") or [])
                    judgment = group.get("fulfillment_judgment") if isinstance(group.get("fulfillment_judgment"), Mapping) else {}
                    values = judgment.get("useful_material") or judgment.get("auxiliary_material") or []
                    if isinstance(values, str):
                        values = [values]
                    if isinstance(values, (list, tuple)):
                        collected_values.extend(str(item) for item in values if str(item).strip())
                    collected_sources.extend(item for item in group.get("source_units") or () if isinstance(item, Mapping))
                if not usable:
                    usable = " ".join(dict.fromkeys(collected_values))
                sources = [{**dict(item.get("record_identity") or {}), **dict(item)} for item in collected_sources] or sources
            if usable or writer:
                material = dict(writer)
                material.update({"need_id": need_id, "chapter_ids": owners, "question": _text(need_info.get("question")),
                                 "decision": _text(need_state.get("action") or local.get("decision")),
                                 "usable_content": usable, "still_missing": _text(need_state.get("still_missing") or local.get("still_missing")),
                                 "sources": [dict(item) for item in sources if isinstance(item, Mapping)]})
                for owner in owners:
                    materials_by_chapter.setdefault(owner, []).append(dict(material))
        return {
            "phase": phase, "status": "complete", "retrieval_loop": loop_result,
            "supplement_results": supplement_results, "directed_results": directed_results,
            "tool_materials_by_chapter": materials_by_chapter,
            "consumed_paper_ids": sorted(consumed_ids),
        }
    return run


def make_directed_reading_runner(
    config: ProgressivePlannerConfig,
    *,
    key_file: str | Path,
    budget_ledger_path: str | Path,
    budget_limit_cny: float,
    prior_readings: Sequence[Mapping[str, Any]] = (),
) -> Callable[..., Mapping[str, Any]]:
    """Build a callable adapter over existing snapshots and DirectedReadingStore."""
    def run(tasks: Sequence[Mapping[str, Any]], **context: Any) -> Mapping[str, Any]:
        if not tasks:
            return {"status": "not_requested", "materials": [], "consumed_paper_ids": [], "attempted_paper_ids": []}
        from .directed_reading import DirectedReadingStore, build_directed_request, run_directed_reading

        phase_root = Path(context.get("output_dir") or config.output_dir / str(context.get("phase") or "tools"))
        store = DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=max(1, config.shared_deep_read_budget))
        plan = context.get("plan") if isinstance(context.get("plan"), Mapping) else {}
        topic = _text(plan.get("research_question") or plan.get("review_title") or plan.get("question_en") or plan.get("question") or config.topic_id)
        topic_binding = "progressive-review:" + config.topic_id
        pool_by_id = context.get("pool_by_id") if isinstance(context.get("pool_by_id"), Mapping) else {}
        results: list[dict[str, Any]] = []
        prior_by_id = {str(row.get("paper_id")): dict(row) for row in prior_readings if isinstance(row, Mapping) and _text(row.get("paper_id"))}

        def normalize_question_rows(task: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            questions: list[dict[str, Any]] = []
            outputs: list[dict[str, Any]] = []
            for index, raw in enumerate(task.get("questions") or [], start=1):
                item = dict(raw) if isinstance(raw, Mapping) else {"question": str(raw)}
                qid = _text(item.get("question_id") or item.get("id")) or f"Q{index:02d}"
                question = _text(item.get("question") or item.get("ask") or item.get("what_to_extract"))
                if not question:
                    continue
                oid = f"O{index:02d}"
                outputs.append({"output_id": oid, "output_type": "practical_material", "description": _text(item.get("purpose") or item.get("required_output") or question)})
                questions.append({"question_id": qid, "question": question, "purpose": _text(item.get("purpose") or item.get("use") or question), "required_output_ids": [oid], "gap_key": _text(item.get("gap_key") or item.get("knowledge_gap") or task.get("knowledge_gap"))})
            if not questions:
                question = _text(task.get("knowledge_gap") or task.get("reason") or "Extract the paper's evidence relevant to the coordinated review scope.")
                questions = [{"question_id": "Q01", "question": question, "purpose": "Supply a concrete, attributed case for the coordinated review.", "required_output_ids": ["O01"], "gap_key": question}]
                outputs = [{"output_id": "O01", "output_type": "practical_material", "description": "Source-based findings, conditions, methods and limits useful to the review."}]
            return questions, outputs

        def read_one(task: Mapping[str, Any]) -> dict[str, Any]:
            paper_id = _text(task.get("paper_id"))
            candidate = pool_by_id.get(paper_id)
            if not isinstance(candidate, Mapping):
                return {"paper_id": paper_id, "status": "unavailable", "reason": "paper_not_in_current_pool"}
            reused = prior_by_id.get(paper_id)
            if reused:
                return {"paper_id": paper_id, "status": "reused_prior_deep_read", "material": reused}
            snapshot = _snapshot_for_candidate(candidate)
            if snapshot is None:
                return {"paper_id": paper_id, "status": "unavailable", "reason": "existing_snapshot_not_found"}
            card = _card_for_candidate(candidate)
            identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), Mapping) else {}
            a_summary = card.get("general_understanding") if isinstance(card.get("general_understanding"), Mapping) else {}
            planning = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), Mapping) else {}
            planning_identity = planning.get("paper_identity") if isinstance(planning.get("paper_identity"), Mapping) else {}
            title = _text(identity.get("title") or planning_identity.get("title") or candidate.get("title"))
            questions, outputs = normalize_question_rows(task)
            chapter_ids = list(task.get("chapter_ids") or [])
            chapter_title = " / ".join(_text(item) for item in chapter_ids) or "Coordinated review"
            kind = _text(identity.get("paper_kind") or a_summary.get("paper_kind") or a_summary.get("study_type") or candidate.get("paper_kind") or "study")
            if isinstance(candidate.get("root_review_note"), Mapping) and candidate.get("root_review_note"):
                return {"paper_id": paper_id, "status": "review_reported_no_reacquire", "reason": "Use the attributed account already supplied by its source review.", "blocked_paper_id": paper_id}
            scope = _text((card.get("material") or {}).get("material_scope") if isinstance(card.get("material"), Mapping) else "") or planning.get("material_scope")
            nomination = {
                "canonical_paper_id": paper_id,
                "title": title,
                "paper_kind": kind,
                "material_scope": scope,
                "snapshot_dir": str(snapshot),
                "plan_path": str(config.plan_path),
                "nomination_reason": _text(task.get("reason") or task.get("knowledge_gap") or "Needed to resolve a specific coordinated review question."),
                "expected_information_gain": "Provides paper-specific findings, conditions, and limits for the requested comparison.",
                "core_justification": "The coordinated outline names this source for a specific evidence need.",
                "knowledge_gap": "; ".join(_text(item) for item in (task.get("knowledge_gaps") or [task.get("knowledge_gap") or task.get("reason") or "The coordinated outline identifies an unresolved evidence question."]) if _text(item)),
                "required_outputs": [row["output_id"] for row in outputs],
                "questions": questions,
                "approve_core": True,
                "substantive_review": "review" in kind.casefold() or "meta-analysis" in kind.casefold(),
                "metadata": {"chapter_ids": chapter_ids, "request_origin": str(context.get("phase") or "progressive_review")},
            }
            request = build_directed_request(
                review_id=config.topic_id,
                topic=topic,
                chapter={"chapter_id": "coordinated_scope", "title": chapter_title},
                questions=questions,
                required_outputs=outputs,
                candidates=[nomination],
                topic_binding=topic_binding,
            )
            admission = store.admit_candidates(config.topic_id, topic_binding, [nomination], topic_hash=config.topic_id)
            if not admission or admission[0].get("admission_status") != "approved_core":
                return {"paper_id": paper_id, "status": "unavailable", "reason": "directed_read_not_admitted", "admission": admission}
            task_payload = {"questions": questions, "required_outputs": outputs, "gap_keys": list(dict.fromkeys(_text(row.get("gap_key")) for row in questions if _text(row.get("gap_key"))))}
            output_dir = phase_root / "directed" / _safe_id(paper_id)
            try:
                result = run_directed_reading(
                    request=request,
                    paper=nomination,
                    snapshot_dir=snapshot,
                    output_dir=output_dir,
                    store=store,
                    task=task_payload,
                    key_file=key_file,
                    budget_ledger_path=budget_ledger_path,
                    budget_limit_cny=budget_limit_cny,
                    max_output_tokens=config.chapter_output_tokens,
                    thinking_budget=config.thinking_budget,
                )
                artifact = result.get("output") if isinstance(result.get("output"), Mapping) else {}
                return {**dict(artifact), "paper_id": paper_id, "status": "complete" if result.get("ready") else "partial", "output_dir": str(output_dir), "material": dict(artifact)}
            except Exception as exc:
                return {"paper_id": paper_id, "status": "failed", "error": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=max(1, config.reader_workers)) as executor:
            futures = [executor.submit(read_one, task) for task in tasks]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: _text(row.get("paper_id")))
        attempted = [_text(row.get("paper_id")) for row in results if _text(row.get("paper_id"))]
        consumed = [_text(row.get("paper_id")) for row in results if row.get("status") in {"complete", "partial", "failed"} and _text(row.get("paper_id"))]
        blocked = [_text(row.get("blocked_paper_id")) for row in results if _text(row.get("blocked_paper_id"))]
        materials = [dict(row.get("material") or {}) for row in results if isinstance(row.get("material"), Mapping) and row.get("material")]
        return {"status": "complete" if all(row.get("status") in {"complete", "partial", "reused_prior_deep_read", "review_reported_no_reacquire"} for row in results) else "partial", "results": results, "materials": materials, "consumed_paper_ids": consumed, "attempted_paper_ids": attempted, "blocked_paper_ids": blocked}
    return run


def _tool_material_keys(item: Mapping[str, Any]) -> list[str]:
    """One dedupe key per source handle inside a writer-material item."""

    sources = item.get("sources")
    handles: list[str] = []
    if isinstance(sources, (list, tuple)):
        for source in sources:
            if isinstance(source, Mapping) and _text(source.get("source_handle")):
                handles.append(_text(source["source_handle"]))
    if not handles and _text(item.get("source_handle")):
        handles.append(_text(item["source_handle"]))
    need = _text(item.get("need_id"))
    return [need + "|" + handle for handle in handles] or [need + "|"]


def _tool_material_for_prompt(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact a writer-material record for the chapter-details model input."""

    sources = []
    for source in item.get("sources") or ():
        if not isinstance(source, Mapping):
            continue
        sources.append({
            "source_handle": _text(source.get("source_handle")),
            "title": _text(source.get("title")),
            "year": _text(source.get("year")),
            "reading_role": _text(source.get("reading_role")),
            "material_depth": _text(source.get("material_depth")),
            "section_path": list(source.get("section_path") or []),
        })
    return {
        "need_id": _text(item.get("need_id")),
        "question": _text(item.get("question")),
        "intended_use": _text(item.get("intended_use")),
        "decision": _text(item.get("decision")),
        "usable_content": _text(item.get("usable_content")),
        "conditions": list(item.get("conditions") or []),
        "limits": list(item.get("limits") or []),
        "still_missing": _text(item.get("still_missing")),
        "sources": sources,
        "allowed_use": list(item.get("allowed_use") or []),
    }


def _source_keys_in_value(value: Any) -> tuple[set[str], set[str]]:
    """Collect source handles and paper IDs actually cited by a chapter plan."""
    handles: set[str] = set()
    paper_ids: set[str] = set()

    def add(target: set[str], raw: Any) -> None:
        if isinstance(raw, Mapping):
            raw = raw.get("source_handle") or raw.get("reference_handle") or raw.get("paper_id") or raw.get("canonical_paper_id")
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(target, item)
            return
        text = _text(raw)
        if text:
            target.add(text)

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key in {"source_handle", "reference_handle", "source_handles", "reference_handles"}:
                    add(handles, child)
                elif key in {"source_id", "source_ids", "paper_id", "paper_ids", "canonical_paper_id"}:
                    add(paper_ids, child)
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
        elif isinstance(node, str):
            handles.update(re.findall(r"\bP\d{4,}\b", node))

    visit(value)
    return handles, paper_ids


def _chapter_review_source_materials(
    packet: Mapping[str, Any],
    *,
    include_deep_read: bool = False,
) -> list[dict[str, Any]]:
    """Compact only the A/B material for sources cited by this chapter.

    The whole-plan coordinator gets the compact shape (no deep reads): its job
    is cross-chapter placement, not re-reading every paper.  A chapter owner's
    revision call passes ``include_deep_read=True`` so late paid readings are
    reviewed together with the plan they must correct.
    """
    raw_materials = [item for item in packet.get("source_materials") or [] if isinstance(item, Mapping)]
    handles, paper_ids = _source_keys_in_value(packet.get("chapter_plan") or {})
    by_paper = {
        _text((item.get("paper_id"))): _text(item.get("source_handle"))
        for item in raw_materials
        if _text(item.get("paper_id")) and _text(item.get("source_handle"))
    }
    available_handles = {_text(item.get("source_handle")) for item in raw_materials if _text(item.get("source_handle"))}
    used_handles = handles | {by_paper[paper_id] for paper_id in paper_ids if paper_id in by_paper}
    used_handles.update(paper_id for paper_id in paper_ids if paper_id in available_handles)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_materials:
        handle = _text(item.get("source_handle"))
        if not handle or handle not in used_handles or handle in seen:
            continue
        seen.add(handle)
        row = {
            key: item[key]
            for key in (
                "source_handle", "paper_id", "title", "doi", "year", "material_depth",
                "study_summary_A", "review_planning_B", "supplement_gap_material",
                "supplement_gap_materials", "supplement_material", "supplement_materials",
                "local_passages",
            )
            if key in item
        }
        if include_deep_read and item.get("deep_read_material"):
            row["deep_read_material"] = _compact_reading_material(item.get("deep_read_material"))
        output.append(row)
    return output


def merge_tool_materials_into_packets(
    packets: Sequence[Mapping[str, Any]],
    tool_materials: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach new tool-returned material to the writer packets that should use it.

    Work order 05: material found by the shared queue has to reach the writing
    input, not just the planner's own summary.  A material item carries the
    chapter and unit it was gathered for; the packet keeps every item under the
    program-managed handle so the writer can cite it, and the packet records
    which questions are still open.

    Work order review-v2/01: an item attaches to a single source only when its
    COMPLETE source set is one distinct identity that resolves to a handle
    present in the chapter's own ``source_materials``; it is then merged into
    that source's ``tool_supplement_materials`` so the writing side consumes it
    through the per-source catalog instead of an unread packet top-level list.
    Items citing further sources that never resolved to a handle (paper_id or
    DOI only), several sources, or an unknown handle stay at the chapter level
    with their source set intact; a multi-source synthesis is never folded
    into one paper's record.  ``need_id`` groups rounds of one information
    need: fully identical duplicates fold, a clear update replaces the earlier
    round, complementary content for the same need survives.
    """

    by_chapter: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for raw in tool_materials:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if isinstance(item.get("chapter_ids"), (list, tuple)):
            targets = [_text(value) for value in item["chapter_ids"] if _text(value)]
        else:
            targets = [_text(item.get("chapter_id"))] if _text(item.get("chapter_id")) else []
        for target in targets:
            by_chapter.setdefault(target, []).append(item)
        if not targets:
            unassigned.append(item)

    def item_handles(item: Mapping[str, Any]) -> list[str]:
        handles: list[str] = []
        for source in item.get("sources") or ():
            if isinstance(source, Mapping):
                handle = _text(source.get("source_handle"))
                if handle and handle not in handles:
                    handles.append(handle)
        return handles

    merged: list[dict[str, Any]] = []
    for packet in packets:
        row = dict(packet)
        chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
        chapter_id = _text(row.get("chapter_id") or chapter.get("chapter_id") or chapter.get("id"))
        additions = [*by_chapter.get(chapter_id, []), *unassigned]
        if additions:
            source_rows = [
                dict(item) if isinstance(item, Mapping) else item
                for item in (row.get("source_materials") or [])
            ]
            source_by_handle = {
                _text(item.get("source_handle")): item
                for item in source_rows
                if isinstance(item, Mapping) and _text(item.get("source_handle"))
            }
            existing = [item for item in (row.get("tool_materials") or []) if isinstance(item, Mapping)]
            seen = {key for item in existing for key in _tool_material_keys(item)}
            attached_any = False
            for item in additions:
                handles = item_handles(item)
                identities = distinct_tool_material_sources(item)
                if len(identities) == 1 and len(handles) == 1 and handles[0] in source_by_handle:
                    target = source_by_handle[handles[0]]
                    target["tool_supplement_materials"] = merge_tool_supplement_entry(
                        target.get("tool_supplement_materials") or [], tool_supplement_entry(item))
                    attached_any = True
                    continue
                keys = _tool_material_keys(item)
                if all(key in seen for key in keys):
                    continue
                seen.update(keys)
                existing.append(item)
            if attached_any or existing != list(row.get("tool_materials") or []):
                row["source_materials"] = source_rows
                row["tool_materials"] = [dict(item) for item in existing]
                row["tool_material_count"] = len(existing)
                row["open_questions"] = list(dict.fromkeys(
                    [*(row.get("open_questions") or []),
                     *[_text(entry.get("still_missing"))
                       for source in source_rows if isinstance(source, Mapping)
                       for entry in (source.get("tool_supplement_materials") or [])
                       if isinstance(entry, Mapping) and _text(entry.get("still_missing"))],
                     *[_text(item.get("still_missing")) for item in existing if _text(item.get("still_missing"))]]
                ))
        merged.append(row)
    return merged


def _render_json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n```"


def render_writer_packet_markdown(packet: Mapping[str, Any]) -> str:
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    plan = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
    lines = [f"# {_text(chapter.get('title') or chapter.get('chapter_id'))}", "", f"**Review question:** {_text(packet.get('research_question'))}", "", "## Placement in the shared outline", ""]
    lines.extend(_render_readable(packet.get("shared_outline"), level=3, label="Shared outline"))
    lines.extend(_render_readable(packet.get("adjacent_chapters"), level=3, label="Adjacent chapters"))
    lines.extend(["", "## Writer-ready chapter plan", ""])
    lines.extend(_render_readable(plan, level=3))
    lines.extend(["", "## Supplied source material", ""])
    for row in packet.get("source_materials") or []:
        if not isinstance(row, Mapping):
            continue
        title = _text(row.get("title") or row.get("paper_id"))
        lines.extend([f"### {title}", "", f"- Source handle: `{_text(row.get('source_handle'))}`", f"- Paper ID: `{_text(row.get('paper_id'))}`"])
        if row.get("doi"):
            lines.append(f"- DOI: {_text(row.get('doi'))}")
        for key, label in (
            ("material_depth", "Available material depth"),
            ("study_summary_A", "Paper understanding from A"),
            ("review_planning_B", "Review-planning account from B"),
            ("supplement_gap_material", "Supplement material"),
            ("supplement_gap_materials", "Other supplement material"),
            ("local_passages", "Focused local passages"),
            ("deep_read_material", "Directed-reading material"),
        ):
            if row.get(key):
                lines.append("")
                lines.extend(_render_readable(row[key], level=4, label=label))
        lines.append("")
    lines.extend(["## Citation and scope rules", ""])
    lines.extend(_render_readable(packet.get("citation_namespace_rule") or packet.get("writer_handoff") or {}, level=3))
    return "\n".join(lines).rstrip() + "\n"


_FIELD_LABELS_ZH = {
    "title": "标题", "purpose": "本节作用", "scope": "范围", "thesis": "本章主张",
    "reader_objective": "读者应理解", "units": "论述单元", "subsections": "子节",
    "sections": "章节", "substantive_threads": "论证线索", "point": "核心观点",
    "substantive_point": "核心观点", "ordered_development": "展开顺序",
    "development": "展开方式", "cases": "具体研究与案例", "case": "案例",
    "paper_references": "论文依据", "references": "参考材料", "cross_paper_synthesis": "跨论文综合",
    "synthesis": "跨论文综合", "conditions": "适用条件", "evidence_conditions": "证据条件",
    "limitations": "边界与限制", "limits": "边界与限制", "transition": "承接下一节",
    "transitions": "章节衔接", "chapter_id": "章节编号", "review_title": "综述标题",
    "central_question": "中心问题", "central_thesis": "中心论点", "central_argument": "中心论证",
    "review_argument": "综述论证", "shared_scope": "共同范围", "shared_outline": "共同提纲",
    "material_theme_inventory": "材料主题", "unresolved_limits": "尚存限制",
    "source_handles": "来源", "source_handle": "来源编号", "paper_id": "论文编号",
    "bibliography": "文献信息", "finding": "研究发现", "method": "方法",
    "population": "研究对象", "model": "研究模型", "outcome": "研究结果",
    "comparison": "比较", "evidence_type": "证据类型", "review_reported": "综述转述的原始研究",
    "reason": "说明", "notes": "说明", "harmonization_notes": "统筹说明",
}


def _readable_label(key: str) -> str:
    if key in _FIELD_LABELS_ZH:
        return _FIELD_LABELS_ZH[key]
    return re.sub(r"[_-]+", " ", key).strip().capitalize()


def _render_readable(value: Any, *, level: int = 3, label: str = "") -> list[str]:
    """Render nested plan fields as headings and prose instead of JSON dumps."""
    if value is None or value == "" or value == [] or value == {}:
        return []
    label_text = _readable_label(label) if label else ""
    indent = "  " * max(0, level - 3)
    if isinstance(value, Mapping):
        data = dict(value)
        own_title = _text(data.get("title") or data.get("name") or data.get("label"))
        lines: list[str] = []
        if own_title:
            lines.append("#" * min(6, max(3, level)) + " " + own_title)
        elif label_text:
            lines.append("#" * min(6, max(3, level)) + " " + label_text)
        elif level == 3:
            lines.append("### 规划内容")
        for key, item in data.items():
            if key in {"title", "name", "label"} or item is None or item == "" or item == [] or item == {}:
                continue
            rendered = _render_readable(item, level=min(6, level + 1), label=str(key))
            lines.extend(rendered)
        return lines
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if not items:
            return []
        lines: list[str] = []
        if label_text:
            lines.append("#" * min(6, max(3, level)) + " " + label_text)
        for index, item in enumerate(items, start=1):
            if isinstance(item, Mapping):
                title = _text(item.get("title") or item.get("name") or item.get("unit_title") or item.get("chapter_id"))
                if title:
                    lines.append(f"{indent}- **{title}**")
                    body = {key: child for key, child in item.items() if key not in {"title", "name", "unit_title", "chapter_id"} and child not in (None, "", [], {})}
                    for key, child in body.items():
                        lines.extend(_render_readable(child, level=min(6, level + 1), label=str(key)))
                else:
                    lines.append(f"{indent}- **{index}.**")
                    lines.extend(_render_readable(item, level=min(6, level + 1)))
            elif isinstance(item, (list, tuple)):
                lines.extend(_render_readable(item, level=min(6, level + 1)))
            else:
                lines.append(f"{indent}- {_text(item)}")
        return lines
    text = _text(value)
    if not text:
        return []
    if label_text:
        return [f"{indent}- **{label_text}:** {text}"]
    return [f"{indent}{text}"]


def render_plan_markdown(plan: Mapping[str, Any]) -> str:
    provisional = plan.get("provisional_scope") if isinstance(plan.get("provisional_scope"), Mapping) else {}
    title = _text(plan.get("review_title") or provisional.get("review_title") or plan.get("research_question") or "综述规划")
    lines = [f"# {title}", "", f"**研究问题：** {_text(plan.get('research_question'))}", f"**状态：** {_text(plan.get('status'))}", f"**规划阶段：** {_text(plan.get('completed_through') or '详细规划')}", ""]
    scope = plan.get("shared_scope") or plan.get("provisional_scope") or {}
    lines.extend(["## 综述范围与论证主线", ""])
    lines.extend(_render_readable(scope, level=3))
    lines.extend(["", "## 共同提纲", ""])
    outline = plan.get("shared_outline") or {}
    lines.extend(_render_readable(outline, level=3))
    lines.extend(["", "## 章节写作计划", ""])
    for row in plan.get("chapters") or []:
        if not isinstance(row, Mapping):
            continue
        chapter = row.get("chapter") if isinstance(row.get("chapter"), Mapping) else {}
        chapter_title = _text(chapter.get("title") or chapter.get("chapter_id") or "章节")
        lines.extend([f"### {chapter_title}", ""])
        for key in ("purpose", "scope", "substantive_threads"):
            if chapter.get(key):
                lines.extend(_render_readable(chapter[key], level=4, label=key))
        lines.extend(_render_readable(row.get("chapter_plan"), level=4, label="chapter_plan"))
        lines.append("")
    lines.extend(["## 写作材料包", ""])
    for row in plan.get("writer_packets") or []:
        if isinstance(row, Mapping):
            lines.append(f"- **{_text(row.get('chapter_id'))}:** {_text(row.get('source_material_count'))} 篇已整理来源；文件：`{_text(row.get('markdown_path'))}`")
    if plan.get("candidate_screening"):
        screening = plan["candidate_screening"]
        lines.extend(["", "## 材料覆盖", "", f"已读取完整 B 池，共 {screening.get('pool_rows_read', 0)} 条；其中 {screening.get('unique_sources_assigned', 0)} 篇至少分配到一章。未分配材料仍保留在 JSON 筛选记录中，文献数量不作为硬性门槛。"])
    if plan.get("whole_plan_improvement"):
        lines.extend(["", "## 全篇统筹调整", ""])
        lines.extend(_render_readable(plan["whole_plan_improvement"], level=3))
    if plan.get("deep_read_budget"):
        lines.extend(["", "## 定向精读", ""])
        lines.extend(_render_readable(plan["deep_read_budget"], level=3))
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "SCHEMA_VERSION", "ProgressivePlanError", "ProgressivePlannerConfig", "ProgressiveReviewPlanner",
    "QwenProgressivePlanner", "load_original_plan", "load_planning_pool", "load_prior_readings", "qwen_local_token_counter",
    "make_planning_supplement_runner", "make_retrieval_loop_runner", "make_directed_reading_runner",
    "render_plan_markdown", "render_writer_packet_markdown",
]
