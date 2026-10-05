"""Work order 06 integration: the OFFICIAL entry runs the unified downstream.

Every full-chain scenario calls ``run_review_delivery`` — the exact function
the ``--delivery-start`` CLI branch dispatches to — with a delivery config;
one subprocess test exercises the real CLI argument wiring end to end.  No
test hand-chains the five stages to fake entry integration.  Model responses
come only from labeled fixtures or existing recordings; a socket-raising
guard proves in-process scenarios attempt no network.
"""

import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import review_delivery as delivery
from optomind_research.runtime.upgrade3.review_delivery import run_review_delivery

REPO = Path(__file__).resolve().parents[2]
REAL_MANIFEST = REPO / "outputs/unit_writing/20260927_astra_repair/DELIVERY_MANIFEST.json"
REAL_BATCH = REPO / "outputs/full_review_draft/20260927_run01"
PLAN_PACKET = REPO / ("outputs/review_v2_repair/05_real_test_20260929/"
                      "attempt02_production_path/UPDATED_WRITER_PACKET.json")
PLAN_RECORDINGS = REPO / "outputs/review_v2_delivery/plan/recordings_real_03.json"

EDIT_MARKER_1 = "〔集成测试编辑一已应用〕"
EDIT_MARKER_2 = "〔集成测试编辑二已应用〕"
FIG_ANCHOR = "【图锚点：机制概览示意】"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _no_socket(*args, **kwargs):
    raise AssertionError("network attempted during offline delivery")


@pytest.fixture(autouse=True)
def _guard_network(monkeypatch):
    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)


# ---------------------------------------------------------------------------
# shared builders: a small real-shaped history batch + a delivery config dir
# ---------------------------------------------------------------------------


def _unit_result(chapter_id: str, unit_id: str, body: str) -> dict:
    return {"chapter_id": chapter_id, "unit_id": unit_id, "complete": True,
            "finish_reason": "stop", "body_markdown": body}


def _history_batch(tmp_path: Path) -> Path:
    """Written-run batch: two units with handles, a figure anchor line and an
    old handle-form reference section."""
    arrangement = tmp_path / "arrangements" / "CH01.json"
    _write_json(arrangement, {
        "chapter_id": "CH01", "title": "材料与方法",
        "units": [
            {"unit_id": "CH01_U01", "focus": "原理",
             "paragraph_tasks": [{"paragraph_id": "CH01_U01_P01", "point": "p",
                                  "development": "d",
                                  "source_uses": [{"source_handle": "P0001",
                                                   "role": "主论据", "use": "u"}]}]},
            {"unit_id": "CH01_U02", "focus": "复用",
             "paragraph_tasks": [{"paragraph_id": "CH01_U02_P01", "point": "p2",
                                  "development": "d2",
                                  "source_uses": [{"source_handle": "P0002",
                                                   "role": "例证", "use": "u"}]}]},
        ],
    })
    manifest = tmp_path / "DELIVERY_MANIFEST.json"
    _write_json(manifest, {
        "review_title": "集成测试综述题名", "chapters": [
            {"chapter_id": "CH01", "arrangement_path": str(arrangement),
             "units": 2, "status": "arranged"}],
    })
    unit_dir = tmp_path / "units" / "CH01_U01"
    _write_json(unit_dir / "UNIT_RESULT.json",
                _unit_result("CH01", "CH01_U01",
                             f"第一章单元正文：机制解释含 [P0001]。\n\n{FIG_ANCHOR}"))
    reused = tmp_path / "elsewhere" / "CH01_U02.json"
    _write_json(reused, _unit_result("CH01", "CH01_U02",
                                     "第二章单元正文：负面结果保留 [P0002]。"))
    (tmp_path / "FRONT_MATTER.md").write_text(
        "# 集成测试综述题名\n\n## 摘要\n\n已有摘要正文。\n\n"
        "**关键词：** 旧关键词甲；旧关键词乙", encoding="utf-8")
    # BACK_MATTER carries the epilogue only — the assembler itself appends
    # the handle-form reference section (same shape as the real run).
    (tmp_path / "BACK_MATTER.md").write_text(
        "## 结语\n\n已有结语正文。\n", encoding="utf-8")
    jobs = [
        {"chapter_id": "CH01", "unit_id": "CH01_U01",
         "arrangement": str(arrangement), "output": str(unit_dir)},
        {"chapter_id": "CH01", "unit_id": "CH01_U02",
         "arrangement": str(arrangement), "reused_result": str(reused)},
    ]
    _write_json(tmp_path / "BATCH_JOBS.json", jobs)
    return manifest


def _catalogs() -> list[dict]:
    def row(handle: str, title: str) -> dict:
        return {"source_handle": handle, "handles": [handle],
                "paper_id": f"paper-{handle}", "doi": f"10.0000/{handle.lower()}",
                "title": title, "year": "2024", "namespace": "cfg_ns"}
    return [{"namespace": "cfg_ns",
             "entries": [row("P0001", "Paper A"), row("P0002", "Paper B"),
                         row("P0003", "Paper C")]}]


def _write_config_dir(tmp_path: Path, *, edit_fixture: dict | None = None,
                      edit_recordings: Path | None = None,
                      fb_fixture: dict | None = None,
                      extra_figure_assets: list[dict] | None = None) -> Path:
    """Config + labeled fixtures; paths inside the config are relative to it."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "FIG_cfg_demo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">'
        "<rect width=\"12\" height=\"12\"/></svg>", encoding="utf-8")
    figure_assets = [{
        "figure_id": "FIG:cfg_demo",
        "path": "FIG_cfg_demo.svg",
        "caption": "集成测试演示图（fixture）：机制概览示意 [P0003]。",
        "anchor_probe": FIG_ANCHOR,
    }] + (extra_figure_assets or [])
    _write_json(cfg_dir / "FIGURE_ASSETS.json", figure_assets)
    _write_json(cfg_dir / "IDENTITY_CATALOGS.json", _catalogs())
    config: dict = {
        "schema": "review_v2_delivery.config.v1",
        "language": "zh",
        "research_question": "集成测试研究问题：机制与干预证据如何分层",
        "chapter_roles": [{"chapter_id": "CH01", "title": "材料与方法",
                           "role": "methods"}],
        "identity_catalogs": {"path": "IDENTITY_CATALOGS.json"},
        "figure_assets": {"path": "FIGURE_ASSETS.json"},
        "table_moves": {},
        "figure_moves": {},
        "compile_pdf": False,
    }
    if edit_fixture is not None:
        _write_json(cfg_dir / "EDIT_FIXTURE.json", edit_fixture)
        config["text_edit"] = {"fixture": "EDIT_FIXTURE.json"}
    if edit_recordings is not None:
        config["text_edit"] = {"recordings": str(edit_recordings)}
    if fb_fixture is not None:
        _write_json(cfg_dir / "PARTS_FIXTURE.json", fb_fixture)
        config["front_back"] = {"fixture": "PARTS_FIXTURE.json"}
    config_path = cfg_dir / "DELIVERY_CONFIG.json"
    _write_json(config_path, config)
    return config_path


def _edit_fixture(*originals: str) -> dict:
    def marker_at(index: int) -> str:
        markers = (EDIT_MARKER_1, EDIT_MARKER_2)
        return markers[index] if index < len(markers) else \
            f"〔集成测试编辑{index + 1}已应用〕"

    return {
        "fixture": True,
        "note": "人工标注fixture：编辑建议锚定装配稿真实文本，非模型输出",
        "changes": [{"operation": "replace", "original_text": original,
                     "replacement_text": original + marker_at(index),
                     "reason": "fixture"}
                    for index, original in enumerate(originals)],
        "unresolved_questions": [],
    }


def _fb_fixture() -> dict:
    return {
        "fixture": True,
        "note": "人工标注fixture：首尾部件演示应用合同，非模型输出",
        "conclusion": {"conclusion":
                       "集成测试结语：边界与分层结论已在正文各章收束。"},
        "introduction": {"introduction":
                         "集成测试引言：说明综述范围与各章分工，与结语承诺一致。"},
        "abstract": {"title": "集成测试综述题名",
                     "abstract": "集成测试摘要：正文证据的综合判断与限制。",
                     "keywords": ["集成关键词甲", "集成关键词乙"]},
    }


def _default_config(tmp_path: Path, *, edit_fixture: dict | None = None,
                    **kwargs) -> Path:
    return _write_config_dir(
        tmp_path,
        edit_fixture=edit_fixture if edit_fixture is not None
        else _edit_fixture("机制解释含 [P0001]。"),
        fb_fixture=_fb_fixture(), **kwargs)


def _read(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _common_content_asserts(published: str, refs: dict) -> None:
    """Content-level invariants of the published manuscript."""
    numbers = sorted(int(n) for n in re.findall(r"^\[(\d+)\]", published, re.M))
    assert numbers and numbers == list(range(1, len(numbers) + 1)), numbers
    assert len(re.findall(r"^#{1,3}\s+参考文献", published, re.M)) == 1
    assert published.count("**关键词") == 1
    assert published.count("**图 ") == 1 and "@@FIG:" not in published
    assert not re.search(r"\[P\d{3,}\]", published)
    for handle, number in refs["handle_to_reference"].items():
        assert f"[{number}]" in published, f"missing citation [{number}] ({handle})"


# ---------------------------------------------------------------------------
# entry: assembly_only vs full downstream
# ---------------------------------------------------------------------------


def test_entry_without_config_reports_assembly_only(tmp_path):
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path)
    assert report["delivery_mode"] == "assembly_only"
    assert "stages" not in report
    assert (out / "assembled" / "REVIEW_DRAFT_HANDLES.md").is_file()
    assert not (out / "05_publication").exists()


def test_entry_config_runs_unified_downstream_history(tmp_path):
    """history start through the OFFICIAL entry: 02→03→04→05 all production
    consumers, each fed by the previous stage's actual returned artifact."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    config = _default_config(tmp_path)
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path,
                                 config_path=config)
    assert report["delivery_mode"] == "full_downstream"
    stages = report["stages"]
    assert set(stages) == {"02_text_edit", "03_front_back",
                           "04_figures_citations", "05_publication"}
    assert report["downstream_status"] == "complete"

    # 02 consumed the ASSEMBLED HANDLE draft (source identity preserved for 04).
    assert stages["02_text_edit"]["source_draft"].endswith(
        "REVIEW_DRAFT_HANDLES.md")
    assert stages["02_text_edit"]["status"] == "edited"
    # Each stage consumed the previous stage's real returned artifact.
    assert stages["03_front_back"]["source_draft"] == \
        stages["02_text_edit"]["edited_draft"]
    assert stages["04_figures_citations"]["handles_draft"] != "" and \
        Path(stages["04_figures_citations"]["reader_draft"]).is_file()
    assert stages["05_publication"]["published_markdown"] != ""

    published = _read(stages["05_publication"]["published_markdown"])
    # The edit result and the front/back results are IN the published text.
    assert EDIT_MARKER_1 in published
    assert "集成测试结语" in published and "集成测试引言" in published
    assert "集成测试摘要" in published and "集成关键词甲" in published
    assert "第二章单元正文：负面结果保留" in published  # untouched content kept
    refs = json.loads(_read(stages["04_figures_citations"]["references_path"]))
    # The caption-only citation [P0003] reached the numbered references.
    assert "P0003" in refs["handle_to_reference"]
    assert "Paper C" in published
    _common_content_asserts(published, refs)
    for stage in stages.values():
        assert stage["external_requests"] == 0


def test_entry_config_error_refuses_silent_assembly_only(tmp_path):
    """A config pointing at a missing fixture must surface as pending — never
    quietly degrade to assembly-only while claiming a full chain."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    config = _write_config_dir(tmp_path, edit_fixture=None, fb_fixture=None)
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path,
                                 config_path=config)
    assert report["delivery_mode"] == "full_downstream"
    assert report["downstream_status"] == "pending"
    stage = report["stages"]["02_text_edit"]
    assert stage["status"] == "pending"
    assert "no_fixture_or_recordings" in json.dumps(stage)
    assert "03_front_back" not in report["stages"]
    assert not (out / "05_publication").exists()


# ---------------------------------------------------------------------------
# official CLI subprocess: real history data end to end
# ---------------------------------------------------------------------------


def test_history_full_chain_via_cli_subprocess(tmp_path):
    """The real CLI invocation (--delivery-start history --delivery-config)
    runs the real 23-unit manuscript through the unified downstream."""
    assert REAL_MANIFEST.is_file() and REAL_BATCH.is_dir()
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    draft_text = _read(REAL_BATCH / "REVIEW_DRAFT_HANDLES.md")
    body = draft_text.split("## 参考文献")[0]
    # Anchor on a real body line — 03 owns the keyword line, so an edit
    # there would be superseded by the front/back application contract.
    line = next(l for l in body.splitlines()
                if 60 < len(l) < 240
                and not l.startswith(("#", ">", "**", "![")))
    original = line[:60]
    while draft_text.count(original) != 1:
        original = line[:len(original) + 20]
    _write_json(cfg_dir / "EDIT_FIXTURE.json", _edit_fixture(original))
    (cfg_dir / "FIG_cli_demo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">'
        "<rect width=\"12\" height=\"12\"/></svg>", encoding="utf-8")
    # Caption cites a catalog handle the body never cites (P0576-class): the
    # caption-only citation must still reach the numbered references.
    refs_real = json.loads(_read(REAL_BATCH / "REFERENCES.json"))["references"]
    unused = [h for r in refs_real for h in r["handles"]
              if f"[{h}]" not in body]
    assert unused, "expected at least one catalog handle unused in the body"
    _write_json(cfg_dir / "FIGURE_ASSETS.json", [{
        "figure_id": "FIG:cli_demo", "path": "FIG_cli_demo.svg",
        "caption": f"CLI集成测试演示图（fixture）：证据地图示意 [{unused[0]}]。",
        "anchor_probe": "## 第1章",
    }])
    rows = [{"source_handle": h, "handles": r["handles"],
             "paper_id": r["paper_id"] or h, "doi": r.get("doi") or "",
             "title": r.get("title") or "", "year": str(r.get("year") or ""),
             "namespace": "run_20260927"}
            for r in refs_real for h in r["handles"]]
    _write_json(cfg_dir / "IDENTITY_CATALOGS.json",
                [{"namespace": "run_20260927", "entries": rows}])
    _write_json(cfg_dir / "PARTS_FIXTURE.json", _fb_fixture())
    config = cfg_dir / "DELIVERY_CONFIG.json"
    _write_json(config, {
        "schema": "review_v2_delivery.config.v1", "language": "zh",
        "research_question": "集成测试研究问题（CLI subprocess）",
        "chapter_roles": [{"chapter_id": "CH01", "title": "引言",
                           "role": "introduction"}],
        "text_edit": {"fixture": "EDIT_FIXTURE.json"},
        "front_back": {"fixture": "PARTS_FIXTURE.json"},
        "identity_catalogs": {"path": "IDENTITY_CATALOGS.json"},
        "figure_assets": {"path": "FIGURE_ASSETS.json"},
        "table_moves": {}, "figure_moves": {}, "compile_pdf": False,
    })

    out_root = tmp_path / "delivery"
    proc = subprocess.run(
        [sys.executable, "run_review_harness.py", "--delivery-start", "history",
         "--delivery-manifest", str(REAL_MANIFEST),
         "--delivery-batch-root", str(REAL_BATCH),
         "--delivery-out", str(out_root), "--delivery-config", str(config)],
        cwd=REPO, capture_output=True, text=True, timeout=600,
        encoding="utf-8", errors="replace")
    assert proc.returncode == 0, (proc.stdout or "")[-2000:] + \
        (proc.stderr or "")[-2000:]

    # The CLI roots the run at <delivery-out>/<start>.
    out = out_root / "history"
    final = json.loads(_read(out / "DELIVERY_REPORT.json"))
    assert final["delivery_mode"] == "full_downstream"
    assert final["downstream_status"] == "complete"
    assert final["original_units"] == final["assembled_units"]
    stages = final["stages"]
    assert set(stages) == {"02_text_edit", "03_front_back",
                           "04_figures_citations", "05_publication"}
    published = _read(stages["05_publication"]["published_markdown"])
    assert original + "〔集成测试编辑一已应用〕" in published
    assert "集成测试结语" in published and "集成测试摘要" in published
    refs = json.loads(_read(stages["04_figures_citations"]["references_path"]))
    # Caption-only citation exists in the final numbered references.
    assert unused[0] in refs["handle_to_reference"]
    _common_content_asserts(published, refs)
    assert all(s["external_requests"] == 0 for s in stages.values())


# ---------------------------------------------------------------------------
# plan start through the official entry (restricted delivery)
# ---------------------------------------------------------------------------


def test_plan_restricted_chain_via_entry(tmp_path):
    """plan start keeps the missing unit honest through the WHOLE chain: the
    final report still says original=4, assembled=3, C6_U04 missing."""
    assert PLAN_PACKET.is_file() and PLAN_RECORDINGS.is_file()
    packet = json.loads(_read(PLAN_PACKET))
    recordings = json.loads(_read(PLAN_RECORDINGS))["recordings"]
    body = json.loads(recordings["writer:C6_U01"]["response"]["content"])
    body_text = body["body_markdown"]
    candidates = [l for l in body_text.splitlines()
                  if 40 < len(l) < 240 and not l.startswith(("#", ">", "|"))]
    # Prefer a handle-free line: the published text replaces [Pxxxx] with
    # numbers, so a handle-bearing anchor cannot be asserted verbatim there.
    line = next((l for l in candidates if "[P" not in l), candidates[0])
    original = line[:50]
    while body_text.count(original) != 1:
        original = line[:len(original) + 20]

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    _write_json(cfg_dir / "EDIT_FIXTURE.json", _edit_fixture(original))
    _write_json(cfg_dir / "PARTS_FIXTURE.json", _fb_fixture())
    rows = [{"source_handle": m["source_handle"], "handles": [m["source_handle"]],
             "paper_id": m.get("paper_id") or m["source_handle"],
             "doi": m.get("doi") or "", "title": m.get("title") or "",
             "year": str(m.get("year") or ""), "namespace": "plan_c6"}
            for m in packet["source_materials"]]
    _write_json(cfg_dir / "IDENTITY_CATALOGS.json",
                [{"namespace": "plan_c6", "entries": rows}])
    config = cfg_dir / "DELIVERY_CONFIG.json"
    _write_json(config, {
        "schema": "review_v2_delivery.config.v1", "language": "zh",
        "research_question": str(packet.get("research_question") or "")[:200],
        "chapter_roles": [{"chapter_id": "C6", "title": "第六章", "role": ""}],
        "text_edit": {"fixture": "EDIT_FIXTURE.json"},
        "front_back": {"fixture": "PARTS_FIXTURE.json"},
        "identity_catalogs": {"path": "IDENTITY_CATALOGS.json"},
        "table_moves": {}, "figure_moves": {}, "compile_pdf": False,
    })

    out = tmp_path / "plan"
    report = run_review_delivery(start="plan", out_dir=out,
                                 packet_path=PLAN_PACKET,
                                 recordings_path=PLAN_RECORDINGS,
                                 config_path=config)
    # Restricted-delivery accounting survives the downstream stages.
    assert report["original_units"] == 4
    assert report["assembled_units"] == 3
    assert report["missing_units"] == ["C6_U04"]
    assert report["assembly"]["missing_units"] == ["C6_U04"]
    assert report["delivery_mode"] == "full_downstream"
    assert report["downstream_status"] == "complete"
    published = _read(report["stages"]["05_publication"]["published_markdown"])
    if "[P" in original:  # handle-bearing anchors read as numbers in publish
        assert "〔集成测试编辑一已应用〕" in published
    else:
        assert original + "〔集成测试编辑一已应用〕" in published
    assert "集成测试结语" in published and "集成测试摘要" in published
    for stage in report["stages"].values():
        assert stage["external_requests"] == 0


# ---------------------------------------------------------------------------
# recovery / propagation / no_change / missing recording
# ---------------------------------------------------------------------------


def test_same_input_rerun_no_duplication(tmp_path):
    """Rerunning the SAME entry command into the SAME out dir must not stack
    introductions, figures or reference sections."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    config = _default_config(tmp_path)
    run1 = run_review_delivery(start="history", out_dir=out,
                               manifest_path=manifest, batch_root=tmp_path,
                               config_path=config)
    pub1 = _read(run1["stages"]["05_publication"]["published_markdown"])
    run2 = run_review_delivery(start="history", out_dir=out,
                               manifest_path=manifest, batch_root=tmp_path,
                               config_path=config)
    pub2 = _read(run2["stages"]["05_publication"]["published_markdown"])
    assert pub1 == pub2
    assert pub1.count("集成测试引言") == 1
    assert pub1.count("**图 ") == 1
    assert len(re.findall(r"^#{1,3}\s+参考文献", pub1, re.M)) == 1
    for stage in run2["stages"].values():
        assert stage["external_requests"] == 0


def test_partial_edit_propagation_reads_fresh_body(tmp_path):
    """A changed edit fixture reaches the published text, and the front/back
    messages are built from the NEW body (not a stale summary)."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    config1 = _write_config_dir(
        tmp_path, edit_fixture=_edit_fixture("机制解释含 [P0001]。"),
        fb_fixture=_fb_fixture())
    run_review_delivery(start="history", out_dir=out,
                        manifest_path=manifest, batch_root=tmp_path,
                        config_path=config1)
    config2 = _write_config_dir(
        tmp_path, edit_fixture=_edit_fixture("机制解释含 [P0001]。",
                                             "负面结果保留 [P0002]。"),
        fb_fixture=_fb_fixture())
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path,
                                 config_path=config2)
    published = _read(report["stages"]["05_publication"]["published_markdown"])
    # Both edits reached the publication; the untouched sentences remain
    # (handles read as numbers there, so assert on the handle-free fragments).
    assert EDIT_MARKER_1 in published and EDIT_MARKER_2 in published
    assert "机制解释含" in published and "负面结果保留" in published
    # The abstract-stage message carried the NEW body (edit 2 visible in it).
    abstract_msg = _read(out / "03_front_back" / "messages"
                         / "front_back_abstract_messages.json")
    assert EDIT_MARKER_2 in abstract_msg


def test_no_change_still_runs_full_downstream(tmp_path):
    """An empty change list is a legitimate no_change: the body still flows
    through 03/04/05 — nothing is lost and nothing is skipped."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    config = _write_config_dir(
        tmp_path, edit_fixture={"fixture": True, "note": "无修改fixture",
                                "changes": [], "unresolved_questions": []},
        fb_fixture=_fb_fixture())
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path,
                                 config_path=config)
    assert report["stages"]["02_text_edit"]["status"] == "no_change"
    assert report["downstream_status"] == "complete"
    published = _read(report["stages"]["05_publication"]["published_markdown"])
    assert "机制解释含" in published  # body preserved verbatim
    assert "集成测试结语" in published
    refs = json.loads(_read(report["stages"]["04_figures_citations"]
                            ["references_path"]))
    _common_content_asserts(published, refs)


def test_missing_recording_names_stage_and_stops(tmp_path):
    """A recording missing the text_edit key halts at 02 with the stage NAMED;
    no downstream stage runs and no live call is attempted."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    empty = tmp_path / "empty_recordings.json"
    _write_json(empty, {"recordings": {}})
    config = _write_config_dir(tmp_path, edit_recordings=empty,
                               fb_fixture=_fb_fixture())
    report = run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest, batch_root=tmp_path,
                                 config_path=config)
    assert report["downstream_status"] == "pending"
    stage = report["stages"]["02_text_edit"]
    assert stage["status"] == "pending"
    assert stage["pending"][0]["step"] == "text_edit:full"
    assert stage["pending"][0]["status"] == "pending_missing_recording"
    assert "03_front_back" not in report["stages"]
    assert not (out / "05_publication").exists()


# ---------------------------------------------------------------------------
# 05 publication consumes the 04 artifacts ONLY (no re-doing 04)
# ---------------------------------------------------------------------------


def _citation_fixtures(tmp_path: Path) -> tuple[Path, Path, list[dict], list[dict]]:
    """A handle draft with an anchor + catalogs + a figure asset file."""
    cfg_dir = tmp_path / "cit"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "FIG_pub.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">'
        "<rect width=\"12\" height=\"12\"/></svg>", encoding="utf-8")
    draft = tmp_path / "HANDLES.md"
    draft.write_text(
        "# 出版测试题名\n\n## 摘要\n\n出版测试摘要正文。\n\n"
        "**关键词：** 出版关键词甲；出版关键词乙\n\n"
        "## 第1章 正文\n\n"
        f"正文段落引用 [P0001]，第二处 [P0002]。\n\n{FIG_ANCHOR}\n\n"
        "## 结语\n\n结语正文。\n\n"
        "## 参考文献（原始 handle）\n\n- [P0001] Paper A\n- [P0002] Paper B\n",
        encoding="utf-8")
    catalogs = _catalogs()
    figure_assets = [{"figure_id": "FIG:pub", "path": str(cfg_dir / "FIG_pub.svg"),
                      "caption": "出版测试演示图（fixture）：概览 [P0003]。",
                      "anchor_probe": FIG_ANCHOR}]
    return draft, cfg_dir, catalogs, figure_assets


def test_publication_consumes_04_without_redoing_it(tmp_path):
    """Publication copies the 04 reader draft as-is: same body, same numbering,
    ONE reference section, ONE figure block; assets resolve for Markdown AND
    TeX; no citation map rebuild, no re-attachment."""
    from optomind_research.runtime.upgrade3.delivery_citations import (
        run_figures_citations_stage,
    )
    draft, _cfg_dir, catalogs, figure_assets = _citation_fixtures(tmp_path)
    out04 = tmp_path / "04_cit"
    r4 = run_figures_citations_stage(
        final_draft_path=draft, identity_catalogs=catalogs, out_dir=out04,
        figure_assets=figure_assets)
    assert r4["status"] == "complete"
    reader_text = _read(r4["reader_draft"])

    out05 = tmp_path / "05_pub"
    report = delivery.run_publication_delivery(
        reader_draft_path=Path(r4["reader_draft"]),
        references_path=Path(r4["references_path"]),
        figure_map_path=Path(r4["mapping_path"]),
        assets_dir=Path(r4["assets_dir"]),
        out_dir=out05, compile_pdf=False)
    assert report["status"] == "complete", report
    published = _read(report["published_markdown"])
    # Body preserved EXACTLY (the numbered draft is the delivery content).
    assert published.rstrip() + "\n" == reader_text.rstrip() + "\n" or \
        published == reader_text, "publication must not rewrite the 04 body"
    # No re-doing of 04: numbering identical to the 04 catalog; single
    # reference section; figure block not duplicated.
    refs = json.loads(_read(r4["references_path"]))
    _common_content_asserts(published, refs)
    for handle, number in refs["handle_to_reference"].items():
        assert reader_text.count(f"[{number}]") == published.count(f"[{number}]")
    # Assets resolve next to the Markdown AND inside the TeX directory.
    assert (out05 / "assets").is_dir() and any((out05 / "assets").iterdir())
    assert (out05 / "tex" / "assets").is_dir() \
        and any((out05 / "tex" / "assets").iterdir())
    assert not (out05 / "publication.pdf").exists()  # compile_pdf=False
    assert report["model_calls"] == 0 and report["external_requests"] == 0


def test_publication_metadata_uses_actual_content(tmp_path):
    """Title/abstract/keywords come from the real manuscript; the abstract
    never contains the keyword line; nothing is replaced by placeholders."""
    from optomind_research.runtime.upgrade3.delivery_citations import (
        run_figures_citations_stage,
    )
    draft, _cfg_dir, catalogs, figure_assets = _citation_fixtures(tmp_path)
    r4 = run_figures_citations_stage(
        final_draft_path=draft, identity_catalogs=catalogs,
        out_dir=tmp_path / "04_cit", figure_assets=figure_assets)
    report = delivery.run_publication_delivery(
        reader_draft_path=Path(r4["reader_draft"]),
        references_path=Path(r4["references_path"]),
        figure_map_path=Path(r4["mapping_path"]),
        assets_dir=Path(r4["assets_dir"]),
        out_dir=tmp_path / "05_pub", compile_pdf=False)
    metadata = report["publication_metadata"]
    assert metadata["title"] == "出版测试题名"
    assert "出版测试摘要正文" in metadata["abstract"]
    assert "关键词" not in metadata["abstract"]
    assert metadata["keywords"] == ["出版关键词甲", "出版关键词乙"]
    assert "placeholder" not in json.dumps(metadata).lower()
    # No fabricated authorship.
    assert not metadata.get("authors")


def test_publication_reports_failure_not_complete(tmp_path):
    """A malformed 04 input (reference section broken) fails LOUDLY: no
    published manuscript is written and the status is not complete."""
    from optomind_research.runtime.upgrade3.delivery_citations import (
        run_figures_citations_stage,
    )
    draft, _cfg_dir, catalogs, figure_assets = _citation_fixtures(tmp_path)
    r4 = run_figures_citations_stage(
        final_draft_path=draft, identity_catalogs=catalogs,
        out_dir=tmp_path / "04_cit", figure_assets=figure_assets)
    reader_text = _read(r4["reader_draft"])
    broken = tmp_path / "BROKEN_READER.md"
    broken.write_text(reader_text + "\n\n## 参考文献\n\n[1] duplicate section\n",
                      encoding="utf-8")
    report = delivery.run_publication_delivery(
        reader_draft_path=broken,
        references_path=Path(r4["references_path"]),
        out_dir=tmp_path / "05_pub", compile_pdf=False)
    assert report["status"] == "failed"
    assert report["verification"]["reference_sections_count"] == 2
    assert not (tmp_path / "05_pub" / "MANUSCRIPT_PUBLISHED.md").exists()


# ---------------------------------------------------------------------------
# assembly-level behaviour (kept from the previous round: still valid)
# ---------------------------------------------------------------------------


def test_same_out_dir_rerun_preserves_content(tmp_path):
    """Rerunning assembly with the same inputs produces the same output."""
    manifest = _history_batch(tmp_path)
    out = tmp_path / "delivery"
    r1 = delivery.run_history_delivery(
        manifest_path=manifest, batch_root=tmp_path, out_dir=out)
    draft_1 = (out / "assembled/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    r2 = delivery.run_history_delivery(
        manifest_path=manifest, batch_root=tmp_path, out_dir=out)
    draft_2 = (out / "assembled/REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert draft_1 == draft_2, "same inputs must produce identical output"
    assert r1["assembly"]["status"] == r2["assembly"]["status"]


def test_missing_unit_causes_assembly_refusal(tmp_path):
    """A missing unit result causes the assembler to refuse; not silent."""
    from scripts.upgrade3 import full_review_draft as draft
    arr = tmp_path / "arr.json"
    arr.write_text(json.dumps({
        "chapter_id": "CH01", "title": "T",
        "units": [{"unit_id": "U1"}, {"unit_id": "U2"}],
    }), encoding="utf-8")
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({
        "review_title": "T", "chapters": [
            {"chapter_id": "CH01", "arrangement_path": str(arr)}]}),
        encoding="utf-8")
    jobs = []
    for uid in ("U1",):  # Only U1 has a result; U2 is missing.
        r = tmp_path / f"{uid}.json"
        r.write_text(json.dumps(
            {"chapter_id": "CH01", "unit_id": uid, "complete": True,
             "body_markdown": "body"}), encoding="utf-8")
        jobs.append({"chapter_id": "CH01", "unit_id": uid,
                     "arrangement": str(arr), "reused_result": str(r)})
    (tmp_path / "BATCH_JOBS.json").write_text(json.dumps(jobs), encoding="utf-8")
    result = draft.main(["--manifest", str(manifest), "--batch-root", str(tmp_path),
                         "--output-root", str(tmp_path / "out")])
    assert result != 0, "missing unit must cause non-zero exit"
