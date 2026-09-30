"""Work order review-v2/04: delivery consumers, case cache, titles, handles.

Offline only.  Real writer result shapes drive the feedback loop and the
assembly; markers are manual fixtures.  These tests assert what the final
consumers report and render, not model behavior.
"""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.review_unit_writer import write_unit_output


def _write_card(tmp_path: Path, paper_id: str, a: dict, b: dict) -> dict:
    path = tmp_path / "cards" / paper_id / "PAPER_READING_CARD.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"general_understanding": a, "review_planning": b},
                               ensure_ascii=False), encoding="utf-8")
    return {
        "_paper_id": paper_id,
        "card_path": str(path),
        "planning_view": {
            "paper_identity": {
                "canonical_paper_id": paper_id,
                "title": f"Study {paper_id}",
                "doi": f"10.0000/{paper_id}",
                "year": "2025",
            },
            "planning_summary": b.get("planning_summary", ""),
        },
    }


# ---------------------------------------------------------------------------
# item 1: real writer return shapes in the feedback loop
# ---------------------------------------------------------------------------


def _feedback_fixture(tmp_path: Path, *, units=None):
    packet_path = tmp_path / "WRITER_PACKET.json"
    packet_path.write_text(json.dumps({
        "chapter": {"chapter_id": "C1", "title": "Concept"},
        "chapter_plan": {"thesis": "old", "units": [
            {"unit_id": "C1_U01", "point": "old", "source_handles": ["P0001"]}]},
        "source_materials": [{"source_handle": "P0001", "paper_id": "paper-1",
                              "study_summary_A": {"finding": "old"}}],
    }), encoding="utf-8")
    arrangement_path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps({"chapter_id": "C1", "units": [
        {"unit_id": unit} for unit in (units or ["C1_U01"])]}), encoding="utf-8")
    return packet_path, arrangement_path


def test_writer_incomplete_body_is_not_reported_resolved(tmp_path):
    packet_path, arrangement_path = _feedback_fixture(tmp_path)

    def owner(*_):
        return {"chapter_updates": [{"chapter_id": "C1", "updated_plan": {
            "thesis": "revised", "units": [
                {"unit_id": "C1_U01", "point": "revised", "source_handles": ["P0001"]}]}}]}

    def arrange(*_):
        return {"chapter_id": "C1", "units": [{"unit_id": "C1_U01"}]}

    # Real production writer shape: body exists, completion says partial, new issues.
    def writer(packet, _arrangement, output_dir):
        view = None

        class _View:
            chapter_id = "C1"
            unit_id = "C1_U01"
            sources = {"P0001": {}}
            material_notes: list = []
            warnings: list = []
            arrangement_path = ""
            view_path = ""

            def material_summary(self):
                return {}

        view = _View()
        return write_unit_output(
            view, "正文已写出但被长度截断", output_dir / "writer",
            model="fixture", language="zh", mode="run", used_messages=[],
            estimate={}, finish_reason="length", complete=False,
            issues=[{"unit_id": "C1_U01", "problem": "机制部分被截断", "action": "chapter_owner"}],
        )

    result = planning.run_feedback_loop(
        packet_path=packet_path, arrangement_path=arrangement_path,
        arrangement={"chapter_id": "C1", "issues": [
            {"action": "chapter_owner", "problem": "revise"}]},
        owner_planner=owner, arrangement_runner=arrange, writer_runner=writer,
        output_dir=tmp_path / "feedback",
    )
    assert result["status"] == "partial", "an incomplete body must not be reported resolved"
    assert result["writer_completion"] == "partial_length"
    assert result["writer_complete"] is False
    assert result["writer_issues"] and result["writer_issues"][0]["problem"] == "机制部分被截断"


def test_multi_unit_split_reports_pending_units(tmp_path):
    packet_path, arrangement_path = _feedback_fixture(tmp_path)

    def owner(*_):
        return {"chapter_updates": [{"chapter_id": "C1", "updated_plan": {
            "thesis": "split", "units": [
                {"unit_id": "C1_U01A", "point": "A", "source_handles": ["P0001"]},
                {"unit_id": "C1_U01B", "point": "B", "source_handles": ["P0001"]},
            ]}}], "unit_id_remap": {"C1_U01A": ["C1_U01"], "C1_U01B": ["C1_U01"]}}

    def arrange(*_):
        return {"chapter_id": "C1", "units": [
            {"unit_id": "C1_U01A"}, {"unit_id": "C1_U01B"}]}

    seen_requests: list[str] = []

    def writer(packet, arrangement, output_dir):
        # Real CLI shape: only one unit was rewritten; the rest stay pending.
        seen_requests.append("writer")
        return {"status": "partial",
                "affected_units": ["C1_U01A", "C1_U01B"],
                "written_units": ["C1_U01A"],
                "reason": "selected_unit_required_for_multiple_units"}

    result = planning.run_feedback_loop(
        packet_path=packet_path, arrangement_path=arrangement_path,
        arrangement={"chapter_id": "C1", "issues": [
            {"action": "chapter_owner", "problem": "split"}]},
        owner_planner=owner, arrangement_runner=arrange, writer_runner=writer,
        output_dir=tmp_path / "feedback",
    )
    assert result["status"] == "partial"
    assert sorted(result.get("pending_units") or []) == ["C1_U01A", "C1_U01B"], \
        "the unresolved affected-units list must reach the final report"


# ---------------------------------------------------------------------------
# item 1 (assembly): restricted draft keeps the body, separates the flags
# ---------------------------------------------------------------------------


def _assembly_fixture(tmp_path: Path, *, unit_result_overrides: dict | None = None,
                      unit_title: str | None = None, table_body: str | None = None):
    unit = {"unit_id": "U1", "focus": "一个很长很长的单元焦点句，用来验证标题回退行为是否稳定可靠。"}
    if unit_title:
        unit["unit_title"] = unit_title
    arrangement = tmp_path / "arrangement.json"
    arrangement.write_text(json.dumps({
        "chapter_id": "CH01", "title": "材料与方法",
        "units": [unit],
        "source_catalog": {
            "P0026": {"paper_id": "doi:10.0000/known", "title": "Known study",
                      "year": "2024", "doi": "10.0000/known"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "review_title": "固态电解质研究", "chapters": [
            {"chapter_id": "CH01", "arrangement_path": str(arrangement)}],
    }, ensure_ascii=False), encoding="utf-8")
    body = table_body or "具体解释材料性质与适用条件 [P0026]。"
    result = {
        "chapter_id": "CH01", "unit_id": "U1", "complete": True,
        "body_markdown": body,
    }
    result.update(unit_result_overrides or {})
    result_path = tmp_path / "U1.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "BATCH_JOBS.json").write_text(json.dumps([{
        "chapter_id": "CH01", "unit_id": "U1",
        "arrangement": str(arrangement), "reused_result": str(result_path),
    }]), encoding="utf-8")
    return ["--manifest", str(manifest), "--batch-root", str(tmp_path),
            "--output-root", str(tmp_path / "result")]


def test_partial_writer_body_assembles_restricted_draft_with_separate_flags(tmp_path):
    from scripts.upgrade3 import full_review_draft as draft

    argv = _assembly_fixture(tmp_path, unit_result_overrides={
        "complete": False, "finish_reason": "length",
        "completion_status": "partial_length",
        "issues": [{"unit_id": "U1", "problem": "末尾比较未完成", "action": "chapter_owner"}],
    })
    assert draft.main(argv) == 0
    summary = json.loads((tmp_path / "result/ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    assert summary["status"] == "complete", "assembly itself is complete"
    assert summary["problems_resolved"] is False, "the partial unit is an unresolved problem"
    assert any(row.get("pending_problem") for row in summary.get("unit_rows") or [])
    body = (tmp_path / "result/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "具体解释材料性质与适用条件" in body, "useful body text must survive"
    report = (tmp_path / "result/RUN_REPORT.md").read_text(encoding="utf-8")
    assert "待处理问题" in report and "partial_length" in report


def test_complete_unit_reports_problems_resolved(tmp_path):
    from scripts.upgrade3 import full_review_draft as draft

    argv = _assembly_fixture(tmp_path)
    assert draft.main(argv) == 0
    summary = json.loads((tmp_path / "result/ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    assert summary["status"] == "complete"
    assert summary["problems_resolved"] is True
    assert summary.get("pending_problems") == []


# ---------------------------------------------------------------------------
# item 3: unit title field wins over the truncated focus sentence
# ---------------------------------------------------------------------------


def test_unit_title_field_preferred_and_fallback_kept(tmp_path):
    from scripts.upgrade3 import full_review_draft as draft

    argv = _assembly_fixture(tmp_path, unit_title="复合界面工程")
    assert draft.main(argv) == 0
    body = (tmp_path / "result/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "1.1 复合界面工程" in body
    assert "一个很长很长的单元焦点句" not in body.split("## 第1章", 1)[1].split("具体解释", 1)[0]


def test_saved_payload_keeps_unit_title_for_cache_and_export():
    from scripts.upgrade3.chapter_arrangement import saved_payload_from

    payload = saved_payload_from({"units": [{
        "unit_id": "C6_U03", "title": "复合界面工程", "unit_title": "复合界面工程",
        "focus": "长句焦点", "paragraph_tasks": [], "table_tasks": [],
    }]})
    assert payload["units"][0]["unit_title"] == "复合界面工程"


def test_validate_arrangement_keeps_model_unit_title():
    from optomind_research.runtime.upgrade3.chapter_arrangement import (
        build_chapter_view,
        validate_arrangement,
    )
    packet = Path(__file__).parent / "data" / "rv2_title_packet.json"
    if not packet.is_file():  # build a minimal packet inline
        packet = Path(__file__).parent / "_rv2_title_packet_tmp.json"
        packet.write_text(json.dumps({
            "chapter": {"chapter_id": "C1", "title": "Chapter"},
            "chapter_plan": {"thesis": "t", "units": [{
                "point": "p", "paragraph_briefs": [
                    {"point": "p", "development": "d", "source_handles": ["P0001"]}],
            }]},
            "source_materials": [{"source_handle": "P0001", "paper_id": "p1",
                                  "study_summary_A": {"finding": "f"}}],
        }, ensure_ascii=False), encoding="utf-8")
    view = build_chapter_view(packet, shared_outline=[], id_map_path=packet.parent / "ID_MAP_title.json")
    response = {"chapter_id": "C1", "chapter_argument": "a", "units": [{
        "unit_id": view.units[0].unit_id, "title": "复合界面工程",
        "focus": view.units[0].title, "paragraph_tasks": [],
    }]}
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["units"][0].get("unit_title") == "复合界面工程"


# ---------------------------------------------------------------------------
# item 4: known residual handles in table source columns become numbers
# ---------------------------------------------------------------------------


def test_table_source_column_known_handle_becomes_reference_number(tmp_path):
    from scripts.upgrade3 import full_review_draft as draft

    table = ("正文先引用该来源 [P0026]。\n\n"
             "| 路线 | 电导率 | 说明 |\n| --- | --- | --- |\n"
             "| P0026 | $10^{-2}$ S/cm | 高电导路线 |\n"
             "| P9999 | 未知 | 未映射来源 |")
    argv = _assembly_fixture(tmp_path, table_body=table)
    assert draft.main(argv) == 0
    body = (tmp_path / "result/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    table_line = next(line for line in body.splitlines() if line.startswith("| P") or line.startswith("| ["))
    assert "[1]" in table_line, "the known handle must show its formal reference number"
    assert "P0026" not in table_line
    unknown_line = next(line for line in body.splitlines() if "未知" in line and line.startswith("|"))
    assert "P9999" in unknown_line, "unknown handles are not guessed"
    summary = json.loads((tmp_path / "result/ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    assert summary.get("table_handle_replacements") == 1
    assert "P9999" in (summary.get("unknown_table_handles") or [])


def test_prose_bare_handle_is_not_blindly_replaced(tmp_path):
    from scripts.upgrade3 import full_review_draft as draft

    body = "编号 P0026 在正文中作为普通标识出现，且并非引用括号。"
    argv = _assembly_fixture(tmp_path, table_body=body)
    assert draft.main(argv) == 0
    text = (tmp_path / "result/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "编号 P0026 在正文中作为普通标识出现" in text


# ---------------------------------------------------------------------------
# item 2: the case batch cache consumes tasks, material and prompt contract
# ---------------------------------------------------------------------------


def _case_unit_row(point: str, handles=("P0001",)):
    return {"unit_key": "C1:1", "chapter_id": "C1", "chapter_title": "Chapter one",
            "unit": {"point": point,
                     "paragraph_briefs": [{"point": point, "development": "d",
                                           "source_handles": list(handles)}],
                     "source_handles": list(handles)}}


def test_case_batch_cache_signatures_separate_tasks_from_reuse():
    rows = [_case_unit_row("原始任务判断")]
    baseline = planning._case_unit_task_signature(rows)
    assert planning._case_unit_task_signature([_case_unit_row("原始任务判断")]) == baseline, \
        "identical inputs reuse the cached batch"
    changed = planning._case_unit_task_signature([_case_unit_row("改变了的单元任务")])
    assert changed != baseline, "a task edit under the same source list must not reuse"
    assert planning.CASE_GROUPS_PROMPT_CONTRACT, "the prompt contract participates in cache identity"


def test_case_signature_includes_routing_and_research_question():
    rows = [_case_unit_row("same task")]
    context = {"research_question": "original", "source_routing": [{"interpretation_limits": "original limits"}]}
    baseline = planning._case_unit_task_signature(rows, context=context)
    assert baseline == planning._case_unit_task_signature(rows, context=dict(context))
    assert baseline != planning._case_unit_task_signature(rows, context={**context, "research_question": "changed"})
    assert baseline != planning._case_unit_task_signature(rows, context={**context, "source_routing": [{"interpretation_limits": "changed limits"}]})


def test_case_batch_cache_reuses_identical_resume_and_refreshes_on_material_update(tmp_path):
    papers = [
        _write_card(tmp_path, "paper-a", {"finding": "original A content"},
                    {"planning_summary": "B"}),
        _write_card(tmp_path, "paper-b", {"finding": "B content"}, {"planning_summary": "B2"}),
    ]
    captured: dict = {}

    def planner(stage, payload):
        captured.setdefault(stage, 0)
        captured[stage] += 1
        if stage == "provisional_scope":
            return {"review_title": "Fixture", "shared_outline": [{"chapter_id": "C1", "title": "Chapter one"}]}
        if stage == "level1_outline":
            return {"shared_scope": {}, "shared_outline": [{"chapter_id": "C1", "title": "Chapter one"}]}
        if stage == "source_routing":
            return {"source_routes": [{
                "source_handle": row.get("source_handle"), "chapter_ids": ["C1"],
                "specific_usable_material": "note"} for row in payload["candidate_batch"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [
                {"chapter_id": "C1", "title": "Chapter one", "source_ids": ["paper-a"]}]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": [{"chapter_id": "C1", "title": "Chapter one"}],
                    "chapters": [{"chapter_id": "C1", "title": "Chapter one", "source_ids": ["paper-a"]}]}
        if stage == "chapter_need_analysis":
            return {"supplement_requests": [], "directed_reads": []}
        if stage == "chapter_details":
            return {"chapter_plan": {"thesis": "t", "units": [{
                "point": "unit point",
                "paragraph_briefs": [{"point": "p", "development": "d", "source_handles": ["P0001"]}]}]}}
        if stage == "case_groups":
            return {"additions": []}
        if stage == "whole_plan_improvement":
            return {"improvement_notes": []}
        if stage == "affected_chapter_revision":
            return {"status": "no_change"}
        raise AssertionError(stage)

    pool_path = tmp_path / "pool.jsonl"
    pool_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in papers),
                         encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"question_en": "fixture question",
                                     "facets": [{"id": "F1", "ask": "fixture"}]}), encoding="utf-8")
    cfg = planning.ProgressivePlannerConfig(
        topic_id="rv2-04-cache", pool_path=pool_path, plan_path=plan_path,
        output_dir=tmp_path / "run", shared_deep_read_budget=0, chapter_workers=1,
        planning_revision_enabled=True,
    )

    def make_flow():
        return planning.ProgressiveReviewPlanner(
            cfg, planner=planner,
            retrieval_loop_runner=lambda **kwargs: {
                "phase": kwargs.get("phase"), "status": "complete",
                "supplement_results": [], "directed_results": [],
                "tool_materials_by_chapter": {}, "consumed_paper_ids": []},
        )

    make_flow().run()
    first_case_calls = captured["case_groups"]
    assert first_case_calls == 1
    cache_files = list((tmp_path / "run/stages/case_groups").glob("*.json"))
    assert cache_files and json.loads(cache_files[0].read_text(encoding="utf-8")).get("task_signature")
    assert json.loads(cache_files[0].read_text(encoding="utf-8")).get("prompt_contract") == \
        planning.CASE_GROUPS_PROMPT_CONTRACT

    # Identical resume: the cached batch is reused, no new case call.
    make_flow().run(resume=True)
    assert captured["case_groups"] == first_case_calls, "identical inputs must reuse"

    # Same handle, updated research content: the batch answer must refresh.
    card_path = Path(papers[0]["card_path"])
    card = json.loads(card_path.read_text(encoding="utf-8"))
    card["general_understanding"]["finding"] = "UPDATED research content for the same handle"
    card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    make_flow().run(resume=True)
    assert captured["case_groups"] == first_case_calls + 1, \
        "updated research content for the same handle must not reuse the cached answer"
