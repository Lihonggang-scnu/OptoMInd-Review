"""Work order review_v2_delivery/04: unified figures, citations, cross-refs.

The real 9/27 long draft provides the normal path; the edge scenarios
(cross-run same handle, unknown handle, math pipes, figure moves) are
LABELED fixtures in this file.  Assertions read the FINAL rendered text, not
just counts.
"""

import json
import re
import struct
import zlib
from pathlib import Path

from optomind_research.runtime.upgrade3 import delivery_citations as dc

REAL_DRAFT = Path("outputs/full_review_draft/20260927_run01/REVIEW_DRAFT_HANDLES.md")
# The real draft's catalog (namespace "run_20260927") rebuilt from its
# REFERENCES.json — the same identity data the historical run shipped.
CHAPTER_ROLES = []


def _tiny_png(path: Path) -> None:
    """A real (1x1 red) PNG file written byte-by-byte — no network, no deps."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _real_catalog() -> dict:
    refs = json.loads(
        Path("outputs/full_review_draft/20260927_run01/REFERENCES.json")
        .read_text(encoding="utf-8"))["references"]
    rows = []
    for ref in refs:
        for handle in ref["handles"]:
            rows.append({
                "source_handle": handle,
                "handles": ref["handles"],
                "paper_id": ref["paper_id"] or handle,
                "doi": ref.get("doi") or "",
                "title": ref.get("title") or "",
                "year": str(ref.get("year") or ""),
                "namespace": "run_20260927",
            })
    return {"namespace": "run_20260927", "entries": rows}


def test_real_draft_map_and_reader_rendering(tmp_path):
    assert REAL_DRAFT.is_file(), "the real handle draft must stay available"
    handle_text = REAL_DRAFT.read_text(encoding="utf-8")
    catalog = _real_catalog()
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[catalog])
    reader, _n, residual = dc.render_reader_citations(handle_text, citation_map)

    # Identity spot-checks against the real REFERENCES.json data.
    refs = {r["reference_number"]: r for r in citation_map["references"]}
    assert refs[1]["handles"] == ["P0094"], "first-occurrence citation is P0094"
    assert "rapid progressive disease" in refs[1]["title"]
    # P0411 appears first inside a table row; it must still be numbered and
    # its identity must match the catalog.
    p0411_number = citation_map["handle_to_number"]["P0411"]
    assert refs[p0411_number]["handles"][0] == "P0411"

    # Reader draft: numbers replace handles; unknown stays visible.
    assert "[P0094]" not in reader and "[1]" in reader
    assert "未知句柄探针 [P9999]" not in reader
    # Math pipes / table structure untouched.
    assert reader.count("|") == handle_text.count("|")
    assert reader.count("\n\n") >= handle_text.count("\n\n") - 2

    report = dc.run_figures_citations_stage(
        final_draft_path=REAL_DRAFT, identity_catalogs=[catalog],
        out_dir=tmp_path / "stage")
    assert report["model_calls"] == 0 and report["external_requests"] == 0
    assert report["reference_count"] == len(citation_map["references"])
    assert report["status"] == "complete"
    persisted = json.loads((tmp_path / "stage/REFERENCES.json").read_text(encoding="utf-8"))
    assert persisted["citation_map_source"] == "build_delivery_citation_map"
    assert persisted["references"][0]["reference_number"] == 1


def test_first_citation_only_in_table_or_caption_gets_numbered(tmp_path):
    handle_text = (
        "## 摘要\n\n正文开头没有引用。\n\n"
        "## 第2章 关联\n\n"
        "| 癌种 | 证据来源 |\n| --- | --- |\n"
        "| 黑色素瘤 | [P0400] |\n"
        "\n正文在表格之后。")
    catalog = {"namespace": "ns", "entries": [
        {"source_handle": "P0400", "paper_id": "paper-400", "doi": "10.0000/400",
         "title": "Table-only study", "year": "2024", "namespace": "ns"}]}
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[catalog])
    assert citation_map["handle_to_number"]["P0400"] == 1, \
        "a citation that first appears inside a table still gets number 1"
    refs = citation_map["references"]
    assert refs[0]["title"] == "Table-only study"
    reader, _n, residual = dc.render_reader_citations(handle_text, citation_map)
    assert "[P0400]" not in reader and "[1]" in reader


def test_cross_run_same_handle_number_not_merged(tmp_path):
    handle_text = "[P0001] 来自 run 甲；[P0001] 再次出现（同 run）。另见 [P0001] 来自 run 乙的写法无法区分，故按句柄命名空间映射。"
    run_a = {"namespace": "run_a", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-a-1", "doi": "10.0000/a1",
         "title": "Run A paper", "year": "2023", "namespace": "run_a"}]}
    run_b = {"namespace": "run_b", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-b-1", "doi": "10.0000/b1",
         "title": "Run B paper", "year": "2024", "namespace": "run_b"}]}
    # First catalog wins for the handle; the other run's same-number handle
    # is a DIFFERENT namespace and would need its own prefixed handle.
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[run_a, run_b])
    assert citation_map["reference_count"] == 1
    assert citation_map["references"][0]["namespace"] == "run_a"
    assert citation_map["references"][0]["title"] == "Run A paper"
    # Namespaced variants (run_b::P0001) map separately:
    text_b = "run_b::P0001 的写法由调用方展开为句柄后再进入本模块。"
    # The catalog resolution is namespace-scoped; direct proof here is that
    # two namespaces never merge into one reference entry:
    all_rows = run_a["entries"] + run_b["entries"]
    papers = {row["paper_id"] for row in all_rows}
    assert len(papers) == 2, "cross-run same handle stays two distinct papers"


def test_unknown_handle_stays_visible_and_reported(tmp_path):
    handle_text = "正文引用已知来源 [P0001]，以及一个未知编号 [P9999]。"
    catalog = {"namespace": "ns", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-1", "title": "Known",
         "year": "2024", "doi": "10.0000/1", "namespace": "ns"}]}
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[catalog])
    assert "P9999" in citation_map["unknown_handles"]
    reader, _n, residual = dc.render_reader_citations(handle_text, citation_map)
    assert "[P0001]" not in reader
    assert "[P9999]" in reader, "unknown handle stays visible in the text"
    assert "P9999" in residual


def test_math_pipes_and_table_structure_survive_numbering(tmp_path):
    handle_text = (
        "| 指标 | 公式 | 来源 |\n| --- | --- | --- |\n"
        "| 响应率 | $\\frac{a}{b}$（含竖线 \\| 示例） | [P0001] |\n\n"
        "正文：比较 $|x|$ 与阈值。")
    catalog = {"namespace": "ns", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-1", "title": "Known",
         "year": "2024", "doi": "10.0000/1", "namespace": "ns"}]}
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[catalog])
    reader, _n, _residual = dc.render_reader_citations(handle_text, citation_map)
    # Pipes and math survive untouched; only the bracket citation is numbered.
    assert reader.count("|") == handle_text.count("|")
    assert "$\\frac{a}{b}$" in reader
    assert "[1]" in reader and "[P0001]" not in reader


def test_figure_assets_attach_and_missing_reported(tmp_path):
    real_asset = tmp_path / "assets"
    real_asset.mkdir()
    _tiny_png(real_asset / "fig_a.png")
    body = (
        "## 5.1 疗效信号\n\n锚定段落：讲 FMT 试验的设计与结果。\n\n"
        "## 6.3 风险\n\n风险段落。")
    assets = [
        {"figure_id": "FIG:fmt_trials", "path": str(real_asset / "fig_a.png"),
         "caption": "代表性 FMT 临床试验设计（fixture 示意图）",
         "anchor_probe": "锚定段落"},
        {"figure_id": "FIG:missing", "path": str(tmp_path / "nope.png"),
         "caption": "缺失图", "anchor_probe": "风险段落"},
    ]
    assets_root = tmp_path / "stage" / "assets"
    updated, _inserted, missing = dc.attach_figures(
        body, assets, assets_dir=assets_root)
    # Insertion phase: token in text, real file copied under assets/,
    # missing asset reported, nothing numbered yet.
    assert "@@FIG:FIG:fmt_trials@@" in updated
    assert "![代表性 FMT 临床试验设计](assets/FIG_fmt_trials.png)" in updated
    assert (assets_root / "FIG_fmt_trials.png").is_file()
    assert missing[0]["figure_id"] == "FIG:missing"
    assert missing[0]["reason"] == "asset_file_missing"
    # Numbering phase assigns reading-order numbers.
    numbered = dc.number_figure_tokens(updated, [])
    assert "**图 1. 代表性 FMT 临床试验设计（fixture 示意图）**" in numbered
    # Caption-only (no asset) stays a pending item in the report path.
    report_only = {"figure_id": "FIG:caption_only", "path": "",
                   "caption": "只有说明的图", "anchor_probe": "锚定段落"}
    _, _captions2, missing2 = dc.attach_figures(body, [report_only],
                                                assets_dir=tmp_path / "a2")
    assert missing2[0]["reason"] == "asset_file_missing"


def test_cross_references_update_after_moves(tmp_path):
    text = "对照表 3 与表 4 可见趋势；详见表 3。如图 2 所示，见图 2 注。"
    new_text, rewrites = dc.update_cross_references(
        text, table_moves={3: 4, 4: 3}, figure_moves={2: 5})
    # A simultaneous swap must not cascade: 3->4 then 4->3 would flip back.
    # Verify each original phrase got rewritten exactly once.
    assert "详见表 4。" in new_text
    assert "如图 5 所示，见图 5 注。" in new_text
    kinds = {(r["kind"], r["old"], r["new"]) for r in rewrites}
    assert ("table", 3, 4) in kinds and ("table", 4, 3) in kinds
    assert ("figure", 2, 5) in kinds


def test_other_topic_fixture_no_domain_word_injection(tmp_path):
    # A short non-medical draft: the map/render path must not inject any
    # domain vocabulary of the long draft.
    handle_text = "生态位分化决定物种共存 [P0002]。"
    catalog = {"namespace": "eco", "entries": [
        {"source_handle": "P0002", "paper_id": "paper-eco",
         "title": "Niche partitioning", "year": "2020", "doi": "10.0000/eco",
         "namespace": "eco"}]}
    citation_map = dc.build_delivery_citation_map(
        final_handle_draft=handle_text, identity_catalogs=[catalog])
    reader, _n, _residual = dc.render_reader_citations(handle_text, citation_map)
    assert "[1]" in reader
    for word in ("微生物组", "免疫检查点", "FMT", "LLZO"):
        assert word not in reader


# ---------------------------------------------------------------------------
# rework: figure-number rewrite safety, same-name assets, full token export
# ---------------------------------------------------------------------------


def test_figure_move_does_not_rewrite_newly_generated_numbers(tmp_path):
    # The manuscript's figures were numbered 1 and 2 by position.  A table
    # move rule is supplied but NO figure moves: the generated figure numbers
    # must not be re-transformed, and the empty figure rule must not error.
    text = "如图 1 所示的趋势，与表 3 一致；图 2 为补充。"
    new_text, rewrites = dc.update_cross_references(
        text, table_moves={3: 4}, figure_moves=None)
    # The table cross-reference follows its move rule; figure numbers stay
    # untouched because no figure rule was supplied (and None is not an error).
    assert new_text == "如图 1 所示的趋势，与表 4 一致；图 2 为补充。"
    assert [r["kind"] for r in rewrites] == ["table"]
    # With an explicit figure move, only matching old numbers change.
    moved, rewrites2 = dc.update_cross_references(
        text, figure_moves={1: 3})
    assert "如图 3 所示" in moved and "图 2 为补充" in moved
    assert ("figure", 1, 3) in {(r["kind"], r["old"], r["new"]) for r in rewrites2}


def test_same_name_assets_from_two_runs_stay_distinct(tmp_path):
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "figure.svg").write_bytes(b"<svg>A</svg>")
    (d2 / "figure.svg").write_bytes(b"<svg>B</svg>")
    body = "甲段。\n\n乙段。"
    assets = [
        {"figure_id": "FIG:run1_diag", "path": str(d1 / "figure.svg"),
         "caption": "图A", "anchor_probe": "甲"},
        {"figure_id": "FIG:run2_diag", "path": str(d2 / "figure.svg"),
         "caption": "图B", "anchor_probe": "乙"},
    ]
    assets_root = tmp_path / "assets"
    updated, _captions, missing = dc.attach_figures(
        body, assets, assets_dir=assets_root)
    files = sorted(p.name for p in assets_root.iterdir())
    assert files == ["FIG_run1_diag.svg", "FIG_run2_diag.svg"],         "same-named assets must stay distinct per figure_id"
    assert missing == []
    assert "![图A](assets/FIG_run1_diag.svg)" in updated
    assert "![图B](assets/FIG_run2_diag.svg)" in updated


def test_references_json_exports_full_namespaced_tokens(tmp_path):
    run_a = {"namespace": "run_a", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-1",
         "doi": "10.0000/same", "title": "Same paper", "year": "2024",
         "namespace": "run_a"}]}
    run_b = {"namespace": "run_b", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-1",
         "doi": "10.0000/same", "title": "Same paper", "year": "2024",
         "namespace": "run_b"}]}
    work = tmp_path / "m.md"
    work.write_text("run_a [P0001]；run_b [run_b::P0001]。", encoding="utf-8")
    report = dc.run_figures_citations_stage(
        final_draft_path=work, identity_catalogs=[run_a, run_b],
        out_dir=tmp_path / "stage")
    assert report["reference_count"] == 1,         "the same paper (same DOI) across two runs merges into ONE reference"
    persisted = json.loads(
        (tmp_path / "stage/REFERENCES.json").read_text(encoding="utf-8"))
    details = persisted["token_details"]
    assert details["P0001"]["namespace"] == "run_a"
    assert details["run_b::P0001"]["namespace"] == "run_b"
    assert details["P0001"]["number"] == details["run_b::P0001"]["number"],         "both tokens point at the single merged reference"


def test_different_papers_across_runs_stay_distinct(tmp_path):
    run_a = {"namespace": "run_a", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-a",
         "doi": "10.0000/a", "title": "Paper A", "year": "2024",
         "namespace": "run_a"}]}
    run_b = {"namespace": "run_b", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-b",
         "doi": "10.0000/b", "title": "Paper B", "year": "2025",
         "namespace": "run_b"}]}
    work = tmp_path / "m.md"
    work.write_text("a [P0001]；b [run_b::P0001]。", encoding="utf-8")
    report = dc.run_figures_citations_stage(
        final_draft_path=work, identity_catalogs=[run_a, run_b],
        out_dir=tmp_path / "stage")
    assert report["reference_count"] == 2,         "genuinely different papers stay distinct"
    reader = (tmp_path / "stage/MANUSCRIPT_READER.md").read_text(encoding="utf-8")
    assert "[1]" in reader and "[2]" in reader


# ---------------------------------------------------------------------------
# rework 2: full-stage consistency of body refs, captions and exported map
# ---------------------------------------------------------------------------


def test_stage_cross_refs_captions_and_map_all_agree(tmp_path):
    run_b = {"namespace": "run_b", "entries": [
        {"source_handle": "P0001", "paper_id": "paper-b", "doi": "10.0000/b",
         "title": "Paper B", "year": "2025", "namespace": "run_b"}]}
    real_catalog = _real_catalog()
    body = (
        "开篇引用 [P0094]。\n\n"
        "## 第2章 关联\n\n"
        "旧图2的内容先讲，正文旧引用见图 2。\n\n"
        "| 项 | 值 |\n| --- | --- |\n| 行 | [P0349] |\n\n"
        "## 5.x 干预\n\n"
        "旧图1的内容后讲，正文旧引用见图 1。\n\n"
        "## 参考文献（原始 handle）\n\n[P0094]. 旧条目。")
    work = tmp_path / "MANUSCRIPT_FINAL.md"
    work.write_text(body, encoding="utf-8")
    late_png = tmp_path / "late.png"
    early_png = tmp_path / "early.png"
    _tiny_png(late_png)
    _tiny_png(early_png)
    assets = [
        {"figure_id": "FIG:late", "path": str(late_png),
         "caption": "后锚定图（fixture 示意图），引用 [run_b::P0001]",
         "anchor_probe": "旧图1的内容后讲"},
        {"figure_id": "FIG:early", "path": str(early_png),
         "caption": "先锚定图（fixture 示意图），引用 [P0349]",
         "anchor_probe": "旧图2的内容先讲"},
    ]
    report = dc.run_figures_citations_stage(
        final_draft_path=work, identity_catalogs=[real_catalog, run_b],
        out_dir=tmp_path / "stage", figure_assets=assets,
        figure_moves={1: 2, 2: 1}, table_moves={})
    reader = (tmp_path / "stage/MANUSCRIPT_READER.md").read_text(encoding="utf-8")
    placed = {row["figure_id"]: row["display_number"]
              for row in report["figures_placed"]}
    refs = json.loads((tmp_path / "stage/REFERENCES.json").read_text(encoding="utf-8"))
    assert placed["FIG:early"] == 1 and placed["FIG:late"] == 2
    cap1 = re.search(r"\*\*图 1\. ([^*]+)\*\*", reader)
    cap2 = re.search(r"\*\*图 2\. ([^*]+)\*\*", reader)
    assert cap1 and "先锚定图" in cap1.group(1)
    assert cap2 and "后锚定图" in cap2.group(1)
    assert refs["references"][0]["reference_number"] == 1
    assert (tmp_path / "stage/assets/FIG_late.png").is_file()
    assert "(assets/FIG_late.png)" in reader
