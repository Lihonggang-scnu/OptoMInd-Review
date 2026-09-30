"""06 集成交付的配置准备脚本（一次性、可重跑、确定性）。

性质说明：本目录全部 JSON 都是“交付配置 + 人工标注 fixture”，不是模型输出。
- 编辑 fixture：锚定真实装配稿文本的追加式替换（演示 02 生产消费链路）。
- 首尾 fixture：可辨认的标题/摘要/关键词/引言/结语（演示 03 应用合同）。
- 身份目录：从真实运行产物 REFERENCES.json / writer packet 派生（只读复制）。
- 图资产：复用 04 验收样本图 fig_scope.png，演示图挂接与图注独有引用；
  不将演示图包装成真实科研成果。

运行：python make_06_configs.py  （在仓库根目录执行）
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

REAL_BATCH = REPO / "outputs/full_review_draft/20260927_run01"
REAL_MANIFEST = REPO / "outputs/unit_writing/20260927_astra_repair/DELIVERY_MANIFEST.json"
PLAN_PACKET = REPO / ("outputs/review_v2_repair/05_real_test_20260929/"
                      "attempt02_production_path/UPDATED_WRITER_PACKET.json")
PLAN_RECORDINGS = REPO / "outputs/review_v2_delivery/plan/recordings_real_03.json"
SAMPLE_FIG = REPO / "outputs/review_v2_delivery/04_figures_citations/assets/fig_scope.png"

OUT = Path(__file__).resolve().parent
EDIT_MARK_1 = "〔06集成fixture编辑一〕"
EDIT_MARK_2 = "〔06集成fixture编辑二〕"

# CH01's introduction identity is an existing delivery contract.  Keep that
# role, but obtain its title from the real assembled handle manuscript below.
# Other roles are copied only when the source manifest explicitly contains
# one; this fixture generator must not infer chapter duties from prose.
KNOWN_EXISTING_ROLES = {"CH01": "introduction"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _unique_prefix(text: str, line: str, start: int = 60) -> str:
    original = line[:start]
    while text.count(original) != 1:
        original = line[:len(original) + 20]
    return original


def _chapter_roles_from_sources(manifest_path: Path, manuscript_path: Path) -> list[dict[str, str]]:
    """Read chapter identities from the real manifest and assembled draft.

    The delivery config needs the exact title used by the assembled manuscript
    so the introduction role can resolve the existing body chapter.  Manifest
    chapter order supplies the structural identity; no role is inferred from a
    title or from chapter prose.  A mismatch is a hard error rather than an
    opportunity to guess.
    """
    manifest = json.loads(_read(manifest_path))
    chapters = manifest.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError(f"history_manifest_chapters_missing:{manifest_path}")

    manuscript = _read(manuscript_path)
    heading_re = re.compile(
        r"^##\s+(?P<prefix>(?:第(?P<number>[0-9]+)章|Chapter\s+(?P<chapter_number>[0-9]+)))\s+(?P<title>.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    headings = list(heading_re.finditer(manuscript))
    if len(headings) != len(chapters):
        raise ValueError(
            "history_manifest_heading_count_mismatch:"
            f"manifest={len(chapters)} manuscript={len(headings)}"
        )

    rows: list[dict[str, str]] = []
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            raise ValueError(f"history_manifest_chapter_not_object:{index}")
        chapter_id = str(chapter.get("chapter_id") or "").strip()
        if not chapter_id:
            raise ValueError(f"history_manifest_chapter_id_missing:{index}")
        heading = headings[index - 1]
        heading_number = heading.group("number") or heading.group("chapter_number")
        if heading_number and int(heading_number) != index:
            raise ValueError(
                "history_manifest_heading_order_mismatch:"
                f"chapter={chapter_id} expected={index} actual={heading_number}"
            )
        title = heading.group("title").strip()
        role = str(
            chapter.get("role")
            or chapter.get("chapter_role")
            or chapter.get("responsibility")
            or KNOWN_EXISTING_ROLES.get(chapter_id, "")
        ).strip()
        # Preserve the established CH01 role.  For all other chapters an
        # absent manifest role stays absent; do not invent duties here.
        if not role:
            continue
        rows.append({"chapter_id": chapter_id, "title": title, "role": role})
    return rows


def _pick_line(body_text: str, *, forbid_handles: bool):
    # Anchor on a REAL body line: 03 owns the keyword line (**关键词), so an
    # edit there would be superseded by the front/back application contract.
    candidates = [l for l in body_text.splitlines()
                  if 40 < len(l) < 240
                  and not l.startswith(("#", ">", "|", "**", "!["))]
    if forbid_handles:
        return next((l for l in candidates if "[P" not in l), candidates[0])
    return candidates[0]


def _edit_fixture(*originals: str) -> dict:
    marks = (EDIT_MARK_1, EDIT_MARK_2)
    return {
        "fixture": True,
        "note": "人工标注fixture：编辑建议锚定真实装配稿文本，非模型输出",
        "changes": [{"operation": "replace", "original_text": original,
                     "replacement_text": original + marks[i],
                     "reason": "06集成交付fixture：演示02编辑应用"}
                    for i, original in enumerate(originals)],
        "unresolved_questions": [],
    }


def _parts_fixture(title: str) -> dict:
    return {
        "fixture": True,
        "note": "人工标注fixture：首尾部件演示03应用合同，非模型输出",
        "conclusion": {"conclusion":
                       "（06集成fixture·结语）本综述按各章给出的证据边界收束："
                       "关联、机制与干预三条线的结论强度不同，"
                       "任何超出正文所列证据范围的推广都不成立。"},
        "introduction": {"introduction":
                         "（06集成fixture·引言）本节说明综述的范围与各章分工："
                         "先建立评估框架，再进入机制与调节因素，最后讨论干预与转化边界；"
                         "各章安排与结语承诺一致。"},
        "abstract": {"title": title,
                     "abstract": ("（06集成fixture·摘要）本摘要由06离线集成fixture生成，"
                                  "用于验证首尾部件进入出版链路；综合判断与限制"
                                  "以正文各章实际证据为准。"),
                     "keywords": ["06集成fixture", "离线交付", "综述工程"]},
    }


def _write_demo_figure(path: Path) -> None:
    """A visible labeled-placeholder figure (shapes only, no scientific claim).

    The 04 acceptance sample asset is a 1x1 pixel placeholder — invisible in
    PDF.  This draws a simple neutral diagram so the acceptance can verify
    image/caption adjacency; the caption keeps the fixture label.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (880, 440), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 859, 419], outline=(60, 60, 60), width=3)
    boxes = [(60, 90, 260, 190), (340, 90, 540, 190), (620, 90, 820, 190)]
    for box in boxes:
        draw.rectangle(box, outline=(40, 80, 160), width=3)
        draw.rectangle([box[0] + 18, box[1] + 18, box[2] - 18, box[3] - 54],
                       fill=(225, 235, 248))
        draw.rectangle([box[0] + 18, box[3] - 38, box[2] - 90, box[3] - 18],
                       fill=(248, 232, 210))
    for x in (260, 540):
        draw.line([x + 40, 140, x + 40, 140], fill=(40, 80, 160), width=3)
        draw.line([x, 140, x + 40, 140], fill=(40, 80, 160), width=3)
        draw.polygon([(x + 40, 133), (x + 40, 147), (x + 52, 140)],
                     fill=(40, 80, 160))
    draw.rectangle([60, 260, 820, 380], outline=(90, 90, 90), width=2)
    for row in range(4):
        y = 282 + row * 24
        draw.line([80, y, 800, y], fill=(160, 160, 160), width=2)
    draw.rectangle([80, 300, 260, 324], fill=(210, 228, 210))
    draw.rectangle([300, 324, 560, 348], fill=(228, 210, 210))
    img.save(path, "PNG")


def build_history_configs() -> None:
    draft_text = _read(REAL_BATCH / "REVIEW_DRAFT_HANDLES.md")
    body = draft_text.split("## 参考文献")[0]
    line = _pick_line(body, forbid_handles=False)
    original = _unique_prefix(draft_text, line)

    _write_demo_figure(OUT / "HISTORY_FIG_SCOPE.png")
    refs_real = json.loads(_read(REAL_BATCH / "REFERENCES.json"))["references"]
    unused = [h for r in refs_real for h in r["handles"] if f"[{h}]" not in body]
    _write_json(OUT / "HISTORY_FIGURE_ASSETS.json", [{
        "figure_id": "FIG:06_history_demo", "path": "HISTORY_FIG_SCOPE.png",
        "caption": (f"06集成fixture演示图（复用04验收样本图，非新科研制图）："
                    f"证据地图示意 [{unused[0]}]。"),
        "anchor_probe": "## 第1章",
    }])
    rows = [{"source_handle": h, "handles": r["handles"],
             "paper_id": r["paper_id"] or h, "doi": r.get("doi") or "",
             "title": r.get("title") or "", "year": str(r.get("year") or ""),
             "namespace": "run_20260927"}
            for r in refs_real for h in r["handles"]]
    _write_json(OUT / "HISTORY_IDENTITY_CATALOGS.json",
                [{"namespace": "run_20260927", "entries": rows}])
    _write_json(OUT / "HISTORY_EDIT_FIXTURE.json", _edit_fixture(original))
    title = draft_text.splitlines()[0].lstrip("# ").strip()
    _write_json(OUT / "HISTORY_PARTS_FIXTURE.json", _parts_fixture(title))
    chapter_roles = _chapter_roles_from_sources(
        REAL_MANIFEST, REAL_BATCH / "REVIEW_DRAFT_HANDLES.md"
    )
    _write_json(OUT / "HISTORY_DELIVERY_CONFIG.json", {
        "schema": "review_v2_delivery.config.v1",
        "note": "06集成交付配置：history起点全链（含一次本地PDF编译验收）",
        "language": "zh",
        "research_question": "肠道微生物组与实体瘤免疫检查点治疗的关联、机制与干预证据如何分层（06集成fixture沿用原运行问题口径）",
        "chapter_roles": chapter_roles,
        "text_edit": {"fixture": "HISTORY_EDIT_FIXTURE.json"},
        "front_back": {"fixture": "HISTORY_PARTS_FIXTURE.json"},
        "identity_catalogs": {"path": "HISTORY_IDENTITY_CATALOGS.json"},
        "figure_assets": {"path": "HISTORY_FIGURE_ASSETS.json"},
        "table_moves": {}, "figure_moves": {},
        "compile_pdf": True,
    })
    print(f"history: edit anchor {len(original)} chars; "
          f"caption-only handle {unused[0]}; catalogs {len(rows)} rows; "
          f"chapter roles {chapter_roles}")


def build_plan_configs() -> None:
    packet = json.loads(_read(PLAN_PACKET))
    recordings = json.loads(_read(PLAN_RECORDINGS))["recordings"]
    body_text = json.loads(
        recordings["writer:C6_U01"]["response"]["content"])["body_markdown"]
    line = _pick_line(body_text, forbid_handles=True)
    original = _unique_prefix(body_text, line, start=40)

    rows = [{"source_handle": m["source_handle"],
             "handles": [m["source_handle"]],
             "paper_id": m.get("paper_id") or m["source_handle"],
             "doi": m.get("doi") or "", "title": m.get("title") or "",
             "year": str(m.get("year") or ""), "namespace": "plan_c6"}
            for m in packet["source_materials"]]
    _write_json(OUT / "PLAN_IDENTITY_CATALOGS.json",
                [{"namespace": "plan_c6", "entries": rows}])
    _write_json(OUT / "PLAN_EDIT_FIXTURE.json", _edit_fixture(original))
    _write_json(OUT / "PLAN_PARTS_FIXTURE.json",
                _parts_fixture("复合与混合策略：能否兼顾硫化物电导率与氧化物稳定性？（06集成fixture）"))
    _write_json(OUT / "PLAN_DELIVERY_CONFIG.json", {
        "schema": "review_v2_delivery.config.v1",
        "note": "06集成交付配置：plan起点受限交付（C6_U04缺录制保持pending）",
        "language": "zh",
        "research_question": str(packet.get("research_question") or "")[:400],
        "chapter_roles": [{"chapter_id": "C6", "title": "第六章", "role": ""}],
        "text_edit": {"fixture": "PLAN_EDIT_FIXTURE.json"},
        "front_back": {"fixture": "PLAN_PARTS_FIXTURE.json"},
        "identity_catalogs": {"path": "PLAN_IDENTITY_CATALOGS.json"},
        "table_moves": {}, "figure_moves": {},
        "compile_pdf": False,
    })
    print(f"plan: edit anchor {len(original)} chars (handle-free line); "
          f"catalogs {len(rows)} rows")


README = """# 06 集成交付配置目录

本目录全部内容为**交付配置 + 人工标注 fixture**，不是模型输出，不代表真实科研内容。

## 配置 schema（review_v2_delivery.config.v1）

| 字段 | 说明 |
|---|---|
| `language` | 交付语言（zh） |
| `research_question` | 研究问题，03 首尾部件构造消息必需 |
| `chapter_roles` | 章节角色列表（chapter_id/title/role），02/03 消息使用 |
| `text_edit` | 02 编辑输入：`{"fixture": 相对路径}` 或 `{"recordings": 相对路径}`，二者恰选其一 |
| `front_back` | 03 首尾输入：同上 |
| `identity_catalogs` | 04 身份目录：`{"path": …}` 或内联列表 |
| `figure_assets` | 04 挂图条目：figure_id/path/caption/anchor_probe；path 相对本配置文件 |
| `table_moves` / `figure_moves` | 交叉引用移动规则（可选） |
| `compile_pdf` | 是否本地编译 PDF（不联网装依赖） |

所有路径相对本配置文件所在目录。缺某阶段输入时该阶段明确报 pending，
不会静默退回“仅装配”。

## 正式命令（在仓库根目录执行）

```bash
# history 起点（全链，含一次本地 PDF 编译验收）
python run_review_harness.py --delivery-start history \\
  --delivery-manifest outputs/unit_writing/20260927_astra_repair/DELIVERY_MANIFEST.json \\
  --delivery-batch-root outputs/full_review_draft/20260927_run01 \\
  --delivery-out outputs/review_v2_delivery/06_integration \\
  --delivery-config outputs/review_v2_delivery/06_integration/configs/HISTORY_DELIVERY_CONFIG.json

# plan 起点（受限交付，C6_U04 缺录制保持 pending）
python run_review_harness.py --delivery-start plan \\
  --delivery-packet outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/UPDATED_WRITER_PACKET.json \\
  --delivery-recordings outputs/review_v2_delivery/plan/recordings_real_03.json \\
  --delivery-out outputs/review_v2_delivery/06_integration \\
  --delivery-config outputs/review_v2_delivery/06_integration/configs/PLAN_DELIVERY_CONFIG.json
```

重新生成配置：`python outputs/review_v2_delivery/06_integration/configs/make_06_configs.py`
"""


if __name__ == "__main__":
    assert REAL_MANIFEST.is_file() and REAL_BATCH.is_dir()
    assert PLAN_PACKET.is_file() and PLAN_RECORDINGS.is_file()
    build_history_configs()
    build_plan_configs()
    (OUT / "README.md").write_text(README, encoding="utf-8")
    print("configs written to", OUT)
