"""Single source of truth for the canonical harness stages (F1).

The harness orchestrator owns the stage list
(`ReviewHarnessOrchestrator.STAGES`, read-only here -- never mutate or
copy it); this module is the ONLY place that attaches human-facing
labels, one-line explanations and progress-track groups to those keys.

`query_planner` runs before STAGES and is part of the canonical set,
bringing the total to 27 stages (the chapter-scoped style pass is the latest
publication-stage addition).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessOrchestrator,
)

# Read-only alias: the harness owns this tuple; we only compare against it.
canonical_stage_keys: Tuple[str, ...] = ReviewHarnessOrchestrator.STAGES

_RECORDS: Tuple[Dict[str, str], ...] = (
    {"key": "query_planner", "label": "理解问题", "explain": "把研究问题整理成可执行的检索计划", "group": "准备"},
    {"key": "topic_scoped_kb", "label": "圈定材料", "explain": "从中央材料库选出主题相关存量材料", "group": "准备"},
    {"key": "s2_literature_intelligence", "label": "检索文献", "explain": "联网检索并解析论文全文摘要证据", "group": "文献"},
    {"key": "review_lead", "label": "设计结构", "explain": "规划综述的章节骨架与论证路线", "group": "结构"},
    {"key": "section_coverage", "label": "整理证据", "explain": "按章节职责归组证据", "group": "结构"},
    {"key": "section_coverage_portfolio", "label": "证据组合", "explain": "为每章挑选最优证据组合", "group": "结构"},
    {"key": "phase3_argument_orchestration", "label": "形成论点", "explain": "把证据组织成可写的论点链", "group": "论证"},
    {"key": "authoring_revision", "label": "章节初稿", "explain": "逐章写作并保留最后有效稿", "group": "论证"},
    {"key": "section_coverage_feedback", "label": "补充证据", "explain": "按审稿反馈补齐缺口证据", "group": "论证"},
    {"key": "section_supplementary_closure", "label": "缺口收口", "explain": "对未满足的证据缺口执行补充检索闭环", "group": "论证"},
    {"key": "quality_review_gate", "label": "质量把关", "explain": "质量关注项的人工确认或显式接受", "group": "交付"},
    {"key": "llm_style_pipeline", "label": "风格治理", "explain": "改写重复句式与抽象主语段落", "group": "出版"},
    {"key": "chapter_style_governance", "label": "段落审修", "explain": "逐章治理段首与简称重复", "group": "出版"},
    {"key": "publication_mainline_enhancement", "label": "章节增强", "explain": "补充解释应用案例可读性结构", "group": "出版"},
    {"key": "publication_mainline_handoff", "label": "交接材料", "explain": "汇总全文写作所需材料并交接", "group": "出版"},
    {"key": "publication_mainline_commander", "label": "全文编排", "explain": "统一章节关系与全文叙事", "group": "出版"},
    {"key": "publication_mainline_staged_completion", "label": "补齐前后文", "explain": "生成标题结论引言与摘要", "group": "出版"},
    {"key": "article_completion", "label": "论文收尾", "explain": "补齐论文结构与出版元数据", "group": "出版"},
    {"key": "article_structure_audit", "label": "结构体检", "explain": "检查论文结构完整性", "group": "出版"},
    {"key": "visual_editor", "label": "视觉规划", "explain": "规划图表图注与挂载位置", "group": "视觉"},
    {"key": "visual_materialization", "label": "图表挂载", "explain": "生成或挂载经审核的视觉材料", "group": "视觉"},
    {"key": "research_plan", "label": "研究计划", "explain": "整理后续研究方向", "group": "交付"},
    {"key": "packaging", "label": "打包交接", "explain": "整理引用元数据与交接包", "group": "交付"},
    {"key": "latex_publication", "label": "编译PDF", "explain": "编译并检查最终英文 PDF", "group": "交付"},
    {"key": "chinese_translation", "label": "中文翻译", "explain": "生成中文版本内容", "group": "交付"},
    {"key": "latex_publication_zh", "label": "中文PDF", "explain": "编译中文版 PDF", "group": "交付"},
    {"key": "research_plan_publication", "label": "计划附件", "explain": "整理研究计划附件 PDF", "group": "交付"},
)

_GROUP_ORDER: Tuple[str, ...] = ("准备", "文献", "结构", "论证", "出版", "视觉", "交付")

_STATUS_LABELS: Dict[str, str] = {
    "starting": "准备启动",
    "running": "正在研究",
    "completed": "已完成",
    "degraded": "降级完成",
    "waiting_for_human": "等待人工确认",
    "awaiting_human_review": "等待你的确认",
    "needs_model_recovery": "需要重新整理问题",
    "budget_exhausted": "预算已用完",
    "budget_rejected": "尚未开始",
    "failed": "运行失败",
    "partial": "部分完成",
    # The stage is declared and has a row here, but no code path runs it yet.
    # Distinct from "尚未开始", which means this run has not reached it.
    "not_integrated": "尚未接入",
    "skipped_cost_budget": "预算不足已跳过",
    "skipped_no_visual_plan": "无配图计划",
    "not_required": "无需裁决",
    "blocked_hard_quality": "质量硬阻断",
    "unknown": "历史任务",
}

_BY_KEY: Dict[str, Dict[str, str]] = {rec["key"]: rec for rec in _RECORDS}

# ---- import-time guards: a wrong registry must fail loudly, at once. ----
assert len(_RECORDS) == 27, f"expected 27 registry rows, got {len(_RECORDS)}"
assert len(_BY_KEY) == len(_RECORDS), "duplicate stage key in registry"
assert set(_BY_KEY) == {"query_planner"} | set(canonical_stage_keys), (
    'registry keys diverge from canonical {"query_planner"} | STAGES'
)
assert all(
    rec["group"] in _GROUP_ORDER and 0 < len(rec["label"]) <= 6 and len(rec["explain"]) <= 30
    for rec in _RECORDS
), "record violates group membership / label<=6 / explain<=30"
_grouped = [rec["key"] for rec in _RECORDS if rec["group"] in _GROUP_ORDER]
assert len(_grouped) == 27 and len(set(_grouped)) == 27, (
    "every stage key must appear in exactly one group"
)


def all_stages() -> Tuple[Dict[str, str], ...]:
    """Ordered 27-stage records: {key,label,explain,group}."""

    return _RECORDS


def stage_label(key: str) -> str:
    """Chinese short label; unknown keys fall back to the raw key (no raise)."""

    record = _BY_KEY.get(str(key or ""))
    return record["label"] if record else str(key or "")


def stage_explain(key: str) -> str:
    """One-line human explanation; empty string for unknown keys (no raise)."""

    record = _BY_KEY.get(str(key or ""))
    return record["explain"] if record else ""


def groups() -> List[Tuple[str, List[str]]]:
    """Ordered [(group_name, [stage keys])] covering all stage keys exactly once."""

    result: List[Tuple[str, List[str]]] = []
    for group in _GROUP_ORDER:
        result.append((group, [rec["key"] for rec in _RECORDS if rec["group"] == group]))
    return result


def status_label(code: str) -> str:
    """Human label for run/status codes; unknown codes return the code itself."""

    code = str(code or "")
    return _STATUS_LABELS.get(code, code)
