"""Local triage and targeted local reading for review-planning gaps.

Work order 02 of ``docs/planning_work_orders/20260926_content_driven_retrieval``.

Work order 01 proved that the topic's registered local material can answer real
writing questions.  This module turns that search into a decision, taken
*before* any external retrieval:

``direct_use``
    The local material already carries what the section needs.  The section is
    served with zero downloads and without re-reading a whole paper.
``local_deep_read``
    The paper is in the pool and its captured full text covers the question, but
    the planning card is too shallow.  A bounded, question-shaped read of the
    stored material is queued; the existing A/B card is never overwritten.
``external_research``
    The local material does not contain the needed object, comparison or study
    design.  A concrete external request is emitted for work order 03.

Only the content explanation is delegated to Qwen.  Paper identity, counts,
quota accounting and file layout are filled by this program.  The module does
not call any retrieval provider, and it does not re-run the A/B card prompts.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .planning_material_search import (
    PlanningMaterialIndex,
    SearchHit,
    SearchResult,
    _adjacent_terms,
    paper_context,
    nominated_paper_passage,
    nominated_paper_material,
    search,
)

SCHEMA_VERSION = "optomind.planning_material_triage.v1"
READING_SCHEMA_VERSION = "optomind.planning_material_local_reading.v1"
REQUEST_SCHEMA = "optomind.planning_material_external_request.v1"

DEFAULT_TRIAGE_MAX_OUTPUT_TOKENS = 12000
DEFAULT_TRIAGE_THINKING_BUDGET = 8192
DEFAULT_INTERPRETATION_MAX_OUTPUT_TOKENS = 8192
DEFAULT_INTERPRETATION_THINKING_BUDGET = 8192

DECISIONS = ("direct_use", "local_deep_read", "external_research")

#: What the section wants from the material.  A background mention and a
#: quantitative comparison put very different demands on the same abstract.
USES = ("background", "mechanism", "comparison", "quantification", "application_outcome", "study_design")

# ``clinical_effect`` appeared in earlier planner requests.  Accept it at the
# boundary, but keep the exposed category broad enough for non-clinical
# applications and outcomes.
INTENDED_USE_ALIASES = {
    "clinical_effect": "application_outcome",
    "clinical_evidence": "application_outcome",
}


def normalize_intended_use(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    return INTENDED_USE_ALIASES.get(normalized, normalized)

#: Uses that a shallow abstract can legitimately serve on its own.
SHALLOW_OK_USES = {"background"}

#: Uses that require the material to report the specific object being asked about.
SPECIFIC_USES = {"mechanism", "comparison", "quantification", "application_outcome", "study_design"}
QUANTITATIVE_USES = {"quantification", "application_outcome"}

MAX_PASSAGES = 5
PASSAGE_CHARS = 1100
LOCAL_EXPANSION_PAPERS = 12
LOCAL_INCREMENT_CHARS = 24000

TRIAGE_SYSTEM_PROMPT = """你是综述作者的材料阅读助手，围绕一个写作问题提炼具体研究认识。
一起阅读 material_found 和 paper_context，保留研究对象或材料、研究设置、比较对象、方法、操作或条件（如有）、结果或论证、验证方式与适用边界。卡片、专家综述转述和原文都按内容是否能回答问题使用，不因形式自动降权。
先理解 user_scope 与问题对象，不能用其他对象、方法、设置或比较冒充本问题的答案。特定模型、材料或场景的结果不得无依据外推到别的对象或设置；观察关联不改写为因果关系；统计或分析方法也不自动证明因果、排除混杂或完成独立验证，方法意义只说材料明确说明的部分。
输出 usable_content 为一段可直接阅读的字符串：适合本节的具体认识、研究异同、解释和条件。先呈现能区别不同研究的发现或论证，不先套领域常见结论；结尾也要保留这些区别。别把多个不同对象、材料、产品、场景或实验设置的结果合成一个普适机制或结论。
涉及数字时，先逐项输出 quantitative_comparisons，每项写 research_object、result_or_measure、setting_or_time、groups；groups 是 [{group:原文组名或缩写,value:紧邻该组的结果含单位}]，保留原文分组顺序与缩写，不重新命名或调序；缩写定义另写 group_definitions。逐字读取每个组名紧随的数值，不从常识、总体结论或组名列表反推配对。reported_statistics 记录原文给出的统计量（没报告才留空）。一组一数成对，不用多组名字对应一串数字；总体与分层比较、不同指标或结果分别记录。usable_content 解释含义，不重复重排数字。原文没有的值不编。问题限定某设置、时间或子组时，必须分别读总体和指定部分，不能拿总体结果代替，也不能把分析时点当成条件发生时点。
decision 判断当前具体问题是否解决，而不是材料有没有用：direct_use=足以回答当前问题，或有明确材料纠正其错误前提；local_deep_read=本地有材料但需要读另一部分或完整比较（read_focus 说明具体要读什么）；external_research=缺新的研究对象、方法、比较或验证材料（external_ask 提出保留对象和关系的窄需求）。external_research 也可以有丰富 usable_content：已有背景可用但核心问题没回答，就先保留内容并提出剩余检索需求。不要因为找到相关材料就宣布 direct_use。问题里的预设（例如研究阶段、设计、测量或效果）不是事实，先以材料校正。
对于“是否已经有某类研究/结果”的问题，找到一个符合条件的实例可以回答；几篇论文未提到，或一篇较早综述说当时没有，不能回答当前是否存在。已有内容可以说明截至该来源时间的研究格局；若仍需确认后来是否出现目标研究，就选择 external_research 并提出该项具体检索，不要一边宣布问题已解决一边仍要求确认其核心答案。只有问题明确限定在这些来源的时间或材料范围内，才可据此直接给出否定答案。
answers_requested_question 只表示当前材料是否真正回答了原问题和 success_criteria；有用但只覆盖背景或部分条件的内容填 false，明确的实验性阴性结果或明确限于这些来源/时间范围的否定答案可填 true。
reading_mode=focused_local_read 时已经执行补读，尽力完成具体认识；仍缺新材料则指出缺口，不循环要求读同一材料。摘要能回答背景或明确比较就可用，不强制全文。
先完成 quantitative_comparisons，再写 usable_content。必须先逐组读原句，不根据总体方向猜测分组方向；结果可以与提问隐含的方向相反。某部分出现反向或无差异时，综合开头就写清不同设置或对象结果不一致，不能先说“一致”再在后面补相反数据。同一指标的总体比较和按条件分层不是同一组数据，不得互换。
返回 JSON，按此顺序写字段：quantitative_comparisons（无数字则 []）, usable_content, decision, answers_requested_question（必须是 JSON boolean；不能确定或 success_criteria 未满足时填 false）, still_missing, read_focus, external_ask, reason。
"""


class PlanningMaterialTriageError(ValueError):
    """Invalid triage input or unusable judge response."""


@dataclass(frozen=True)
class LocalGap:
    """One concrete question a section wants answered from local material."""

    gap_id: str
    question: str
    intended_use: str = "mechanism"
    user_scope: str = ""
    success_criteria: tuple[str, ...] = ()
    chapter_ids: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    required_concepts: tuple[str, ...] = ()
    existing_handles: tuple[str, ...] = ()
    existing_paper_ids: tuple[str, ...] = ()
    current_source_handles: tuple[tuple[str, str], ...] = ()
    reusable_material: str = ""

    def __post_init__(self) -> None:
        if not str(self.gap_id or "").strip():
            raise PlanningMaterialTriageError("gap_id_required")
        if not str(self.question or "").strip():
            raise PlanningMaterialTriageError("gap_question_required")
        normalized_use = normalize_intended_use(self.intended_use)
        if normalized_use not in USES:
            raise PlanningMaterialTriageError("unknown_intended_use:" + str(self.intended_use))
        if normalized_use != self.intended_use:
            object.__setattr__(self, "intended_use", normalized_use)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "question": self.question,
            "intended_use": self.intended_use,
            "user_scope": self.user_scope,
            "success_criteria": list(self.success_criteria),
            "chapter_ids": list(self.chapter_ids),
            "concepts": list(self.concepts),
            "required_concepts": list(self.required_concepts),
            "existing_handles": list(self.existing_handles),
            "existing_paper_ids": list(self.existing_paper_ids),
            "current_source_handles": dict(self.current_source_handles),
        }


@dataclass
class LocalReadingPassage:
    """One bounded read of stored material for one question."""

    source_handle: str
    paper_id: str
    title: str
    year: str
    doi: str
    reading_role: str  # direct_evidence | planning_summary
    text: str
    best_sentence: str
    section_path: tuple[str, ...]
    material_depth: str
    reading_path: str
    card_path: str
    from_existing_field: bool
    #: The individual result statements inside this segment, with their explicit
    #: group/comparison labels.  Work order 06 measured that a single flattened
    #: passage lets a judge attach a number to the wrong group; the labelled
    #: statements are what the judge is asked to quote from.
    key_sentences: tuple[str, ...] = ()
    selection_reason: str = "query_match"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_handle": self.source_handle,
            "paper_id": self.paper_id,
            "title": self.title,
            "year": self.year,
            "doi": self.doi,
            "reading_role": self.reading_role,
            "text": self.text,
            "best_sentence": self.best_sentence,
            "key_sentences": list(self.key_sentences),
            "section_path": list(self.section_path),
            "material_depth": self.material_depth,
            "reading_path": self.reading_path,
            "card_path": self.card_path,
            "from_existing_field": self.from_existing_field,
            "selection_reason": self.selection_reason,
        }


@dataclass
class LocalReadingBundle:
    gap: LocalGap
    passages: list[LocalReadingPassage] = field(default_factory=list)
    searched_papers: int = 0
    downloads: int = 0
    whole_paper_rereads: int = 0
    search_found: bool = False
    not_matched_reason: str = ""
    new_task_fields: tuple[str, ...] = ()
    required_concepts_all_missing: tuple[str, ...] = ()
    paper_contexts: list[Any] = field(default_factory=list)
    bounded_read_omissions: tuple[str, ...] = ()
    explicit_reading_status: dict[str, str] = field(default_factory=dict)

    @property
    def direct_evidence(self) -> list[LocalReadingPassage]:
        return [item for item in self.passages if item.reading_role == "direct_evidence"]

    @property
    def planning_summary(self) -> list[LocalReadingPassage]:
        return [item for item in self.passages if item.reading_role == "planning_summary"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": READING_SCHEMA_VERSION,
            "gap": self.gap.to_dict(),
            "passages": [item.to_dict() for item in self.passages],
            "searched_papers": self.searched_papers,
            "downloads": self.downloads,
            "whole_paper_rereads": self.whole_paper_rereads,
            "search_found": self.search_found,
            "not_matched_reason": self.not_matched_reason,
            "new_task_fields": list(self.new_task_fields),
            "required_concepts_all_missing": list(self.required_concepts_all_missing),
            "bounded_read_omissions": list(self.bounded_read_omissions),
            "explicit_reading_status": dict(self.explicit_reading_status),
            "paper_contexts": [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in self.paper_contexts
            ],
        }


@dataclass
class TriageJudgment:
    gap: LocalGap
    decision: str
    usable_content: str = ""
    still_missing: str = ""
    read_focus: str = ""
    external_ask: str = ""
    reason: str = ""
    source: str = ""  # "judge" | "policy"
    passages: list[LocalReadingPassage] = field(default_factory=list)
    local_reading: LocalReadingBundle | None = None
    cost_cny: float = 0.0
    model_calls: int = 0
    quantitative_comparisons: list[dict[str, Any]] = field(default_factory=list)
    # Optional so old injected judges and saved fixtures retain their legacy
    # routing when they do not provide the new semantic answer signal.
    answers_requested_question: bool | None = None

    @property
    def material_ready(self) -> bool:
        return self.decision == "direct_use" and bool(self.usable_content.strip())

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "gap": self.gap.to_dict(),
            "decision": self.decision,
            "usable_content": self.usable_content,
            "still_missing": self.still_missing,
            "read_focus": self.read_focus,
            "external_ask": self.external_ask,
            "reason": self.reason,
            "source": self.source,
            "material_ready": self.material_ready,
            "passages": [item.to_dict() for item in self.passages],
            "local_reading": self.local_reading.to_dict() if self.local_reading else None,
            "cost_cny": round(float(self.cost_cny), 6),
            "model_calls": int(self.model_calls),
            "quantitative_comparisons": self.quantitative_comparisons,
        }
        if self.answers_requested_question is not None:
            result["answers_requested_question"] = bool(self.answers_requested_question)
        return result

    def external_request(self) -> dict[str, Any]:
        """The concrete request work order 03 consumes when local material fails."""

        request = {
            "schema_version": REQUEST_SCHEMA,
            "gap_id": self.gap.gap_id,
            "gap_question": self.gap.question,
            "intended_use": self.gap.intended_use,
            "user_scope": self.gap.user_scope,
            "success_criteria": list(self.gap.success_criteria),
            "chapter_ids": list(self.gap.chapter_ids),
            "requested_object": self.external_ask,
            "still_missing": self.still_missing,
            "local_evidence_reviewed": [
                {"source_handle": item.source_handle, "section_path": list(item.section_path)}
                for item in self.passages
            ],
            "local_reading_cost": {"downloads": 0, "whole_paper_rereads": 0},
        }
        if self.answers_requested_question is not None:
            request["answers_requested_question"] = bool(self.answers_requested_question)
        return request


# --------------------------------------------------------------------------
# Local reading
# --------------------------------------------------------------------------


def _reading_role(hit: SearchHit) -> str:
    """Captured body text is direct evidence; a planning card is a summary."""

    if hit.segment_kind.startswith("card_") or hit.segment_kind.startswith("directed_"):
        return "planning_summary"
    return "direct_evidence"


def _clip(text: str, limit: int = PASSAGE_CHARS) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + " …"


_NUMERIC_CLAIM = re.compile(r"\d")


def _key_sentences(text: str, question: str, limit: int = 6) -> tuple[str, ...]:
    """Statement-level claims inside a passage, with their own group labels.

    A result sentence that names both sides of a comparison ("ABT exposure was
    negatively associated with rwOS ... [23.9 v 33.6 months]") is what a writer
    can quote safely; a flattened passage is not.  Sentences that carry a number
    or a label are kept verbatim, in reading order, so the reviewer sees the
    comparison as the paper stated it.
    """

    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return ()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    out: list[str] = []
    for sentence in sentences:
        if _NUMERIC_CLAIM.search(sentence) and len(sentence) > 40:
            out.append(sentence)
        if len(out) >= limit:
            break
    if out:
        return tuple(out)
    return tuple(sentences[:limit])


def _expanded_hit_text(index: PlanningMaterialIndex, hit: SearchHit) -> str:
    """Read intact indexed sections instead of cutting comparison sentences."""
    if hit.segment_kind != "document_block":
        return hit.text
    siblings = index.segments_for(hit.paper_id, hit.segment_kind)
    positions = [i for i, r in enumerate(siblings) if str(r["section_path"]) == " / ".join(hit.section_path)]
    rows = siblings[max(0,min(positions)-1):max(positions)+4] if positions else []
    if not rows:
        return hit.text
    from .planning_material_search import _is_non_evidence_material
    rows = [row for row in rows if not _is_non_evidence_material(
        str(row["text"]), tuple(str(row["section_path"]).split(" / ")))]
    if not rows:
        return hit.text
    if sum(len(r["text"]) for r in rows) > 12000:
        terms = set(re.findall(r"[A-Za-z0-9-]+", hit.best_sentence.casefold()))
        pos = max(range(len(rows)), key=lambda i: len(terms & set(re.findall(r"[A-Za-z0-9-]+", str(rows[i]["text"]).casefold()))))
        rows = rows[max(0,pos-2):pos+4]
    body = ""
    for row in rows:
        text = re.sub(r"\s+", " ", str(row["text"])).strip()
        if not text or text in body:
            continue
        overlap = next((n for n in range(min(len(body),len(text),180),15,-1) if body.endswith(text[:n])), 0)
        body += (" " if body and not overlap else "") + text[overlap:]
    return body


def _reading_passage(index: PlanningMaterialIndex, gap: LocalGap, hit: SearchHit,
                     *, text: str | None = None) -> LocalReadingPassage:
    body = _expanded_hit_text(index, hit) if text is None else text
    handles = dict(gap.current_source_handles)
    # Historical labels are foreign even when currently unoccupied: a later
    # pool addition can claim that number. Only this run's identity map may
    # provide a handle; otherwise keep the useful material under its stable ID.
    handle = handles.get(hit.paper_id) or hit.paper_id
    return LocalReadingPassage(
        source_handle=handle, paper_id=hit.paper_id, title=hit.title,
        year=hit.year, doi=hit.doi, reading_role=_reading_role(hit), text=body,
        best_sentence=_clip(hit.best_sentence, 500), section_path=hit.section_path,
        material_depth=hit.material_depth, reading_path=hit.reading_path,
        card_path=hit.card_path,
        from_existing_field=(hit.paper_id in gap.existing_paper_ids or handle in gap.existing_handles),
        key_sentences=_key_sentences(body, gap.question),
        selection_reason="query_match" if hit.match_terms else "explicit_nomination_fallback",
    )


def _append_bounded_hits(index: PlanningMaterialIndex, bundle: LocalReadingBundle,
                         hits: Sequence[SearchHit], *, budget: int = LOCAL_INCREMENT_CHARS) -> int:
    """Add intact relevant passages, first giving unread papers a fair share.

    The budget counts exactly the added material text sent to the judge. No
    additional opening/context is added here; the initial contexts are retained.
    Oversized sections fall back to the intact search segment, never a prefix
    that could drop a condition or reverse the reported result.
    """
    represented = {p.paper_id for p in bundle.passages}
    first, rest, seen = [], [], set(represented)
    for hit in hits:
        if hit.paper_id not in seen:
            first.append(hit)
            seen.add(hit.paper_id)
        else:
            rest.append(hit)
    remaining = max(0, int(budget))
    count = 0
    deferred: list[SearchHit] = []
    omitted: list[str] = []

    def append(hit: SearchHit, limit: int) -> bool:
        nonlocal remaining, count
        expanded = _expanded_hit_text(index, hit) if hit.match_terms else hit.text
        if any(p.paper_id == hit.paper_id and (expanded == p.text or hit.text in p.text)
               for p in bundle.passages):
            return True
        body = expanded if len(expanded) <= min(6000, limit) else hit.text
        if not body.strip() or len(body) > limit:
            return False
        bundle.passages.append(_reading_passage(index, bundle.gap, hit, text=body))
        remaining -= len(body)
        count += 1
        return True

    for position, hit in enumerate(first):
        if not append(hit, remaining // max(1, len(first) - position)):
            deferred.append(hit)
    # A long first passage must get a second opportunity with the spare budget;
    # per-paper fair shares are not a second permanent candidate cutoff.
    for hit in [*deferred, *rest]:
        if not append(hit, remaining):
            omitted.append(hit.paper_id)
    bundle.bounded_read_omissions = tuple(dict.fromkeys([*bundle.bounded_read_omissions, *omitted]))

    return count


def prepare_local_reading(
    index: PlanningMaterialIndex,
    gap: LocalGap,
    *,
    max_passages: int = MAX_PASSAGES,
    passages_per_paper: int = 2,
    top_papers: int = 6,
) -> tuple[LocalReadingBundle, SearchResult]:
    """Read the stored material for one question without downloading anything."""

    result = search(
        index,
        gap.question,
        concepts=list(gap.concepts),
        required_concepts=list(gap.required_concepts),
        top_papers=top_papers,
        passages_per_paper=passages_per_paper,
    )
    bundle = LocalReadingBundle(
        gap=gap,
        searched_papers=result.matched_papers,
        downloads=0,
        whole_paper_rereads=0,
        search_found=result.found,
        not_matched_reason=result.not_matched_reason,
    )
    for hit in result.hits[: max(1, int(max_passages))]:
        bundle.passages.append(_reading_passage(index, gap, hit))
    initial_paper_ids = list(dict.fromkeys(p.paper_id for p in bundle.passages))
    # Explicit identities add reading opportunities, never a relevance verdict.
    # Keep the ordinary selection intact; the extra input has its own hard cap.
    pinned_hits = []
    for paper_id in gap.existing_paper_ids[:LOCAL_EXPANSION_PAPERS]:
        if index.paper(paper_id) is None:
            bundle.explicit_reading_status[paper_id] = "not_indexed"
            continue
        selected = nominated_paper_material(
            index, gap.question, paper_id, concepts=gap.concepts,
            required_concepts=gap.required_concepts,
        )
        if not selected:
            fallback = nominated_paper_passage(index, paper_id)
            if fallback is not None:
                selected = [fallback]
            else:
                bundle.explicit_reading_status[paper_id] = "no_substantive_indexed_passage"
        pinned_hits.extend(selected)
    bundle.bounded_read_omissions = gap.existing_paper_ids[LOCAL_EXPANSION_PAPERS:]
    # Reserve actual additional opening/section text in the same 24K allowance.
    # Initial contexts remain byte-for-byte unchanged for ordinary callers.
    additional_contexts = paper_context(index, [pid for pid in gap.existing_paper_ids[:LOCAL_EXPANSION_PAPERS]
        if pid not in initial_paper_ids[:4] and any(hit.paper_id == pid for hit in pinned_hits)],
        opening_chars=400, max_sections=6, substantive_only=True)
    for context in additional_contexts:
        context.sections = tuple(section[:100] for section in context.sections)
    context_chars = sum(len(c.opening) + sum(map(len, c.sections)) for c in additional_contexts)
    _append_bounded_hits(index, bundle, pinned_hits, budget=LOCAL_INCREMENT_CHARS-context_chars)
    for paper_id in gap.existing_paper_ids:
        if any(p.paper_id == paper_id for p in bundle.passages):
            bundle.explicit_reading_status[paper_id] = ("included_nomination_fallback"
                if any(p.paper_id == paper_id and p.selection_reason == "explicit_nomination_fallback" for p in bundle.passages)
                else "included")
        elif paper_id in gap.existing_paper_ids[LOCAL_EXPANSION_PAPERS:]:
            bundle.explicit_reading_status[paper_id] = "candidate_cap"
        elif paper_id not in bundle.explicit_reading_status:
            bundle.explicit_reading_status[paper_id] = "passage_budget"
    bundle.search_found = bool(bundle.passages)
    if bundle.search_found:
        bundle.not_matched_reason = ""
    bundle.searched_papers = len({hit.paper_id for hit in [*result.hits, *pinned_hits]})
    bundle.new_task_fields = (
        ("question", "intended_use", "read_focus") if bundle.direct_evidence else ()
    )
    if gap.required_concepts:
        # Each hard requirement is checked against a single passage, never
        # against the concatenation of everything: "tumour microenvironment" in
        # one paper and "inosine" in another does not answer a question that
        # needs both together.
        bundle.required_concepts_all_missing = tuple(
            concept for concept in gap.required_concepts
            if not any(_concept_present(concept, item.text) for item in bundle.passages)
        )
    # A bounded read of the papers it actually matched: their opening and a
    # section map, so a fact the same paper states outside the matched segment is
    # still visible without re-reading the whole paper.
    matched_paper_ids = list(dict.fromkeys(item.paper_id for item in bundle.passages if item.paper_id))
    if matched_paper_ids:
        bundle.paper_contexts = [*paper_context(index, initial_paper_ids[:4]),
            *(context for context in additional_contexts if context.paper_id in matched_paper_ids)]
        handles = dict(gap.current_source_handles)
        for context in bundle.paper_contexts:
            context.source_handle = handles.get(context.paper_id) or context.paper_id
    return bundle, result


def _concept_present(concept: str, text: str) -> bool:
    """True when the concept's own words sit together in this passage."""

    from .planning_material_search import _query_pairs, concept_term, tokenize

    try:
        term = concept_term(concept)
    except Exception:
        return True
    forms: set[str] = set()
    for form in term.forms:
        forms.update(tokenize(form))
    if not forms:
        return True
    if not (forms & set(tokenize(text))):
        return False
    pairs = _query_pairs([term])
    if not pairs:
        return True
    return _adjacent_terms(text, forms, pairs)


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


def _triage_payload(gap: LocalGap, bundle: LocalReadingBundle) -> dict[str, Any]:
    payload = {
        "gap_id": gap.gap_id,
        "question": gap.question,
        "intended_use": gap.intended_use,
        "user_scope": gap.user_scope,
        "success_criteria": list(gap.success_criteria),
        "reading_mode": "focused_local_read" if "focused_local_read" in bundle.new_task_fields else "initial_read",
        "material_found": [
            {
                "source_handle": item.source_handle,
                "type": item.reading_role,
                "material_depth": item.material_depth,
                "section": list(item.section_path),
                "text": item.text,
            }
            for item in bundle.passages
        ],
        "paper_context": [
            {
                "source_handle": item.source_handle,
                "opening": item.opening,
                "sections": list(item.sections),
            }
            for item in bundle.paper_contexts
        ],
        "local_search": {
            "found": bundle.search_found,
            "not_matched_reason": bundle.not_matched_reason,
            "papers_seen": bundle.searched_papers,
        },
    }

    if gap.reusable_material:
        payload["reusable_material"] = gap.reusable_material
    return payload


def _policy_decision(gap: LocalGap, bundle: LocalReadingBundle) -> TriageJudgment:
    """Deterministic triage used when no judge is wired or the judge is unusable.

    The policy is deliberately conservative: it never claims the material
    answers a specific question, it only decides where the work should go next.
    A question whose own hard requirement (for example a named compartment or
    assay) matches no local passage cannot be served by "reading deeper" in the
    same pool, so it is routed to external research.
    """

    if not bundle.passages:
        return TriageJudgment(
            gap=gap,
            decision="external_research",
            still_missing=gap.question,
            external_ask=(
                f"Local material does not cover: {gap.question} "
                f"(intended use: {gap.intended_use}). Retrieve primary studies that measure the requested object."
            ),
            reason="no_local_passage_matched:" + (bundle.not_matched_reason or "unknown"),
            source="policy",
            passages=bundle.passages,
            local_reading=bundle,
        )
    if bundle.required_concepts_all_missing:
        return TriageJudgment(
            gap=gap,
            decision="external_research",
            still_missing=gap.question,
            external_ask=(
                f"Local material has no passage for the required element(s) "
                f"{', '.join(bundle.required_concepts_all_missing)} of: {gap.question}"
            ),
            reason="required_concept_absent_from_every_local_passage",
            source="policy",
            passages=bundle.passages,
            local_reading=bundle,
        )
    if bundle.direct_evidence:
        return TriageJudgment(
            gap=gap,
            decision="local_deep_read",
            usable_content="",
            still_missing=gap.question,
            read_focus=f"Read the captured body sections listed in passages for: {gap.question}",
            reason="captured_body_text_available_without_a_question_shaped_read",
            source="policy",
            passages=bundle.passages,
            local_reading=bundle,
        )
    return TriageJudgment(
        gap=gap,
        decision="local_deep_read",
        usable_content="",
        still_missing=gap.question,
        read_focus=f"Inspect the captured material for: {gap.question}",
        reason="only_card_summary_matched;_check_the_captured_material",
        source="policy",
        passages=bundle.passages,
        local_reading=bundle,
    )


def _normalize_judgment(raw: Mapping[str, Any], gap: LocalGap) -> dict[str, Any]:
    decision = str(raw.get("decision") or "").strip().casefold()
    if decision not in DECISIONS:
        raise PlanningMaterialTriageError("judge_decision_invalid:" + decision)

    def readable(value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("content", "text", "finding", "summary"):
                if value.get(key):
                    return readable(value[key])
            return "\n".join(f"{key}: {readable(item)}" for key, item in value.items() if item)
        if isinstance(value, (list, tuple)):
            return "\n".join(readable(item) for item in value if item)
        return str(value or "").strip()

    def text(key: str) -> str:
        return readable(raw.get(key))

    answer_signal = raw.get("answers_requested_question")
    answers_requested_question = answer_signal if isinstance(answer_signal, bool) else None
    normalized = {
        "decision": decision,
        "usable_content": text("usable_content"),
        "still_missing": text("still_missing"),
        "read_focus": text("read_focus"),
        "external_ask": text("external_ask"),
        "reason": text("reason"),
        "quantitative_comparisons": [dict(row) for row in (raw.get("quantitative_comparisons") or []) if isinstance(row, Mapping)],
    }
    if answers_requested_question is not None:
        normalized["answers_requested_question"] = answers_requested_question
    return normalized


class QwenLocalTriageJudge:
    """One small direct-Qwen judgment per gap, charging the shared CNY ledger."""

    def __init__(
        self,
        *,
        key_file: str | Path,
        budget_ledger_path: str | Path,
        budget_limit_cny: float,
        model: str = "qwen3.7-flash",
        max_output_tokens: int = DEFAULT_TRIAGE_MAX_OUTPUT_TOKENS,
        thinking_budget: int = DEFAULT_TRIAGE_THINKING_BUDGET,
        timeout_seconds: float = 300.0,
        raw_response_dir: str | Path | None = None,
        quantitative_model: str | None = "qwen3.5-plus",
        interpretation_max_output_tokens: int = DEFAULT_INTERPRETATION_MAX_OUTPUT_TOKENS,
        interpretation_thinking_budget: int = DEFAULT_INTERPRETATION_THINKING_BUDGET,
    ) -> None:
        if not key_file or not budget_ledger_path:
            raise PlanningMaterialTriageError("triage_key_and_ledger_required")
        self.key_file = Path(key_file)
        self.budget_ledger_path = Path(budget_ledger_path)
        self.budget_limit_cny = float(budget_limit_cny)
        self.model = model
        self.quantitative_model = quantitative_model
        self.max_output_tokens = max(64, int(max_output_tokens))
        self.thinking_budget = int(thinking_budget)
        self.interpretation_max_output_tokens = max(64, int(interpretation_max_output_tokens))
        self.interpretation_thinking_budget = int(interpretation_thinking_budget)
        self.timeout_seconds = float(timeout_seconds)
        self.raw_response_dir = Path(raw_response_dir) if raw_response_dir else None

    def __call__(self, gap: LocalGap, bundle: LocalReadingBundle) -> dict[str, Any]:
        from .module4.runtime import GlobalBudgetLedger, QwenDirectClient, invoke_client

        model = self.quantitative_model if self.model == "qwen3.7-flash" and self.quantitative_model and gap.intended_use in QUANTITATIVE_USES else self.model
        ledger = GlobalBudgetLedger(limit_cny=self.budget_limit_cny, path=self.budget_ledger_path)
        client = QwenDirectClient(
            model=model,
            key_file=self.key_file,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            max_retries=0,
            thinking=True,
            thinking_budget=self.thinking_budget,
            json_mode=model == "qwen3.7-flash",
            raw_response_dir=(self.raw_response_dir / str(time.time_ns())) if self.raw_response_dir else None,
            budget_ledger=ledger,
        )
        messages = [
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_triage_payload(gap, bundle), ensure_ascii=False, sort_keys=True)},
        ]
        self.last_call_count = 1
        raw = invoke_client(
            client,
            messages,
            model=model,
            max_output_tokens=self.max_output_tokens,
            thinking=True,
            thinking_budget=self.thinking_budget,
            call_id=f"planning-material-triage:{gap.gap_id}",
        )
        decoded = self._decode(raw.get("content"))
        comparisons = decoded.get("quantitative_comparisons") or []
        if comparisons and gap.intended_use in QUANTITATIVE_USES:
            # Extraction and synthesis are different tasks: interpret the paired
            # groups without the long source narrative dominating the conclusion.
            self.last_call_count += 1
            summary = invoke_client(client, [
                {"role": "system", "content": "你把已经读出的分组结果组织为综述素材。先对比用户问的那个设置、时间或子组，不用总体结果冒充指定部分。不同设置、对象或结果方向不同时，开头明确说明不一致，再解释哪些比较提示正向、负向或无明显差异。原文缩写按group_definitions解释，不调换组名。比大小不等于统计显著，总体或多组统计量不代表任意两组显著，更不证明哪组驱动总体结果；显著性只依据reported_statistics；缺少某种统计量不等于没有统计检验，不把观察性差异说成操作造成，也不猜论文未报告的机制。写一个紧凑usable_content和适用条件；不要重复整张数字表。返回JSON {usable_content:...}。"},
                {"role": "user", "content": json.dumps({"question": gap.question, "review_scope": gap.user_scope,
                    "comparisons": comparisons, "remaining_information": decoded.get("still_missing")}, ensure_ascii=False)},
            ], model=model, max_output_tokens=self.interpretation_max_output_tokens, thinking=True, thinking_budget=self.interpretation_thinking_budget,
               call_id=f"planning-material-interpretation:{gap.gap_id}")
            interpreted = self._decode(summary.get("content"))
            if interpreted.get("usable_content"):
                decoded["usable_content"] = interpreted["usable_content"]
        return decoded

    @staticmethod
    def _decode(content: Any) -> dict[str, Any]:
        if isinstance(content, Mapping):
            return dict(content)
        if isinstance(content, str):
            try:
                decoded = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE))
            except json.JSONDecodeError as exc:
                raise PlanningMaterialTriageError("triage_response_not_json") from exc
            if isinstance(decoded, Mapping):
                return dict(decoded)
        raise PlanningMaterialTriageError("triage_response_shape_invalid")


Judge = Callable[[LocalGap, LocalReadingBundle], Mapping[str, Any]]


def triage_gap(
    index: PlanningMaterialIndex,
    gap: LocalGap,
    *,
    judge: Judge | None = None,
    max_passages: int = MAX_PASSAGES,
    top_papers: int = 6,
    passages_per_paper: int = 2,
    policy_override: Mapping[str, str] | None = None,
) -> TriageJudgment:
    """Decide direct use / local deep read / external research for one gap.

    ``judge`` explains the content only.  Identity, counts and the request shape
    are filled here, so a model cannot invent a paper or a handle.
    """

    bundle, _result = prepare_local_reading(
        index, gap,
        max_passages=max_passages,
        top_papers=top_papers,
        passages_per_paper=passages_per_paper,
    )
    if judge is None or not bundle.passages:
        judgment = _policy_decision(gap, bundle)
    else:
        try:
            normalized = _normalize_judgment(judge(gap, bundle), gap)
        except PlanningMaterialTriageError:
            judgment = _policy_decision(gap, bundle)
            judgment.reason = "judge_unusable;" + judgment.reason
        else:
            judgment = TriageJudgment(
                gap=gap,
                decision=normalized["decision"],
                usable_content=normalized["usable_content"],
                still_missing=normalized["still_missing"],
                read_focus=normalized["read_focus"],
                external_ask=normalized["external_ask"],
                reason=normalized["reason"],
                source="judge",
                passages=bundle.passages,
                local_reading=bundle,
                model_calls=int(getattr(judge, "last_call_count", 1)),
                quantitative_comparisons=normalized.get("quantitative_comparisons", []),
                answers_requested_question=normalized.get("answers_requested_question"),
            )
            judgment.usable_content = _with_comparisons(judgment.usable_content, judgment.quantitative_comparisons)
            judgment = _apply_guardrails(judgment, bundle)
    if policy_override:
        forced = str(policy_override.get(gap.gap_id) or "").strip().casefold()
        if forced in DECISIONS:
            judgment.decision = forced
            judgment.source = judgment.source + "+policy_override"
    if not judgment.local_reading:
        judgment.local_reading = bundle
    return judgment


def _with_comparisons(content: str, comparisons: Sequence[Mapping[str, Any]]) -> str:
    lines = [content.strip()] if content.strip() else []
    def value(row: Mapping[str, Any], *keys: str) -> Any:
        return next((row.get(key) for key in keys if row.get(key) not in (None, "")), "")

    for row in comparisons:
        pairs = [f"{g.get('group')}: {g.get('value')}" for g in (row.get("groups") or [])
                 if isinstance(g, Mapping) and g.get("group") and g.get("value") is not None]
        if pairs:
            label = " / ".join(str(value(row, *keys)) for keys in (
                ("research_object", "population"),
                ("result_or_measure", "outcome"),
                ("setting_or_time", "timing"),
            ) if value(row, *keys))
            lines.append(label + "：" + "；".join(pairs) + ("；" + str(row["reported_statistics"]) if row.get("reported_statistics") else ""))
    return "\n".join(lines)


def read_local_capture(index: PlanningMaterialIndex, judgment: TriageJudgment, *, judge: Judge) -> TriageJudgment:
    """One diverse, bounded incremental read after an insufficient first read."""
    gap = judgment.gap
    # The original question owns the candidate slots. A judge's narrower or
    # broader focus can supplement them, never replace its top-12 anchors.
    result = search(index, gap.question, concepts=gap.concepts,
                    required_concepts=gap.required_concepts,
                    top_papers=LOCAL_EXPANSION_PAPERS, passages_per_paper=2,
                    bounded_channels=True, context_chars=0)
    candidate_ids = list(dict.fromkeys(hit.paper_id for hit in result.hits))
    hits = list(result.hits)
    if judgment.read_focus:
        focused = search(index, judgment.read_focus, concepts=gap.concepts,
                         required_concepts=gap.required_concepts,
                         top_papers=LOCAL_EXPANSION_PAPERS, passages_per_paper=2,
                         bounded_channels=True, context_chars=0)
        for hit in focused.hits:
            if hit.paper_id not in candidate_ids and len(candidate_ids) < LOCAL_EXPANSION_PAPERS:
                candidate_ids.append(hit.paper_id)
        # Also inspect the focus within the anchors even when the focus's own
        # global ranking would omit them. Bounded per-paper, no query rewrite.
        if candidate_ids:
            focused = search(index, judgment.read_focus, concepts=gap.concepts,
                             required_concepts=gap.required_concepts, paper_ids=candidate_ids,
                             top_papers=LOCAL_EXPANSION_PAPERS, passages_per_paper=2,
                             bounded_channels=True, context_chars=0)
            hits.extend(focused.hits)
    previous = judgment.local_reading or LocalReadingBundle(gap=gap)
    bundle = replace(previous, passages=list(previous.passages),
                     paper_contexts=list(previous.paper_contexts),
                     explicit_reading_status=dict(previous.explicit_reading_status),
                     new_task_fields=("focused_local_read",), searched_papers=len(candidate_ids))
    if not _append_bounded_hits(index, bundle, hits):
        return judgment
    bundle.required_concepts_all_missing = tuple(
        concept for concept in gap.required_concepts
        if not any(_concept_present(concept, p.text) for p in bundle.passages)
    )
    normalized = _normalize_judgment(judge(gap, bundle), gap)
    result = TriageJudgment(gap=gap, source="judge+focused_local_read", passages=bundle.passages,
                           local_reading=bundle, model_calls=judgment.model_calls+int(getattr(judge, "last_call_count", 1)), **normalized)
    result.usable_content = _with_comparisons(result.usable_content, result.quantitative_comparisons)
    # A later partial read must not erase useful content already obtained.
    old, new = judgment.usable_content.strip(), result.usable_content.strip()
    if old and old not in new:
        result.usable_content = old + ("\n\n" + new if new else "")
    return _apply_guardrails(result, bundle)


def _apply_guardrails(judgment: TriageJudgment, bundle: LocalReadingBundle) -> TriageJudgment:
    """Keep a model decision inside what the local material can actually support."""

    gap = judgment.gap
    if not bundle.passages:
        judgment.decision = "external_research"
        judgment.reason = "no_local_passage;" + judgment.reason
        if judgment.answers_requested_question is False and not judgment.still_missing.strip():
            judgment.still_missing = gap.question
        return judgment
    if judgment.decision == "direct_use":
        if not judgment.usable_content.strip():
            judgment.decision = "external_research"
            judgment.reason = "direct_use_without_stated_content;" + judgment.reason
        elif judgment.answers_requested_question is False:
            # Preserve useful partial context, but do not let it satisfy a
            # broader question whose requested object or success criteria are
            # still unmet.  The original question is the bounded external ask.
            judgment.decision = "external_research"
            judgment.still_missing = judgment.still_missing or gap.question
            judgment.reason = "useful_context_does_not_answer_question;" + judgment.reason
        elif gap.intended_use in SHALLOW_OK_USES and bundle.planning_summary:
            # Background use may rely on a clear summary statement.
            judgment.reason = judgment.reason or "background_use_allowed_from_summary"
    elif judgment.decision == "local_deep_read":
        if not bundle.direct_evidence:
            judgment.decision = "external_research"
            judgment.reason = "no_local_captured_text_to_read_deeper;" + judgment.reason
    if judgment.decision == "external_research" and not judgment.external_ask.strip():
        judgment.external_ask = (
            f"Local material does not cover: {gap.question} (intended use: {gap.intended_use})."
        )
    if judgment.answers_requested_question is False and not judgment.still_missing.strip():
        judgment.still_missing = gap.question
    return judgment


def triage_gaps(
    index: PlanningMaterialIndex,
    gaps: Sequence[LocalGap],
    *,
    judge: Judge | None = None,
    output_dir: str | Path | None = None,
    top_papers: int = 6,
    passages_per_paper: int = 2,
    max_passages: int = MAX_PASSAGES,
    policy_override: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Triage several gaps and write the stage artifacts."""

    judgments: list[TriageJudgment] = []
    for gap in gaps:
        judgments.append(triage_gap(
            index, gap,
            judge=judge,
            top_papers=top_papers,
            passages_per_paper=passages_per_paper,
            max_passages=max_passages,
            policy_override=policy_override,
        ))
    summary = {
        "schema_version": SCHEMA_VERSION,
        "gap_count": len(judgments),
        "decisions": {
            decision: sum(1 for item in judgments if item.decision == decision)
            for decision in DECISIONS
        },
        "external_requests": [
            item.external_request() for item in judgments if item.decision == "external_research"
        ],
        "model_calls": sum(item.model_calls for item in judgments),
        "downloads": sum((item.local_reading.downloads if item.local_reading else 0) for item in judgments),
        "whole_paper_rereads": sum(
            (item.local_reading.whole_paper_rereads if item.local_reading else 0) for item in judgments
        ),
        "judgments": [item.to_dict() for item in judgments],
    }
    if output_dir is not None:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / "TRIAGE_SUMMARY.json", summary)
        for item in judgments:
            _write_json(root / (item.gap.gap_id + ".judgment.json"), item.to_dict())
        if summary["external_requests"]:
            _write_json(root / "EXTERNAL_REQUESTS.json", {"requests": summary["external_requests"]})
        blocked = {
            "schema_version": SCHEMA_VERSION,
            "reason": "local_material_insufficient",
            "blocked_gap_ids": [item.gap.gap_id for item in judgments if item.decision == "external_research"],
        }
        if blocked["blocked_gap_ids"]:
            _write_json(root / "EXTERNAL_REQUEST.json", blocked)
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------
# Writer-ready material
# --------------------------------------------------------------------------

WRITER_MATERIAL_SCHEMA = "optomind.planning_writer_material.v1"

#: Fields the writer may rely on, mapped from what the captured material holds.
WRITER_ALLOWED_FIELDS = (
    "background_context", "explicit_objectives", "explicit_method_summaries",
    "explicit_reported_findings", "explicit_leads",
)


def build_writer_material(
    judgment: TriageJudgment,
    *,
    chapter_id: str = "",
    unit_key: str = "",
) -> dict[str, Any]:
    """Turn one triage judgment into material a chapter writer can actually use.

    The writer gets the concrete statement, the conditions it holds under, the
    gaps it does not cover, and the program-managed paper identity for each
    source.  The chapter receives the useful content together with the remaining question.
    """

    # Conditions describe the study, not the file format or section heading.
    deduped_conditions = list(dict.fromkeys(
        str(value).strip()
        for row in judgment.quantitative_comparisons
        for value in (
            row.get("research_object") or row.get("population"),
            row.get("setting_or_time") or row.get("timing"),
        )
        if value and str(value).strip()
    ))
    deduped_limits = [judgment.still_missing] if judgment.still_missing else []

    sources = [
        {
            "paper_id": passage.paper_id,
            "source_handle": passage.source_handle,
            "title": passage.title,
            "year": passage.year,
            "doi": passage.doi,
            "reading_role": passage.reading_role,
            "selection_reason": passage.selection_reason,
            "section_path": list(passage.section_path),
            "material_depth": passage.material_depth,
            "reading_path": passage.reading_path,
            "card_path": passage.card_path,
        }
        for passage in judgment.passages
    ]
    result = {
        "schema_version": WRITER_MATERIAL_SCHEMA,
        "chapter_id": chapter_id,
        "unit_key": unit_key,
        "need_id": judgment.gap.gap_id,
        "question": judgment.gap.question,
        "intended_use": judgment.gap.intended_use,
        "decision": judgment.decision,
        "usable_content": judgment.usable_content,
        "conditions": deduped_conditions,
        "limits": deduped_limits,
        "still_missing": judgment.still_missing,
        "read_focus": judgment.read_focus,
        "external_ask": judgment.external_ask,
        "allowed_use": list(WRITER_ALLOWED_FIELDS),
        "sources": sources,
        "provenance": {
            "downloads": judgment.local_reading.downloads if judgment.local_reading else 0,
            "whole_paper_rereads": judgment.local_reading.whole_paper_rereads if judgment.local_reading else 0,
            "model_calls": judgment.model_calls,
            "source": judgment.source,
        },
    }
    if judgment.answers_requested_question is not None:
        result["answers_requested_question"] = bool(judgment.answers_requested_question)
    return result


# --------------------------------------------------------------------------
# Adapters for the existing planning chain
# --------------------------------------------------------------------------


def local_gaps_from_requests(
    gaps: Sequence[Mapping[str, Any]],
    *,
    intended_use: str = "mechanism",
    user_scope: str = "",
) -> list[LocalGap]:
    """Build triage inputs from ``_normalize_gaps`` output in the planner."""

    out: list[LocalGap] = []
    for index, raw in enumerate(gaps, start=1):
        gap_id = str(raw.get("gap_id") or f"gap_{index:02d}")
        question = str(raw.get("gap_question") or raw.get("question") or "").strip()
        if not question:
            continue
        concepts: list[str] = []
        for query in raw.get("targeted_queries") or ():
            if isinstance(query, Mapping):
                text = str(query.get("query_text") or "").strip()
                if text:
                    concepts.append(text)
            elif str(query).strip():
                concepts.append(str(query).strip())
        raw_criteria = raw.get("success_criteria") or []
        if isinstance(raw_criteria, str):
            raw_criteria = [raw_criteria]
        out.append(LocalGap(
            gap_id=gap_id,
            question=question,
            intended_use=str(raw.get("intended_use") or intended_use),
            user_scope=str(raw.get("user_scope") or user_scope),
            success_criteria=tuple(str(item) for item in raw_criteria if str(item).strip()),
            chapter_ids=tuple(str(item) for item in (raw.get("chapter_ids") or ()) if str(item).strip()),
            concepts=tuple(concepts[:6]),
            required_concepts=tuple(str(item) for item in (raw.get("required_concepts") or ()) if str(item).strip()),
            existing_handles=tuple(str(item) for item in (raw.get("known_paper_handles") or ()) if str(item).strip()),
        ))
    return out


__all__ = [
    "DECISIONS",
    "INTENDED_USE_ALIASES",
    "MAX_PASSAGES",
    "PASSAGE_CHARS",
    "SCHEMA_VERSION",
    "SHALLOW_OK_USES",
    "SPECIFIC_USES",
    "USES",
    "WRITER_ALLOWED_FIELDS",
    "WRITER_MATERIAL_SCHEMA",
    "LocalGap",
    "LocalReadingBundle",
    "LocalReadingPassage",
    "PlanningMaterialTriageError",
    "QwenLocalTriageJudge",
    "TriageJudgment",
    "QUANTITATIVE_USES",
    "build_writer_material",
    "local_gaps_from_requests",
    "normalize_intended_use",
    "prepare_local_reading",
    "triage_gap",
    "triage_gaps",
]
