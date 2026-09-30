"""Work order content-handoff/01: the owner's paragraph tasks survive arrangement.

The arrangement model may reorder, merge and split, but the writer must
receive the owner's original point/development/source_handles restored by the
program from explicit references.  Fixtures provide structure only; the
distinctive conditions below are manual markers of the channel, not science
claims.
"""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view,
    build_source_catalog,
    compact_chapter_tool_materials,
    validate_arrangement,
    write_view,
)
from optomind_research.runtime.upgrade3.review_unit_writer import (
    build_unit_view,
    unit_messages,
)

OWNER_P01_DEV = ("硫化物室温电导率可达 10⁻² S/cm 但 80°C 以下界面阻抗上升；"
                 "对比氧化物的低电导与高稳定性。")
OWNER_P02_DEV = ("复合通过界面约束重构两相行为；限制条件：仅在 300 MPa 冷压体系验证，"
                 "未覆盖长期循环。")
OWNER_P03_DEV = ("层状电池在 12.5 MPa、80°C 下首圈放电 126 mAh/g，为理论容量的 70%；"
                 "160/107 来自另一压力对比实验，不可与长循环混用。")

CROSS_CHAPTER_DEV = "跨章复用来源在本章的实际用途：提供全电池语境的对比基准。"


def _write_packet(tmp_path: Path) -> Path:
    packet = {
        "chapter": {"chapter_id": "C1", "title": "Chapter"},
        "research_question": "fixture research question",
        "chapter_plan": {
            "thesis": "owner thesis",
            "units": [{
                "unit_id": "C1_U01",
                "substantive_point": "unit substantive point",
                "evidence_conditions_and_limits": "单元级条件：仅在冷压体系验证；60 分钟混合。",
                "cross_paper_synthesis_and_conflicts": "单元级综合：两相关系是约束而非叠加。",
                "transition": "承接下一单元的界面分析。",
                "paragraph_briefs": [
                    {"point": "单一材料各有硬瓶颈", "development": OWNER_P01_DEV,
                     "source_handles": ["P0018", "P0026"]},
                    {"point": "复合的理论动机", "development": OWNER_P02_DEV,
                     "source_handles": ["P0014"]},
                    {"point": "工况敏感性", "development": OWNER_P03_DEV,
                     "source_handles": ["P0025"]},
                ],
                "supporting_studies": [
                    {"source_handle": "P0030", "contribution": CROSS_CHAPTER_DEV,
                     "conditions_limits": "全电池语境"},
                ],
            }],
        },
        "source_materials": [
            {"source_handle": handle, "paper_id": f"paper-{handle}",
             "study_summary_A": {"finding": f"material of {handle}"}}
            for handle in ("P0018", "P0026", "P0014", "P0025", "P0030")
        ],
        "source_identity_map": {handle: {"paper_id": f"paper-{handle}"}
                                for handle in ("P0018", "P0026", "P0014", "P0025", "P0030")},
    }
    path = tmp_path / "WRITER_PACKET.json"
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    return path


def _prepare(tmp_path: Path):
    packet_path = _write_packet(tmp_path)
    view = build_chapter_view(packet_path, shared_outline=[], review_argument="argument",
                              id_map_path=tmp_path / "ID_MAP.json")
    write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    return view


def _export_and_writer_messages(tmp_path: Path, view, arrangement):
    exported = dict(arrangement)
    exported["source_catalog"] = build_source_catalog(view, arrangement)
    exported["chapter_tool_materials"] = compact_chapter_tool_materials(view)
    unit_dir = tmp_path / "arranged" / view.chapter_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    write_view(view, unit_dir / "ARRANGEMENT_INPUT.json")
    arrangement_path = unit_dir / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(
        json.dumps(exported, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    wview = build_unit_view(arrangement_path, view.units[0].unit_id)
    return wview, unit_messages(wview, planning_revision=True)


def _brief_ids(view):
    return [brief.paragraph_id for brief in view.units[0].paragraph_briefs]


# ---------------------------------------------------------------------------
# the work-order reverse example: the model compresses, the writer still
# receives the owner's original task
# ---------------------------------------------------------------------------


def test_condensing_model_cannot_strip_owner_conditions(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1",
        "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01",
            "focus": "焦点",
            "paragraph_tasks": [{
                "paragraph_id": brief_ids[2],
                "point": "工况敏感性：在特定条件下表现稳定。",
                "development": "说明该层状电池在特定堆叠压力和温度下能实现稳定循环。",
                "source_briefs": [brief_ids[2]],
                "source_uses": [
                    {"source_handle": "P0025", "role": "主论据", "use": "支撑压力管理分析"}],
            }, {
                "paragraph_id": brief_ids[0],
                "point": "单一材料瓶颈。",
                "development": "对比两类材料。",
                "source_briefs": [brief_ids[0]],
                "source_uses": [
                    {"source_handle": "P0018", "role": "背景", "use": "背景"},
                    {"source_handle": "P0026", "role": "主论据", "use": "数据"}],
            }, {
                "paragraph_id": brief_ids[1],
                "point": "复合动机。",
                "development": "说明复合逻辑。",
                "source_briefs": [brief_ids[1]],
                "source_uses": [
                    {"source_handle": "P0014", "role": "主论据", "use": "机制"}],
            }],
        }],
        "unused_sources": [{"source_handle": "P0030", "reason": "本单元不使用跨章基准"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    task = arrangement["units"][0]["paragraph_tasks"][0]
    assert "80°C" in task["development"] and "126 mAh/g" in task["development"], \
        "the restored task must carry the owner's original conditions, not the compression"
    assert task["source_briefs"] == [brief_ids[2]]

    wview, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    blob = json.dumps(messages, ensure_ascii=False)
    assert "80°C" in blob and "126 mAh/g" in blob and "理论容量的 70%" in blob
    assert "160/107 来自另一压力对比实验" in blob, "the cross-experiment boundary travels too"
    # The owner's unit-level conditions and synthesis ride with the unit.
    assert "单元级条件：仅在冷压体系验证" in blob
    assert "单元级综合：两相关系是约束而非叠加" in blob


def test_reorder_keeps_model_order_with_owner_content(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"source_briefs": [brief_ids[2]], "point": "压缩", "development": "压缩",
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[0]], "point": "压缩", "development": "压缩",
                 "source_uses": [{"source_handle": "P0018", "role": "背景", "use": "u"}]},
                {"source_briefs": [brief_ids[1]], "point": "压缩", "development": "压缩",
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0030", "reason": "本单元不使用跨章基准"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert [task["source_briefs"][0] for task in tasks] == [brief_ids[2], brief_ids[0], brief_ids[1]]
    assert "12.5 MPa" in tasks[0]["development"]
    wview, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    blob = json.dumps(messages, ensure_ascii=False)
    assert blob.index("12.5 MPa") < blob.index("单一材料各有硬瓶颈"), \
        "the model's order is the writing order; the content is the owner's"


def test_merge_keeps_every_brief_relation(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"paragraph_id": "C1_U01_P01",
                 "point": "合并后的承重判断",
                 "source_briefs": [brief_ids[0], brief_ids[1]],
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[2]],
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0030", "reason": "本单元不使用跨章基准"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    merged = arrangement["units"][0]["paragraph_tasks"][0]
    assert OWNER_P01_DEV in merged["development"] and OWNER_P02_DEV in merged["development"], \
        "a merge keeps each original task's object-setting-result relation"
    handles = {use["source_handle"] for use in merged["source_uses"]}
    assert {"P0018", "P0026", "P0014"} <= handles, "brief handles must reach source_uses"
    details = merged["source_brief_details"]
    assert [item["paragraph_id"] for item in details] == brief_ids[:2]
    assert details[0]["point"] == "单一材料各有硬瓶颈"
    assert details[1]["point"] == "复合的理论动机"
    assert details[1]["source_handles"] == ["P0014"]
    wview, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    blob = json.dumps(wview.to_dict(), ensure_ascii=False)
    assert OWNER_P01_DEV in blob and OWNER_P02_DEV in blob
    writer_payload = json.loads(messages[1]["content"])
    assert writer_payload["paragraph_tasks"][0]["source_brief_details"] == details


def test_split_references_original_with_explicit_ids_and_portions(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"paragraph_id": "C1_U01_P03a",
                 "source_briefs": [brief_ids[2]],
                 "portion": "本段只讲 12.5 MPa 与 80°C 下的容量与分母",
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
                {"paragraph_id": "C1_U01_P03b",
                 "source_briefs": [brief_ids[2]],
                 "portion": "本段只讲 160/107 属于另一压力对比实验的边界",
                 "source_uses": [{"source_handle": "P0025", "role": "局限", "use": "u"}]},
                {"source_briefs": [brief_ids[0]],
                 "source_uses": [{"source_handle": "P0018", "role": "背景", "use": "u"}]},
                {"source_briefs": [brief_ids[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0030", "reason": "本单元不使用跨章基准"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert tasks[0]["paragraph_id"] == "C1_U01_P03a"
    assert tasks[1]["portion"] == "本段只讲 160/107 属于另一压力对比实验的边界"
    assert all(OWNER_P03_DEV in task["development"] for task in tasks[:2]), \
        "each split part carries the original task it explains a portion of"
    wview, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    blob = json.dumps(messages, ensure_ascii=False)
    assert "本段只讲 12.5 MPa 与 80°C 下的容量与分母" in blob


def test_split_without_output_ids_keeps_unique_task_ids_and_brief_details(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1",
        "units": [{
            "unit_id": "C1_U01",
            "paragraph_tasks": [
                {"source_briefs": [brief_ids[2]], "portion": "容量与分母",
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[2]], "portion": "另一实验边界",
                 "source_uses": [{"source_handle": "P0025", "role": "局限", "use": "u"}]},
                {"source_briefs": [brief_ids[0]],
                 "source_uses": [{"source_handle": "P0018", "role": "背景", "use": "u"}]},
                {"source_briefs": [brief_ids[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0030", "reason": "本单元不使用跨章基准"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert tasks[0]["paragraph_id"] != tasks[1]["paragraph_id"]
    assert tasks[0]["paragraph_id"] not in brief_ids
    assert tasks[1]["paragraph_id"] not in brief_ids
    assert tasks[0]["source_brief_details"][0]["paragraph_id"] == brief_ids[2]
    assert tasks[1]["source_brief_details"][0]["paragraph_id"] == brief_ids[2]
    _, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    payload = json.loads(messages[1]["content"])
    assert [task["paragraph_id"] for task in payload["paragraph_tasks"][:2]] == [
        tasks[0]["paragraph_id"], tasks[1]["paragraph_id"]]
    assert [task["portion"] for task in payload["paragraph_tasks"][:2]] == ["容量与分母", "另一实验边界"]
    assert all(task["source_brief_details"][0]["source_handles"] == ["P0025"]
               for task in payload["paragraph_tasks"][:2])


def test_non_object_paragraph_does_not_shift_brief_restoration(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1",
        "units": [{
            "unit_id": "C1_U01",
            "paragraph_tasks": [
                None,
                {"source_briefs": [brief_ids[0]],
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[2]],
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0026", "reason": "已由 brief 关系携带"},
                            {"source_handle": "P0030", "reason": "本单元不使用"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert any(error.startswith("paragraph_not_object:") for error in arrangement["validation"]["errors"])
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert tasks[0]["source_brief_details"][0]["paragraph_id"] == brief_ids[0]
    assert tasks[1]["source_brief_details"][0]["paragraph_id"] == brief_ids[1]
    assert tasks[2]["source_brief_details"][0]["paragraph_id"] == brief_ids[2]
    assert not any(error.startswith("briefs_unclaimed:") for error in arrangement["validation"]["errors"])


def test_missing_output_id_avoids_explicit_sibling_id(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1",
        "units": [{
            "unit_id": "C1_U01",
            "paragraph_tasks": [
                {"paragraph_id": "C1_U01_P02", "source_briefs": [brief_ids[0]],
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[2]],
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0026", "reason": "已由 brief 关系携带"},
                            {"source_handle": "P0030", "reason": "本单元不使用"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert tasks[0]["paragraph_id"] == "C1_U01_P02"
    assert tasks[1]["paragraph_id"] != tasks[0]["paragraph_id"]
    assert tasks[1]["paragraph_id"] != brief_ids[1]
    assert tasks[1]["source_brief_details"][0]["paragraph_id"] == brief_ids[1]


def test_cross_chapter_reuse_source_reaches_writer_with_material(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"source_briefs": [brief_ids[0]], "point": "p", "development": "d",
                 "source_uses": [{"source_handle": "P0030", "role": "比较",
                                  "use": CROSS_CHAPTER_DEV},
                                 {"source_handle": "P0018", "role": "背景", "use": "u"}]},
                {"source_briefs": [brief_ids[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
                {"source_briefs": [brief_ids[2]],
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    wview, messages = _export_and_writer_messages(tmp_path, view, arrangement)
    blob = json.dumps(messages, ensure_ascii=False)
    assert CROSS_CHAPTER_DEV in blob
    assert "material of P0030" in blob, "the reused source keeps its catalog material"


def test_unclaimed_brief_is_reported_not_silently_dropped(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"source_briefs": [brief_ids[0]],
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": h, "reason": "r"}
                           for h in ("P0014", "P0025", "P0026")],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert not arrangement["validation"]["ok"]
    assert any(str(e).startswith("briefs_unclaimed:") for e in arrangement["validation"]["errors"]), \
        "dropping an owner paragraph without a claim is a visible failure"


def test_unknown_brief_reference_is_reported(tmp_path):
    view = _prepare(tmp_path)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"source_briefs": ["C1_U01_P09"],
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
                {"source_briefs": [_brief_ids(view)[1]],
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
                {"source_briefs": [_brief_ids(view)[2]],
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": "P0026", "reason": "r"}],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert not arrangement["validation"]["ok"]
    assert any("brief_reference_unknown" in str(e) for e in arrangement["validation"]["errors"])


def test_new_mode_fallback_restores_only_explicit_id_matches(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    # A legacy-shaped new-mode output: no source_briefs; task ids reuse the
    # original ids; the third task is a brand-new id with rewritten text.
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [
                {"paragraph_id": brief_ids[0], "point": "压缩成特定条件",
                 "development": "特定条件下表现稳定。",
                 "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
                {"paragraph_id": brief_ids[1], "point": "压缩", "development": "压缩",
                 "source_uses": [{"source_handle": "P0014", "role": "主论据", "use": "u"}]},
                {"paragraph_id": brief_ids[2], "point": "压缩", "development": "压缩",
                 "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
                {"paragraph_id": "C1_U01_P09", "point": "新衔接段", "development": "衔接。",
                 "source_uses": [{"source_handle": "P0030", "role": "背景", "use": "u"}]},
            ],
        }],
        "unused_sources": [],
    }
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    tasks = arrangement["units"][0]["paragraph_tasks"]
    assert "80°C" in tasks[0]["development"], "explicit id reuse restores the owner's text"
    assert tasks[3]["development"] == "衔接。", "a genuinely new task keeps the model's text"
    assert tasks[3].get("paragraph_id") in (
        arrangement["units"][0].get("unmapped_paragraph_tasks") or []) or \
        tasks[3]["paragraph_id"] in json.dumps(arrangement, ensure_ascii=False), \
        "unmapped tasks stay visible"


def test_legacy_mode_output_is_unchanged(tmp_path):
    view = _prepare(tmp_path)
    brief_ids = _brief_ids(view)
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [{
                "paragraph_id": brief_ids[0], "point": "模型改写的判断",
                "development": "模型改写的展开，不含原文条件。",
                "source_uses": [{"source_handle": "P0018", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": h, "reason": "r"}
                           for h in ("P0014", "P0025", "P0026")],
    }
    arrangement = validate_arrangement(response, view, planning_revision=False)
    task = arrangement["units"][0]["paragraph_tasks"][0]
    assert task["point"] == "模型改写的判断", "legacy contract keeps the model's text"
    assert "80°C" not in task["development"]
    assert not any(str(e).startswith("briefs_unclaimed") for e in arrangement["validation"]["errors"])
    assert "source_briefs" not in task and "owner_unit_context" not in arrangement["units"][0]


def test_real_attempt02_packet_conditions_survive_condensing_arrangement(tmp_path):
    real_packet = Path("outputs/review_v2_repair/05_real_test_20260929"
                       "/attempt02_production_path/UPDATED_WRITER_PACKET.json")
    assert real_packet.is_file(), "the real paid-run packet must stay available (read-only)"
    view = build_chapter_view(real_packet, shared_outline=[], review_argument="argument",
                              id_map_path=tmp_path / "ID_MAP.json")
    target = next(u for u in view.units if u.unit_id == "C6_U03")
    compressed_brief = target.paragraph_briefs[0]

    def task_for(brief):
        return {"source_briefs": [brief.paragraph_id],
                "source_uses": [{"source_handle": handle, "role": "主论据", "use": "u"}
                                for handle in brief.source_handles]}

    units = []
    used_handles: set[str] = {"P0025"}
    for unit in view.units:
        tasks = []
        for brief in unit.paragraph_briefs:
            if unit.unit_id == "C6_U03" and brief.paragraph_id == compressed_brief.paragraph_id:
                # What the real arrangement did in the paid run: compress the
                # condition away.  With the mapping, the program restores it.
                tasks.append({
                    "paragraph_id": brief.paragraph_id,
                    "point": "界面稳定性与压力管理：在特定堆叠压力和温度下稳定循环。",
                    "development": "说明在特定堆叠压力（12.5 MPa）和温度下稳定循环。",
                    "source_briefs": [brief.paragraph_id],
                    "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}],
                })
            else:
                tasks.append(task_for(brief))
                used_handles.update(brief.source_handles)
        units.append({"unit_id": unit.unit_id, "focus": "焦点", "paragraph_tasks": tasks})
    all_handles = {source.source_handle for source in view.sources}
    response = {
        "chapter_id": "C6", "chapter_argument": "argument",
        "units": units,
        "unused_sources": [{"source_handle": handle, "reason": "r"}
                           for handle in sorted(all_handles - used_handles)],
    }
    write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    arrangement = validate_arrangement(response, view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    exported = dict(arrangement)
    exported["source_catalog"] = build_source_catalog(view, arrangement)
    exported["chapter_tool_materials"] = compact_chapter_tool_materials(view)
    unit_dir = tmp_path / "arranged" / "C6"
    unit_dir.mkdir(parents=True, exist_ok=True)
    write_view(view, unit_dir / "ARRANGEMENT_INPUT.json")
    arrangement_path = unit_dir / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps(exported, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8")
    wview = build_unit_view(arrangement_path, "C6_U03")
    messages = unit_messages(wview, planning_revision=True)
    blob = json.dumps(messages, ensure_ascii=False)
    assert "80°C" in blob and "126 mAh/g" in blob and "理论容量的 70%" in blob, \
        "the writer input must carry the owner's original conditions even when the arrangement compressed them"
