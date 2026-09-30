"""Work order content-handoff/02: the writer consumes each original owner task.

Arrangement tasks may merge several owner briefs (top-level point is only the
first, development a compatibility concatenation, source_uses a union) or
split one brief across paragraphs (`portion`).  The writer input must present
each original task's independent claim and its own source relations, and the
prompt must say so in generic language.  Markers are manual fixtures; the
real attempt02 packet and a real cross-domain card provide the real content.
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
    load_writer_prompt,
    unit_messages,
    unit_payload,
)


OWNER_A_POINT = "层状电池工况敏感性"
OWNER_A_DEV = ("层状电池在 12.5 MPa、80°C 下首圈放电 126 mAh/g，为 NCM811 理论容量的 70%；"
               "该比率以理论容量为基准，不是相对首圈的保持率。")
OWNER_B_POINT = "高压枝晶与机械失效"
OWNER_B_DEV = ("对称电池数据来自另一压力对比实验（160/107 mAh/g），与长循环 126 mAh/g "
               "不可拼接为同一实验的结果。")
OWNER_C_POINT = "新界面的引入"
OWNER_C_DEV = "新界面增加界面阻抗，需要单独的稳定性验证，当前材料未提供。"


def _write_packet(tmp_path: Path, *, sources: list[dict], unit_point: str = "unit point") -> Path:
    packet = {
        "chapter": {"chapter_id": "C1", "title": "Chapter"},
        "research_question": "fixture research question",
        "chapter_plan": {
            "thesis": "owner thesis",
            "units": [{
                "unit_id": "C1_U01",
                "substantive_point": unit_point,
                "evidence_conditions_and_limits": "单元级条件：冷压体系；60 分钟混合。",
                "cross_paper_synthesis_and_conflicts": "单元级综合：约束而非叠加。",
                "transition": "承接界面分析。",
                "paragraph_briefs": [
                    {"point": OWNER_A_POINT, "development": OWNER_A_DEV,
                     "source_handles": ["P0025"]},
                    {"point": OWNER_B_POINT, "development": OWNER_B_DEV,
                     "source_handles": ["P0025", "P0016"]},
                    {"point": OWNER_C_POINT, "development": OWNER_C_DEV,
                     "source_handles": ["P0025"]},
                ],
                "supporting_studies": [
                    {"source_handle": "P0030", "contribution": "跨章复用来源：全电池对比基准",
                     "conditions_limits": "全电池语境"},
                ],
            }],
        },
        "source_materials": sources,
        "source_identity_map": {row["source_handle"]: {"paper_id": row["paper_id"]}
                                for row in sources},
    }
    path = tmp_path / "WRITER_PACKET.json"
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    return path


def _source_row(handle: str, material_note: str, card_path: str | None = None) -> dict:
    row = {"source_handle": handle, "paper_id": f"paper-{handle}",
           "study_summary_A": {"finding": material_note}}
    if card_path:
        row["card_path"] = card_path
    return row


def _prepare(tmp_path: Path, sources: list[dict]):
    packet_path = _write_packet(tmp_path, sources=sources)
    view = build_chapter_view(packet_path, shared_outline=[], review_argument="argument",
                              id_map_path=tmp_path / "ID_MAP.json")
    write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    return view


def _merged_response(view, *, split=False):
    brief_ids = [brief.paragraph_id for brief in view.units[0].paragraph_briefs]
    all_handles = {source.source_handle for source in view.sources}
    used = {"P0025", "P0016"}
    if split:
        tasks = [
            {"paragraph_id": "C1_U01_P01a", "source_briefs": [brief_ids[0]],
             "portion": "本段只展开 12.5 MPa/80°C 的容量与其理论容量分母",
             "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            {"paragraph_id": "C1_U01_P01b", "source_briefs": [brief_ids[0]],
             "portion": "本段只说明 160/107 属于另一压力对比实验",
             "source_uses": [{"source_handle": "P0025", "role": "局限", "use": "u"}]},
            {"source_briefs": [brief_ids[1]],
             "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"},
                             {"source_handle": "P0016", "role": "主论据", "use": "u"}]},
            {"source_briefs": [brief_ids[2]],
             "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
        ]
    else:
        tasks = [
            {"paragraph_id": brief_ids[0], "source_briefs": brief_ids,
             "point": "只保留的第一条主张",
             "development": "只保留的第一条拼接展开。",
             "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"},
                             {"source_handle": "P0016", "role": "主论据", "use": "u"}]},
        ]
    return {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{"unit_id": "C1_U01", "focus": "焦点", "paragraph_tasks": tasks}],
        "unused_sources": [{"source_handle": handle, "reason": "r"}
                           for handle in sorted(all_handles - used)],
    }


def _export_and_messages(tmp_path: Path, view, arrangement):
    exported = dict(arrangement)
    exported["source_catalog"] = build_source_catalog(view, arrangement)
    exported["chapter_tool_materials"] = compact_chapter_tool_materials(view)
    unit_dir = tmp_path / "arranged" / view.chapter_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    write_view(view, unit_dir / "ARRANGEMENT_INPUT.json")
    arrangement_path = unit_dir / "CHAPTER_ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps(exported, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8")
    wview = build_unit_view(arrangement_path, view.units[0].unit_id)
    return wview, unit_payload(wview, language="zh"), unit_messages(wview, planning_revision=True)


def _default_sources() -> list[dict]:
    return [
        _source_row("P0025", "P0025 material: layered-cell conditions and cycle data"),
        _source_row("P0016", "P0016 material: symmetric-cell pressure contrast"),
        _source_row("P0014", "P0014 material: composite densification"),
        _source_row("P0030", "P0030 material: full-cell comparison baseline"),
    ]


def test_merged_task_keeps_each_owner_claim_distinct_in_writer_input(tmp_path):
    view = _prepare(tmp_path, _default_sources())
    arrangement = validate_arrangement(_merged_response(view), view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    wview, payload, messages = _export_and_messages(tmp_path, view, arrangement)
    task = payload["paragraph_tasks"][0]
    details = task["source_brief_details"]
    assert [item["point"] for item in details] == [OWNER_A_POINT, OWNER_B_POINT, OWNER_C_POINT], \
        "each original owner task must stay an independent claim"
    assert details[0]["development"] == OWNER_A_DEV and details[1]["development"] == OWNER_B_DEV
    assert details[1]["source_handles"] == ["P0025", "P0016"], \
        "per-claim source relations survive the merge"
    blob = json.dumps(messages, ensure_ascii=False)
    for marker in (OWNER_A_POINT, OWNER_A_DEV, OWNER_B_POINT, OWNER_B_DEV, OWNER_C_POINT):
        assert marker in blob
    # The actual post-01 interface: top-level point is only the first owner
    # claim, development is the compatibility concatenation, and each
    # original task lives in source_brief_details — the prompt must tell the
    # writer to use the details, not just the top-level text.
    assert task["point"] == OWNER_A_POINT
    assert task["development"] == "\n\n".join([OWNER_A_DEV, OWNER_B_DEV, OWNER_C_DEV])


def test_split_portion_reaches_writer_with_shared_original(tmp_path):
    view = _prepare(tmp_path, _default_sources())
    arrangement = validate_arrangement(_merged_response(view, split=True), view,
                                       planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    wview, payload, messages = _export_and_messages(tmp_path, view, arrangement)
    first = payload["paragraph_tasks"][0]
    assert first["portion"] == "本段只展开 12.5 MPa/80°C 的容量与其理论容量分母"
    assert first["source_brief_details"][0]["development"] == OWNER_A_DEV, \
        "each split part still carries the original task it expands a portion of"
    blob = json.dumps(messages, ensure_ascii=False)
    assert "本段只说明 160/107 属于另一压力对比实验" in blob


def test_prompt_exposes_consumption_contract_in_generic_language():
    revision = load_writer_prompt(planning_revision=True)
    legacy = load_writer_prompt()
    for phrase in (
        "source_brief_details",
        "portion",
        "owner_unit_context",
        "独立主张",
        "温度/压力/时间/循环",
        "分开描述",
        "不整段拒写",
    ):
        assert phrase in revision, phrase
    assert "source_brief_details" not in legacy, "the old contract stays unchanged"
    for domain_word in ("ALD", "LLZO", "电解质", "sulfide", "氧化物"):
        assert domain_word not in revision, domain_word


def test_old_payload_shape_unchanged_without_details(tmp_path):
    view = _prepare(tmp_path, _default_sources())
    response = {
        "chapter_id": "C1", "chapter_argument": "argument",
        "units": [{
            "unit_id": "C1_U01", "focus": "焦点",
            "paragraph_tasks": [{
                "paragraph_id": view.units[0].paragraph_briefs[0].paragraph_id,
                "point": "模型改写", "development": "模型改写展开。",
                "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}]},
            ],
        }],
        "unused_sources": [{"source_handle": h, "reason": "r"}
                           for h in ("P0014", "P0016", "P0030")],
    }
    arrangement = validate_arrangement(response, view, planning_revision=False)
    wview, payload, _messages = _export_and_messages(tmp_path, view, arrangement)
    task = payload["paragraph_tasks"][0]
    assert "source_brief_details" not in task and "portion" not in task
    assert "owner_unit_context" not in payload
    assert task["point"] == "模型改写"


def test_unit_materials_input_once_even_when_shared_across_tasks(tmp_path):
    view = _prepare(tmp_path, _default_sources())
    arrangement = validate_arrangement(_merged_response(view, split=True), view,
                                       planning_revision=True)
    wview, payload, _messages = _export_and_messages(tmp_path, view, arrangement)
    handles = [row["source_handle"] for row in payload["sources"]]
    assert len(handles) == len(set(handles)), "shared unit materials are input once"
    assert {"P0025", "P0016"} <= set(handles)


def test_real_attempt02_u03_claims_all_reach_writer(tmp_path):
    real_packet = Path("outputs/review_v2_repair/05_real_test_20260929"
                       "/attempt02_production_path/UPDATED_WRITER_PACKET.json")
    assert real_packet.is_file()
    view = build_chapter_view(real_packet, shared_outline=[], review_argument="argument",
                              id_map_path=tmp_path / "ID_MAP.json")
    unit = next(u for u in view.units if u.unit_id == "C6_U03")
    brief_ids = [brief.paragraph_id for brief in unit.paragraph_briefs]
    write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    all_handles = {source.source_handle for source in view.sources}
    units = []
    used = set()
    for u in view.units:
        if u.unit_id == "C6_U03":
            tasks = [{
                "paragraph_id": brief_ids[0],
                "source_briefs": brief_ids,
                "point": "合并后的首条主张",
                "development": "合并拼接。",
                "source_uses": [{"source_handle": "P0025", "role": "主论据", "use": "u"}],
            }]
            used = {"P0025"}
        else:
            tasks = [{"source_briefs": [brief.paragraph_id],
                      "source_uses": [{"source_handle": handle, "role": "主论据", "use": "u"}
                                      for handle in brief.source_handles]}
                     for brief in u.paragraph_briefs]
            used.update(handle for brief in u.paragraph_briefs for handle in brief.source_handles)
        units.append({"unit_id": u.unit_id, "focus": "焦点", "paragraph_tasks": tasks})
    response = {
        "chapter_id": "C6", "chapter_argument": "argument",
        "units": units,
        "unused_sources": [{"source_handle": handle, "reason": "r"}
                           for handle in sorted(all_handles - used)],
    }
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
    payload = unit_payload(wview, language="zh")
    messages = unit_messages(wview, planning_revision=True)
    details = payload["paragraph_tasks"][0]["source_brief_details"]
    points = [item["point"] for item in details]
    assert points == ["界面稳定性与压力管理", "高压枝晶与机械失效风险", "新界面的引入与复杂性"]
    blob = json.dumps(messages, ensure_ascii=False)
    assert "80°C" in blob and "理论容量的 70%" in blob and "126 mAh/g" in blob
    assert "160/107" in blob, "the pressure-contrast experiment keeps its own context"


def test_other_domain_real_card_consumes_same_contract(tmp_path):
    card = Path("outputs/paper_cards/20260922_microbiome_batch/corrected_reading"
                "/aebdf732ab482ad463c0fff29facfe06177b127f/PAPER_READING_CARD.json")
    assert card.is_file(), "the real cross-domain card must stay available (read-only)"
    real_a = json.loads(card.read_text(encoding="utf-8")).get("general_understanding") or {}
    sources = [
        _source_row("P0025", "microbiome material", card_path=str(card)),
        _source_row("P0016", "second material"),
        _source_row("P0014", "third material"),
        _source_row("P0030", "fourth material"),
    ]
    view = _prepare(tmp_path, sources)
    arrangement = validate_arrangement(_merged_response(view), view, planning_revision=True)
    assert arrangement["validation"]["ok"], arrangement["validation"]
    wview, payload, messages = _export_and_messages(tmp_path, view, arrangement)
    details = payload["paragraph_tasks"][0]["source_brief_details"]
    assert [item["point"] for item in details] == [OWNER_A_POINT, OWNER_B_POINT, OWNER_C_POINT]
    entry = next(row for row in wview.materials if row["source_handle"] == "P0025")
    assert "宏基因组" in json.dumps(entry.get("study_summary_A") or {}, ensure_ascii=False), \
        "the real cross-domain card material reaches the writer through the catalog"
    blob = json.dumps(messages, ensure_ascii=False)
    assert OWNER_B_DEV in blob, \
        "the second owner claim reaches the writer input in the other domain too"
