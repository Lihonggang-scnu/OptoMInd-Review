"""Offline checks of the real planning prompts, without importing live adapters."""

import ast
import json
from pathlib import Path
from typing import Any, Mapping

import pytest


SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "optomind_research/runtime/upgrade3/progressive_review_plan.py"
)
OPENING = (
    "你是学术综述规划编辑。目标是形成有论证主线的长篇综述，规划内容用中文撰写，"
    "论文原题和专名可保留原文；不要写成问答清单。"
)
BODY_BOUNDARY = (
    "本规划链只规划综述正文中需要实质展开的知识与分析。全篇开场、向读者宣告综述范围与文章组织、摘要，"
    "以及收束全文的最终总结，由正文完成后的独立模块负责，不作为本链路的章节或单元任务；"
    "也不要将这些职责换名为“背景”“概述”等章节继续规划。理解后续分析所必需的背景、概念、理论教学、机制、方法比较，"
    "以及具有具体问题与分析的未来研究方向，仍属于正文，应按解释需要充分展开。每章应有明确的知识问题或分析任务，"
    "不能仅以引入全文或总结全文为目的。本链路仍须确定研究范围、组织视角和章节分工，"
    "但这些规划信息不等于需要写成一个入口章节。上述职责归属适用于本链路所有阶段。"
)
OLD_INTRODUCTION_DUTY = (
    "An introduction establishes context, the review's problem and organizing perspective; "
    "it must not become a miniature full review."
)
STAGES = (
    "provisional_scope",
    "level1_outline",
    "source_routing",
    "chapter_proposals",
    "harmonize_scope",
    "finalize_chapter_scope",
    "chapter_details",
    "chapter_need_analysis",
    "whole_plan_improvement",
    "affected_chapter_revision",
    "case_groups",
)


@pytest.fixture(scope="module")
def planner_functions():
    """Execute production function bodies only; no model/client/network imports."""
    names = {
        "_json_default", "_text", "_planner_instructions", "_messages_for",
        "_outline_chapter_rows", "_compact_routing_outline", "_chapter_units",
    }
    source = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    functions = [
        node for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    namespace = {"Any": Any, "Mapping": Mapping, "Path": Path, "json": json}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE_PATH), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("planning_revision", [False, True])
@pytest.mark.parametrize("stage", STAGES)
def test_body_boundary_reaches_real_messages_at_every_stage(
    planner_functions, stage, planning_revision,
):
    payload = {"planning_revision_mode": planning_revision, "question": "材料怎样解释机制？"}
    messages = planner_functions["_messages_for"](stage, payload)
    assert [message["role"] for message in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert system.startswith(OPENING + "\n\n" + BODY_BOUNDARY + "\n\n")
    assert system.count(BODY_BOUNDARY) == 1
    assert OLD_INTRODUCTION_DUTY not in system
    assert "理论、概念、方法论和设计研究也应按其实际论证结构组织。" in system
    assert "解释、比较、例证、背景或发展等职责" in system
    assert "Return JSON only." in system
    assert ("【章节论证模式】" in system) is planning_revision
    assert "当前任务：" + stage in messages[1]["content"]


@pytest.mark.parametrize("planning_revision", [False, True])
def test_chapter_details_preserves_substantive_background_and_assignment(
    planner_functions, planning_revision,
):
    system = planner_functions["_messages_for"](
        "chapter_details", {"planning_revision_mode": planning_revision},
    )[0]["content"]
    assert (
        "topics owned by other chapters instead of developing them again. "
        "Treat the outline as an editorial assignment, not a factual source:"
    ) in system
    assert "enough selected cases, contrasts and background sources" in system
    assert "ordered substantive units" in system
    assert "paragraph_briefs: an ordered list" in system


@pytest.mark.parametrize("title", ["Introduction", "背景", "概述"])
@pytest.mark.parametrize("planning_revision", [False, True])
def test_substantive_chapter_is_not_filtered_by_title(
    planner_functions, title, planning_revision,
):
    chapter = {
        "chapter_id": "C1", "title": title,
        "purpose": "解释后续机制分析必需的概念与理论，并比较方法适用条件。",
    }
    unit = {
        "unit_id": "C1_U1", "title": title,
        "substantive_point": "研究对象与设置如何决定理论假设的适用性。",
    }
    outline = {"chapters": [chapter]}
    plan = {"units": [unit]}
    assert planner_functions["_outline_chapter_rows"](outline) == [chapter]
    assert planner_functions["_compact_routing_outline"](outline) == [
        {"chapter_id": "C1", "title": title, "focus": chapter["purpose"]},
    ]
    assert planner_functions["_chapter_units"](plan) == [unit]
    payload = {
        "planning_revision_mode": planning_revision,
        "chapter": chapter, "shared_outline": outline, "chapter_plan": plan,
    }
    messages = planner_functions["_messages_for"]("chapter_details", payload)
    encoded_payload = messages[1]["content"].split("\n", 1)[1].split("\n\n【本轮交付】", 1)[0]
    assert json.loads(encoded_payload) == payload
