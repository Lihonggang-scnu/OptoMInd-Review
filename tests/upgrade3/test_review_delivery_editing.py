"""Work orders review_v2_delivery/02+03: full-text local editing and
front/back parts.

The coordination proposals and front/back parts below are LABELED MANUAL
FIXTURES anchored to real sentences of the September-27 handle draft; they
exercise the real message construction, parser and applier.  They are not
model output and prove the application contract, not model quality.
"""

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import article_text_editor as editor
from optomind_research.runtime.upgrade3 import manuscript_front_back as front_back
from optomind_research.runtime.upgrade3.review_delivery import _write_json

REAL_DRAFT = Path("outputs/full_review_draft/20260927_run01/REVIEW_DRAFT_HANDLES.md")
CHAPTER_ROLES = [
    {"chapter_id": "CH01", "title": "引言与评估框架", "role": "背景与框架"},
    {"chapter_id": "CH02", "title": "菌群关联与生物标志物", "role": "临床关联主讲"},
    {"chapter_id": "CH03", "title": "机制网络", "role": "机制主讲"},
    {"chapter_id": "CH04", "title": "外部调节因素", "role": "调节因素"},
    {"chapter_id": "CH05", "title": "干预研究", "role": "干预主讲"},
    {"chapter_id": "CH06", "title": "可重复性与转化边界", "role": "限制与转化"},
]


def _anchor(text: str, probe: str, take: int = 200) -> str:
    start = text.find(probe)
    assert start >= 0, f"anchor probe missing in real draft: {probe}"
    end = text.find("。", start)
    return text[start:end + 1]


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 02: full-text local editing
# ---------------------------------------------------------------------------


def _real_edit_proposals(draft_text: str) -> dict:
    duplicate_sentence = _anchor(
        draft_text,
        "FMT-LUMINate II 期试验显示，在 NSCLC 一线治疗中")
    terminology_old = "FMT-LUMINate II期试验提供了"
    transition_old = _anchor(
        draft_text, "也凸显了当前领域缺乏大规模III期随机对照试验")
    return {
        "fixture": True,
        "note": "人工标注fixture：编辑建议锚定真实长稿文本，非模型输出",
        "changes": [
            {
                "target_block_id": "CH06",
                "operation": "replace",
                "original_text": duplicate_sentence,
                "replacement_text": (
                    "FMT-LUMINate 试验的客观缓解率数据已在 5.1 节完整报告，此处不再重复数值；"
                    "本节仅沿用它说明响应者以基线有害菌清除为主的特征。"),
                "reason": "同一试验同一结果（80% ORR）在 5.1 节已完整讲述，此处为相同作用的重复，压缩为回指",
            },
            {
                "target_block_id": "CH05",
                "operation": "replace",
                "original_text": terminology_old,
                "replacement_text": "FMT-LUMINate II 期试验提供了",
                "reason": "书写归一：全文其余 22 处均为“II 期”（带空格），专名 FMT-LUMINate 不变",
            },
            {
                "target_block_id": "CH05",
                "operation": "replace",
                "original_text": transition_old,
                "replacement_text": transition_old
                + "上述疗效信号能否转化为实际获益，还取决于 5.2 节集中讨论的供体来源与毒性因素。",
                "reason": "相邻章节衔接：5.1 的疗效信号与 5.2 的毒性/供体讨论是同一线索的两面，衔接写具体",
            },
        ],
        "unresolved_questions": [
            {"where": "CH02 与 CH06 的生物标志物一致性论述",
             "question": "两处结论的适用条件表述存在差异，需负责人决定是否统一"},
        ],
    }


def test_text_edit_stage_applies_fixture_to_real_draft(tmp_path):
    assert REAL_DRAFT.is_file(), "the real 9/27 handle draft must stay available (read-only)"
    draft_text = REAL_DRAFT.read_text(encoding="utf-8")
    fixture = _real_edit_proposals(draft_text)
    fixture_path = tmp_path / "EDITS_FIXTURE.json"
    _write_json(fixture_path, fixture)
    work = tmp_path / "draft.md"
    work.write_text(draft_text, encoding="utf-8")

    report = editor.run_text_edit_stage(
        draft_path=work, chapter_roles=CHAPTER_ROLES, out_dir=tmp_path / "stage",
        proposals_fixture_path=fixture_path)

    assert report["model_calls"] == 0 and report["external_requests"] == 0
    assert report["status"] == "edited"
    assert len(report["applied"]) == 3 and report["skipped"] == []
    assert report["unresolved_questions"], "unresolved questions travel in the report"

    # The real coordination messages were built and carry the FULL draft text
    # (mid-draft marker present, no silent truncation).
    saved = json.loads((tmp_path / "stage/messages/text_edit_full_messages.json")
                       .read_text(encoding="utf-8"))
    user = saved[1]["content"]
    assert len(user) > 60_000
    assert "单一物种标志物的跨队列可重复性有限" in user, \
        "mid-draft content is present verbatim"

    edited = (tmp_path / "stage/draft_EDITED.md").read_text(encoding="utf-8")
    # 同案例相同作用重复被压缩为回指
    assert "已在 5.1 节完整报告" in edited
    assert fixture["changes"][0]["original_text"] not in edited
    # 同案例不同作用保留：6.3 的毒性语境与 5.1 的主讲都还在
    assert "这种风险谱系表现出强烈的情境依赖性" in edited
    assert "FMT-LUMINate II 期试验提供了目前最详尽的双癌种数据" in edited
    # 术语书写归一且专名不变
    assert "FMT-LUMINate II 期试验提供了" in edited
    assert "FMT-LUMINate II期试验" not in edited
    assert "FMT-LUMINate" in edited and "*Enterocloster*" in edited
    # 相邻章节具体衔接
    assert "还取决于 5.2 节集中讨论的供体来源与毒性因素" in edited
    # 正负结果并存未被删除
    assert "并非普遍适用的“耐药逆转剂”" in edited
    assert "未观察到严重 irAEs 增加" in edited
    # 原稿保留
    assert work.read_text(encoding="utf-8") == draft_text


def test_text_edit_second_run_is_no_change(tmp_path):
    draft_text = REAL_DRAFT.read_text(encoding="utf-8")
    fixture_path = tmp_path / "EDITS_FIXTURE.json"
    _write_json(fixture_path, _real_edit_proposals(draft_text))
    work = tmp_path / "draft.md"
    work.write_text(draft_text, encoding="utf-8")
    editor.run_text_edit_stage(draft_path=work, chapter_roles=CHAPTER_ROLES,
                               out_dir=tmp_path / "stage1",
                               proposals_fixture_path=fixture_path)
    # Second run on the EDITED text: every target is gone -> skipped, no edit.
    report = editor.run_text_edit_stage(
        draft_path=tmp_path / "stage1/draft_EDITED.md", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "stage2", proposals_fixture_path=fixture_path)
    assert report["status"] == "no_change"
    assert report["applied"] == [] and len(report["skipped"]) == 3
    assert all(item["reason"] in {"already_applied", "target_occurs_0_times"} for item in report["skipped"])
    assert (tmp_path / "stage2/draft_EDITED_EDITED.md").read_text(encoding="utf-8") == \
        (tmp_path / "stage1/draft_EDITED.md").read_text(encoding="utf-8")


def test_text_edit_stale_and_ambiguous_targets_are_skipped(tmp_path):
    text = "甲段内容。\n\n乙段内容。\n\n甲段内容。"
    changes = [
        {"operation": "replace", "original_text": "不存在的目标。",
         "replacement_text": "x", "reason": "r"},
        {"operation": "replace", "original_text": "甲段内容。",
         "replacement_text": "x", "reason": "r"},
    ]
    new_text, applied, skipped = editor.apply_text_edits(text, changes)
    assert applied == [] and len(skipped) == 2
    assert new_text == text


def test_text_edit_parser_contracts():
    with pytest.raises(editor.TextEditError):
        editor.parse_edit_proposals({"changes": [{"operation": "replace",
                                                  "original_text": "x"}]})
    with pytest.raises(editor.TextEditError):
        editor.parse_edit_proposals({"changes": [{"operation": "move",
                                                  "original_text": "x",
                                                  "replacement_text": "y"}]})
    no_change = editor.parse_edit_proposals({"changes": [], "unresolved_questions": []})
    assert no_change["no_change"] is True


def test_text_edit_missing_recording_reports_pending(tmp_path):
    work = tmp_path / "draft.md"
    work.write_text("只有一段很普通的正文，没有任何需要修改之处。", encoding="utf-8")
    report = editor.run_text_edit_stage(draft_path=work, chapter_roles=CHAPTER_ROLES,
                                        out_dir=tmp_path / "stage", recordings={})
    assert report["status"] == "pending"
    assert report["pending"] == [{"step": "text_edit:full",
                                  "status": "pending_missing_recording"}]
    assert (tmp_path / "stage/messages/text_edit_full_messages.json").is_file()


def test_editor_prompt_is_generic_and_exposes_contract():
    prompt = editor.load_editor_prompt()
    for phrase in ("同案例相同作用", "不同作用", "书写", "衔接", "反面结果", "no_change",
                   "恰好出现一次"):
        assert phrase in prompt, phrase
    for domain_word in ("FMT", "LUMINate", "LLZO", "电解质", "菌群", "ICI", "电池"):
        assert domain_word not in prompt, domain_word


# ---------------------------------------------------------------------------
# 03: front/back parts and final assembly
# ---------------------------------------------------------------------------


def _front_back_fixture() -> dict:
    return {
        "fixture": True,
        "note": "人工标注fixture：首尾部件演示应用合同，非模型输出",
        "conclusion": {
            "conclusion": (
                "本综述按临床关联、机制、外部调节与干预四条线整理了肠道微生物组与"
                "免疫检查点治疗的现有证据：关联层面跨癌种可重复性仍有限，机制层面有"
                "临床前支持但人体因果链未闭合，干预层面早期信号积极而 randomized 证据不足。"
                "这些边界决定了当前结论只能支持谨慎的研究分层，而非临床常规检测。")
        },
        "introduction": {
            "introduction": (
                "免疫检查点抑制剂改变了多种实体瘤的治疗格局，但响应差异显著。"
                "本综述只覆盖胃肠道菌群与 ICI 响应关联这一范围：先建立临床关联与评估框架，"
                "再进入机制与外部调节因素，最后讨论干预研究与转化边界；"
                "各章分工的理由与正文实际展开一致，不在此处重复。")
        },
        "abstract": {
            "title": "肠道微生物组与实体瘤免疫检查点治疗：关联、机制与干预证据的综述",
            "abstract": (
                "本综述覆盖肠道菌群与免疫检查点抑制剂疗效关联的临床证据、机制解释、"
                "外部调节因素与干预研究。主要综合判断：跨癌种关联可重复性有限，"
                "机制解释以临床前证据为主，干预的随机对照证据仍不足；"
                "关键限制包括队列异质性与方法学差异。"),
            "keywords": ["肠道微生物组", "免疫检查点抑制剂", "粪便微生物移植", "生物标志物"],
        },
    }


def test_front_back_updates_existing_parts_and_inserts_missing(tmp_path):
    assert REAL_DRAFT.is_file()
    work = tmp_path / "manuscript.md"
    work.write_text(REAL_DRAFT.read_text(encoding="utf-8"), encoding="utf-8")
    assert "## 摘要" in work.read_text(encoding="utf-8")
    fixture_path = tmp_path / "PARTS_FIXTURE.json"
    _write_json(fixture_path, _front_back_fixture())

    report = front_back.run_front_back_stage(
        draft_path=work, research_question="肠道微生物组与 ICI 疗效",
        chapter_roles=CHAPTER_ROLES, out_dir=tmp_path / "stage",
        parts_fixture_path=fixture_path)

    assert report["status"] == "generated"
    assert sorted(report["generated"]) == ["abstract", "conclusion", "introduction"]
    final = (tmp_path / "stage/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    # Single main positions.  The real draft's first body chapter IS the
    # introduction chapter ("## 第1章 引言：…"), so the generated intro updates
    # THAT chapter's opening and no second "## 引言" section may appear.
    assert final.count("## 摘要") == 1
    assert final.count("## 引言") == 0, "no standalone 引言 section next to the intro chapter"
    assert final.count("## 结语") + final.count("## 结论") == 1
    assert final.count("第1章 引言") == 1, "the body introduction chapter stays exactly once"
    intro_chapter = final.index("第1章 引言")
    next_chapter = final.index("第2章")
    assert "本综述只覆盖胃肠道菌群与 ICI 响应关联这一范围" in final[intro_chapter:next_chapter],         "the generated introduction opens the body introduction chapter"
    # Updated parts appear; the old abstract body is replaced, not duplicated,
    # and the abstract body stays attached to its own heading.
    assert "关键限制包括队列异质性与方法学差异" in final
    assert "randomized 证据不足" in final
    abstract_head = final.index("## 摘要")
    first_chapter_head = final.index("## 第1章")
    assert "关键限制包括队列异质性" in final[abstract_head:first_chapter_head],         "the abstract body must sit under the 摘要 heading"
    # Keywords land in the manuscript attached to the abstract.
    assert "**关键词：**" in final[abstract_head:first_chapter_head]
    assert "粪便微生物移植" in final[abstract_head:first_chapter_head]
    # Conclusion sits before the references section.
    conclusion_head = final.index("## 结语")
    references_head = final.index("## 参考文献")
    assert conclusion_head < references_head
    # Body chapters and 展望-style content survive untouched.
    assert "单一物种标志物的跨队列可重复性有限" in final
    assert "粪便微生物移植（FMT）联合免疫检查点抑制剂" in final
    # Real messages on disk.
    assert (tmp_path / "stage/messages/front_back_conclusion_messages.json").is_file()


def test_front_back_inserts_all_parts_into_plan_draft(tmp_path):
    plan_draft = Path("outputs/review_v2_delivery/plan/assembled/REVIEW_DRAFT.md")
    assert plan_draft.is_file(), "the real plan-assembled draft must stay available"
    work = tmp_path / "manuscript.md"
    work.write_text(plan_draft.read_text(encoding="utf-8"), encoding="utf-8")
    body = work.read_text(encoding="utf-8")
    assert "## 摘要" not in body and "## 引言" not in body, \
        "the plan-assembled draft has no front/back yet (insert path)"
    fixture_path = tmp_path / "PARTS_FIXTURE_C6.json"
    fixture = {
        "fixture": True,
        "conclusion": {"conclusion": "两条路线各有约束，复合策略的收益依赖界面与工艺条件；本轮证据不支持单一最优路线。"},
        "introduction": {"introduction": "全固态电池的电解质选择是本综述的唯一范围；先比较两条路线，再评估复合策略。"},
        "abstract": {"title": "硫化物与氧化物固态电解质对比",
                     "abstract": "比较硫化物与氧化物固态电解质在电导率、稳定性与可制造性上的证据。",
                     "keywords": ["固态电解质", "复合策略"]},
    }
    _write_json(fixture_path, fixture)
    report = front_back.run_front_back_stage(
        draft_path=work, research_question="硫化物与氧化物固态电解质对比",
        chapter_roles=[{"chapter_id": "C6", "title": "复合与混合策略", "role": "复合策略主讲"}],
        out_dir=tmp_path / "stage", parts_fixture_path=fixture_path)
    assert report["status"] == "generated"
    final = (tmp_path / "stage/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert final.count("## 摘要") == 1 and final.count("## 引言") == 1
    assert "本轮证据不支持单一最优路线" in final
    assert final.startswith("# 硫化物与氧化物固态电解质对比") or "## 摘要" in final[:400]
    if "## 参考文献" in final:
        assert final.index("## 结语") < final.index("## 参考文献"),             "a newly added conclusion goes before the references"


def test_front_back_empty_and_placeholder_parts_are_rejected(tmp_path):
    work = tmp_path / "manuscript.md"
    work.write_text("# 标题\n\n## 摘要\n\n旧摘要。", encoding="utf-8")
    fixture_path = tmp_path / "PARTS_BAD.json"
    _write_json(fixture_path, {
        "fixture": True,
        "abstract": {"title": "t", "abstract": "", "keywords": []},
        "conclusion": {"conclusion": "offline_placeholder"},
    })
    report = front_back.run_front_back_stage(
        draft_path=work, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "stage", parts_fixture_path=fixture_path)
    assert report["status"] == "no_parts_generated",         "no usable part at all must never read as generated"
    stages_failed = {item["stage"] for item in report["failures"]}
    assert stages_failed == {"abstract", "conclusion", "introduction"}
    final = (tmp_path / "stage/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert "旧摘要。" in final, "with no usable parts the manuscript stays as imported"
    assert "offline_placeholder" not in final


def test_front_back_body_change_regenerates_parts(tmp_path):
    work = tmp_path / "manuscript.md"
    work.write_text("# 标题\n\n## 摘要\n\n旧摘要说 A。\n\n## 第1章\n\n正文说 A。", encoding="utf-8")
    fixture_v1 = tmp_path / "PARTS_V1.json"
    _write_json(fixture_v1, {
        "fixture": True,
        "abstract": {"title": "t", "abstract": "摘要 v1：正文说 A。", "keywords": ["k"]},
    })
    front_back.run_front_back_stage(draft_path=work, research_question="q",
                                    chapter_roles=CHAPTER_ROLES,
                                    out_dir=tmp_path / "stage1",
                                    parts_fixture_path=fixture_v1)
    # The body understanding changed; a v2 fixture (labeled) refreshes the part.
    work.write_text("# 标题\n\n## 摘要\n\n旧摘要说 A。\n\n## 第1章\n\n正文已改为说 B。", encoding="utf-8")
    fixture_v2 = tmp_path / "PARTS_V2.json"
    _write_json(fixture_v2, {
        "fixture": True,
        "abstract": {"title": "t", "abstract": "摘要 v2：正文已改为说 B。", "keywords": ["k"]},
    })
    report = front_back.run_front_back_stage(draft_path=work, research_question="q",
                                             chapter_roles=CHAPTER_ROLES,
                                             out_dir=tmp_path / "stage2",
                                             parts_fixture_path=fixture_v2)
    assert report["status"] == "partial"  # only the abstract part was provided
    assert report["generated"] == ["abstract"]
    final = (tmp_path / "stage2/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert "摘要 v2：正文已改为说 B。" in final
    assert "摘要 v1" not in final


def test_front_back_missing_recording_reports_failures(tmp_path):
    work = tmp_path / "manuscript.md"
    work.write_text("# 标题\n\n正文。", encoding="utf-8")
    report = front_back.run_front_back_stage(draft_path=work, research_question="q",
                                             chapter_roles=CHAPTER_ROLES,
                                             out_dir=tmp_path / "stage", recordings={})
    assert report["status"] == "no_parts_generated"
    assert {item["stage"] for item in report["failures"]} == set(front_back.STAGE_ORDER)
    assert all(item["error"] == "pending_missing_recording" for item in report["failures"])


# ---------------------------------------------------------------------------
# rework: Astra acceptance points (serial dependency, target-local edits,
# oversized drafts)
# ---------------------------------------------------------------------------


def test_edit_elsewhere_phrase_does_not_block_target(tmp_path):
    # Astra's exact scenario: "II 期" already exists elsewhere in the text;
    # the target's own "II期" must still be edited, not judged already-applied.
    work = tmp_path / "draft.md"
    work.write_text(
        "别处写的是 II 期试验，与本段无关。\n\n"
        "目标段：FMT-LUMINate II期试验提供了数据。", encoding="utf-8")
    changes = [{"operation": "replace",
                "original_text": "FMT-LUMINate II期试验提供了",
                "replacement_text": "FMT-LUMINate II 期试验提供了",
                "reason": "书写归一"}]
    new_text, applied, skipped = editor.apply_text_edits(
        work.read_text(encoding="utf-8"), changes)
    assert len(applied) == 1 and skipped == []
    assert "FMT-LUMINate II 期试验提供了数据" in new_text
    # And re-running the applied edit is still idempotent.
    _, applied2, skipped2 = editor.apply_text_edits(new_text, changes)
    assert applied2 == [] and skipped2[0]["reason"] == "target_occurs_0_times"


def test_edit_append_style_not_doubled_and_not_blocked_elsewhere(tmp_path):
    text = "别处已有 新增句乙。\n\n目标段：段落甲。"
    changes = [{"operation": "replace", "original_text": "段落甲。",
                "replacement_text": "段落甲。新增句乙。", "reason": "衔接"}]
    new_text, applied, _ = editor.apply_text_edits(text, changes)
    assert applied and "目标段：段落甲。新增句乙。" in new_text
    _, applied2, skipped2 = editor.apply_text_edits(new_text, changes)
    assert applied2 == [] and skipped2[0]["reason"] == "already_applied"
    assert new_text.count("新增句乙。") == 2  # the elsewhere one + the applied one


def test_front_back_serial_dependency_messages_carry_parsed_parts(tmp_path):
    work = tmp_path / "manuscript.md"
    work.write_text("# 题\n\n正文一段。", encoding="utf-8")
    fixture = {
        "fixture": True,
        "conclusion": {"conclusion": "SERIAL-结语标记：证据边界如上。"},
        "introduction": {"introduction": "SERIAL-引言标记：范围与承诺。"},
        "abstract": {"title": "t", "abstract": "SERIAL-摘要标记。",
                     "keywords": ["k1", "k2"]},
    }
    fixture_path = tmp_path / "PARTS.json"
    _write_json(fixture_path, fixture)
    report = front_back.run_front_back_stage(
        draft_path=work, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "stage", parts_fixture_path=fixture_path)
    assert report["status"] == "generated"

    intro_messages = json.loads(
        (tmp_path / "stage/messages/front_back_introduction_messages.json")
        .read_text(encoding="utf-8"))
    intro_user = intro_messages[1]["content"]
    assert "SERIAL-结语标记" in intro_user,         "the introduction request must read the PARSED conclusion"
    assert "已提炼的结语" in intro_user

    abstract_messages = json.loads(
        (tmp_path / "stage/messages/front_back_abstract_messages.json")
        .read_text(encoding="utf-8"))
    abstract_user = abstract_messages[1]["content"]
    assert "SERIAL-结语标记" in abstract_user and "SERIAL-引言标记" in abstract_user,         "the abstract request must read BOTH parsed front/back parts"

    conclusion_messages = json.loads(
        (tmp_path / "stage/messages/front_back_conclusion_messages.json")
        .read_text(encoding="utf-8"))
    assert "SERIAL-引言标记" not in conclusion_messages[1]["content"],         "the conclusion runs first and must not see later parts"


def test_oversized_drafts_are_rejected_as_unsupported(tmp_path):
    work = tmp_path / "huge.md"
    work.write_text("# 题\n\n" + ("正文。" * 200_000), encoding="utf-8")
    with pytest.raises(front_back.FrontBackError, match="too_long"):
        front_back.run_front_back_stage(
            draft_path=work, research_question="q", chapter_roles=CHAPTER_ROLES,
            out_dir=tmp_path / "stage", recordings={})
    with pytest.raises(editor.TextEditError, match="too_long"):
        editor.build_edit_passes(draft_text=work.read_text(encoding="utf-8"),
                                 chapter_roles=CHAPTER_ROLES)


# ---------------------------------------------------------------------------
# rework: introduction positioning and repeat-run replacement
# ---------------------------------------------------------------------------


def _intro_fixture(intro_text: str) -> dict:
    return {
        "fixture": True,
        "introduction": {"introduction": intro_text},
    }


def test_intro_repeat_runs_do_not_accumulate(tmp_path):
    assert REAL_DRAFT.is_file()
    work = tmp_path / "m.md"
    work.write_text(REAL_DRAFT.read_text(encoding="utf-8"), encoding="utf-8")
    fixture_v1 = tmp_path / "PARTS_V1.json"
    _write_json(fixture_v1, _intro_fixture("RUN1-引言开篇：范围与承诺一次。"))
    r1 = front_back.run_front_back_stage(
        draft_path=work, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "s1", parts_fixture_path=fixture_v1)
    assert r1["application_log"][0]["position"] == "body_introduction_chapter_opening_inserted"
    first = (tmp_path / "s1/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")

    # Second run over the ALREADY-EDITED manuscript with the same intro.
    work2 = tmp_path / "m2.md"
    work2.write_text(first, encoding="utf-8")
    r2 = front_back.run_front_back_stage(
        draft_path=work2, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "s2", parts_fixture_path=fixture_v1)
    assert r2["application_log"][0]["position"] ==         "body_introduction_chapter_opening_replaced", "later runs replace, not re-insert"
    second = (tmp_path / "s2/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert second.count("RUN1-引言开篇") == 1, "no accumulated copies"
    assert second.count(front_back._GENERATED_INTRO_START) == 1
    assert len(second) == len(first), "same intro twice must not add content"
    # Chapter body preserved.
    ch1 = second[second.index("第1章 引言"):second.index("第2章")]
    assert "> 本章论断：本章确立肠道微生物组" in ch1 and "临床紧迫性" in ch1


def test_intro_changed_version_keeps_only_new(tmp_path):
    work = tmp_path / "m.md"
    work.write_text(REAL_DRAFT.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(tmp_path / "V1.json", _intro_fixture("OLD-引言开篇：旧版本承诺。"))
    front_back.run_front_back_stage(
        draft_path=work, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "s1", parts_fixture_path=tmp_path / "V1.json")
    edited = (tmp_path / "s1/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")

    work2 = tmp_path / "m2.md"
    work2.write_text(edited, encoding="utf-8")
    _write_json(tmp_path / "V2.json", _intro_fixture("NEW-引言开篇：修订后的范围与承诺。"))
    r2 = front_back.run_front_back_stage(
        draft_path=work2, research_question="q", chapter_roles=CHAPTER_ROLES,
        out_dir=tmp_path / "s2", parts_fixture_path=tmp_path / "V2.json")
    final = (tmp_path / "s2/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert "NEW-引言开篇" in final and "OLD-引言开篇" not in final,         "only the new introduction version remains"
    assert final.count(front_back._GENERATED_INTRO_START) == 1
    assert r2["application_log"][0]["position"] == "body_introduction_chapter_opening_replaced"


def test_intro_located_by_role_when_title_lacks_the_word(tmp_path):
    # Real draft with 第1章's "引言" wording removed from the title; the role
    # mapping alone must still find and update that chapter.
    text = REAL_DRAFT.read_text(encoding="utf-8")
    text = text.replace(
        "## 第1章 引言：微生物组 - 免疫检查点轴的临床需求与理论基础",
        "## 第1章 研究背景与理论基础", 1)
    assert "第1章 引言" not in text
    work = tmp_path / "m.md"
    work.write_text(text, encoding="utf-8")
    roles = [
        {"chapter_id": "CH01", "title": "研究背景与理论基础", "role": "引言：交代背景与范围"},
        *[{"chapter_id": row["chapter_id"], "title": row["title"], "role": row["role"]}
          for row in CHAPTER_ROLES[1:]],
    ]
    _write_json(tmp_path / "V.json", _intro_fixture("ROLEMAP-引言开篇：按角色定位。"))
    r = front_back.run_front_back_stage(
        draft_path=work, research_question="q", chapter_roles=roles,
        out_dir=tmp_path / "s", parts_fixture_path=tmp_path / "V.json")
    final = (tmp_path / "s/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")
    assert r["application_log"][0]["chapter_heading"].startswith("## 第1章"),         "the role mapping resolves the first chapter without the word 引言"
    ch1 = final[final.index("第1章 研究背景"):final.index("第2章")]
    assert "ROLEMAP-引言开篇" in ch1
    assert "> 本章论断：" in ch1, "the chapter thesis quote is preserved"


def test_intro_chapter_id_match_precedes_order_and_avoids_prefix_match():
    manuscript = "## CH02 Clinical\n\n## CH010 Distractor\n\n## CH01 Foundations"
    roles = [
        {"chapter_id": "CH01", "title": "Foundations", "role": "introduction"},
        {"chapter_id": "CH02", "title": "Clinical", "role": "clinical"},
    ]

    assert front_back._introduction_chapter_heading(manuscript, roles) == "## CH01 Foundations"


def test_intro_chapter_title_matches_exact_english_heading():
    manuscript = "## Chapter 1 Foundations\n\nBody"
    roles = [{
        "chapter_id": "CH01",
        "title": "Chapter 1 Foundations",
        "role": "introduction",
    }]

    assert front_back._introduction_chapter_heading(manuscript, roles) == "## Chapter 1 Foundations"
