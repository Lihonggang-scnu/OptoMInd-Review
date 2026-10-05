"""Work order review_v2_delivery/01: unified offline delivery entry.

Two start points through the real production constructors and the real
assembler; replayed/labeled fixtures only.  A socket-raising guard proves no
network is attempted.  Recorded responses here are structural fixtures; the
real recorded responses from the 03 paid run are exercised by the manual
command documented in records/01.md.
"""

import json
import socket
import sys
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import review_delivery as delivery


OWNER_A_DEV = "负责人原任务A：在 12.5 MPa、80°C 工况下测得首圈 126 mAh/g（理论容量的 70%）。"
OWNER_B_DEV = "负责人原任务B：对称电池压力对比实验（160/107）不可与长循环拼接。"


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _unit_result(chapter_id: str, unit_id: str, body: str, **extra) -> dict:
    row = {
        "chapter_id": chapter_id, "unit_id": unit_id, "complete": True,
        "finish_reason": "stop", "body_markdown": body,
    }
    row.update(extra)
    return row


def _history_fixture(tmp_path: Path):
    """A written-run batch: one normal unit, one cross-directory reused unit."""
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
                                  "source_uses": [{"source_handle": "P0001",
                                                   "role": "例证", "use": "u"}]}]},
        ],
        "source_catalog": {"P0001": {"paper_id": "doi:10.0000/a", "title": "Paper A",
                                     "year": "2024", "doi": "10.0000/a"}},
    })
    manifest = tmp_path / "DELIVERY_MANIFEST.json"
    _write_json(manifest, {
        "review_title": "综述标题", "chapters": [
            {"chapter_id": "CH01", "arrangement_path": str(arrangement),
             "units": 2, "status": "arranged"}],
    })
    local = tmp_path / "units" / "CH01_U01" / "UNIT_RESULT.json"
    _write_json(local, _unit_result("CH01", "CH01_U01",
                                    "普通单元正文：机制解释含 [P0001]。"))
    other_root = tmp_path / "elsewhere"
    reused = other_root / "CH01_U02.json"
    _write_json(reused, _unit_result("CH01", "CH01_U02",
                                     "跨目录复用单元正文：负面结果保留 [P0001]。"))
    (tmp_path / "FRONT_MATTER.md").write_text(
        "# 综述标题\n\n## 摘要\n\n已有摘要正文。", encoding="utf-8")
    (tmp_path / "BACK_MATTER.md").write_text("## 结语\n\n已有结语正文。", encoding="utf-8")
    _write_json(tmp_path / "TITLE_OVERRIDES.json",
                {"titles": {"CH01_U02": "复用单元标题"}})
    _write_json(tmp_path / "TABLE_TITLES.json",
                {"titles": {1: "对比表标题"}})
    jobs = [
        {"chapter_id": "CH01", "unit_id": "CH01_U01",
         "arrangement": str(arrangement), "output": str(tmp_path / "units" / "CH01_U01")},
        {"chapter_id": "CH01", "unit_id": "CH01_U02",
         "arrangement": str(arrangement), "reused_result": str(reused)},
    ]
    _write_json(tmp_path / "BATCH_JOBS.json", jobs)
    return manifest


def _writer_packet(tmp_path: Path) -> Path:
    packet = {
        "chapter": {"chapter_id": "C1", "title": "Chapter"},
        "research_question": "fixture 研究问题：对象与工况如何决定结论",
        "chapter_plan": {"thesis": "owner thesis", "units": [
            {
                "unit_id": "C1_U01",
                "substantive_point": "unit point",
                "paragraph_briefs": [
                    {"point": "任务A", "development": OWNER_A_DEV, "source_handles": ["P0001"]},
                    {"point": "任务B", "development": OWNER_B_DEV, "source_handles": ["P0001"]},
                ],
            },
            {
                "unit_id": "C1_U02",
                "substantive_point": "second unit point",
                "paragraph_briefs": [
                    {"point": "任务C", "development": "负责人原任务C：背景与限制。",
                     "source_handles": ["P0001"]},
                ],
            },
        ]},
        "source_materials": [
            {"source_handle": handle, "paper_id": f"paper-{handle}",
             "study_summary_A": {"finding": f"{handle} 的实际材料内容"}}
            for handle in ("P0001",)
        ],
        "source_identity_map": {"P0001": {"paper_id": "paper-P0001"}},
    }
    path = tmp_path / "WRITER_PACKET.json"
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    return path


def _plan_recordings(tmp_path: Path, *, include_arrangement=True,
                     writer_bodies=None) -> Path:
    arrangement_response = {
        "chapter_id": "C1", "chapter_argument": "结构编排",
        "units": [
            {
                "unit_id": "C1_U01", "focus": "焦点",
                "paragraph_tasks": [
                    {"paragraph_id": "C1_U01_P01", "source_briefs": ["C1_U01_P01"],
                     "source_uses": [{"source_handle": "P0001", "role": "主论据", "use": "u"}]},
                    {"paragraph_id": "C1_U01_P02", "source_briefs": ["C1_U01_P02"],
                     "source_uses": [{"source_handle": "P0001", "role": "局限", "use": "u"}]},
                ],
            },
            {
                "unit_id": "C1_U02", "focus": "第二单元",
                "paragraph_tasks": [
                    {"paragraph_id": "C1_U02_P01", "source_briefs": ["C1_U02_P01"],
                     "source_uses": [{"source_handle": "P0001", "role": "背景", "use": "u"}]},
                ],
            },
        ],
        "unused_sources": [],
    }
    recordings: dict[str, dict] = {}
    if include_arrangement:
        recordings["arrangement:C1"] = {
            "model": "fixture-arranger",
            "response": {"content": json.dumps(arrangement_response, ensure_ascii=False),
                         "finish_reason": "stop", "complete": True, "usage": {}},
        }
    recordings["writer:C1_U01"] = {
        "model": "fixture-writer",
        "response": {"content": json.dumps({
            "body_markdown": "单元正文：机制段保留 80°C 与 126 mAh/g（理论容量 70%）[P0001]；\n\n"
                             "负面结果段说明 160/107 属另一实验 [P0001]。",
            "issues": []}, ensure_ascii=False),
            "finish_reason": "stop", "complete": True, "usage": {}},
    }
    recordings["writer:C1_U02"] = {
        "model": "fixture-writer",
        "response": {"content": json.dumps({
            "body_markdown": "第二单元正文：背景与限制 [P0001]。", "issues": []},
            ensure_ascii=False),
            "finish_reason": "stop", "complete": True, "usage": {}},
    }
    path = tmp_path / "recordings.json"
    _write_json(path, {"recordings": recordings})
    return path


# ---------------------------------------------------------------------------
# history start
# ---------------------------------------------------------------------------


def test_history_import_preserves_reused_units_front_back_and_tables(tmp_path):
    manifest = _history_fixture(tmp_path)
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="history", out_dir=out,
                                          manifest_path=manifest,
                                          batch_root=tmp_path)
    assert report["model_calls"] == 0 and report["external_requests"] == 0
    summary = report["assembly"]
    assert summary["status"] == "complete" and summary["problems_resolved"] is True
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "普通单元正文：机制解释含" in draft, "normal unit body preserved"
    assert "跨目录复用单元正文：负面结果保留" in draft, "cross-directory reused unit preserved"
    assert "已有摘要正文" in draft and "已有结语正文" in draft, "front/back matter preserved"
    assert "复用单元标题" in draft, "title overrides honored"
    assert draft.startswith("# 综述标题")


def test_history_missing_one_unit_assembles_restricted_draft(tmp_path):
    manifest = _history_fixture(tmp_path)
    # Remove the local unit's result: only the reused unit remains.
    (tmp_path / "units" / "CH01_U01" / "UNIT_RESULT.json").unlink()
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="history", out_dir=out,
                                          manifest_path=manifest,
                                          batch_root=tmp_path)
    summary = report["assembly"]
    assert summary["status"] == "partial_check"
    assert summary["missing_units"] == ["CH01_U01"]
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "跨目录复用单元正文" in draft, "remaining usable content is kept"
    assert "普通单元正文" not in draft


def test_history_partial_writer_result_reports_unresolved(tmp_path):
    manifest = _history_fixture(tmp_path)
    _write_json(tmp_path / "units" / "CH01_U01" / "UNIT_RESULT.json",
                _unit_result("CH01", "CH01_U01", "被截断但可用的正文 [P0001]。",
                             complete=False, finish_reason="length",
                             completion_status="partial_length"))
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="history", out_dir=out,
                                          manifest_path=manifest,
                                          batch_root=tmp_path)
    summary = report["assembly"]
    assert summary["status"] == "complete"
    assert summary["problems_resolved"] is False
    assert any(row["pending_problem"] for row in summary["unit_rows"])
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "被截断但可用的正文" in draft, "usable partial body kept in restricted draft"


# ---------------------------------------------------------------------------
# rework 1: on-disk reports agree on restricted status and original counts
# ---------------------------------------------------------------------------


def test_history_reports_on_disk_distinguish_original_from_assembled(tmp_path):
    manifest = _history_fixture(tmp_path)
    (tmp_path / "units" / "CH01_U01" / "UNIT_RESULT.json").unlink()
    out = tmp_path / "out"
    delivery.run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest,
                                 batch_root=tmp_path)
    # Read the persisted files, not the function's return value.
    delivery_report = json.loads((out / "DELIVERY_REPORT.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "assembled" / "ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    run_report = (out / "assembled" / "RUN_REPORT.md").read_text(encoding="utf-8")

    assert delivery_report["original_units"] == 2, "the original plan had two units"
    assert delivery_report["assembled_units"] == 1, "only one unit entered the assembly"
    assert delivery_report["missing_units"] == ["CH01_U01"]
    assert summary["status"] == "partial_check"
    assert summary["missing_units"] == ["CH01_U01"]
    assert summary["problems_resolved"] is False
    assert delivery_report["assembly"]["status"] == summary["status"]
    assert delivery_report["assembly"]["problems_resolved"] == summary["problems_resolved"]
    assert "partial_check" in run_report
    assert "缺少：1" in run_report
    assert "待处理问题" in run_report and "missing_unit" in run_report
    # Delivery-level accounting must be explicit in the run report.
    assert "原计划 2 个单元；实际装配 1 个；缺件 1 个（CH01_U01）" in run_report


def test_plan_reports_on_disk_distinguish_original_from_assembled(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"].pop("writer:C1_U02")
    _write_json(recordings, data)
    out = tmp_path / "out"
    delivery.run_review_delivery(start="plan", out_dir=out,
                                 packet_path=packet,
                                 recordings_path=recordings)
    delivery_report = json.loads((out / "DELIVERY_REPORT.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "assembled" / "ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    run_report = (out / "assembled" / "RUN_REPORT.md").read_text(encoding="utf-8")

    assert delivery_report["original_units"] == 2
    assert delivery_report["assembled_units"] == 1
    assert delivery_report["missing_units"] == ["C1_U02"]
    assert summary["status"] == "partial_check"
    assert summary["problems_resolved"] is False
    assert summary["missing_units"] == ["C1_U02"], \
        "the persisted summary must carry the real missing units"
    assert delivery_report["assembly"]["status"] == summary["status"]
    assert "partial_check" in run_report
    assert "pending_missing_recording" in run_report
    # The run report must not read as complete: it states the delivery
    # accounting (original vs assembled vs missing) explicitly.
    assert "原计划 2 个单元；实际装配 1 个；缺件 1 个（C1_U02）" in run_report


# ---------------------------------------------------------------------------
# rework 2: BATCH_RUN.json as the only locator survives history filtering
# ---------------------------------------------------------------------------


def test_history_filter_keeps_unit_located_only_via_batch_run(tmp_path):
    manifest = _history_fixture(tmp_path)
    # U01 becomes missing (no local result).  U02 has neither reused_result
    # nor a resolvable default output — only BATCH_RUN.json locates it.
    (tmp_path / "units" / "CH01_U01" / "UNIT_RESULT.json").unlink()
    jobs = json.loads((tmp_path / "BATCH_JOBS.json").read_text(encoding="utf-8"))
    for job in jobs:
        if job["unit_id"] == "CH01_U02":
            job.pop("reused_result", None)
            job["output"] = str(tmp_path / "units" / "CH01_U02")
    _write_json(tmp_path / "BATCH_JOBS.json", jobs)
    _write_json(tmp_path / "BATCH_RUN.json", {"jobs": [{
        "chapter_id": "CH01", "unit_id": "CH01_U02",
        "result": str(tmp_path / "elsewhere" / "CH01_U02.json"),
    }]})
    out = tmp_path / "out"
    delivery.run_review_delivery(start="history", out_dir=out,
                                 manifest_path=manifest,
                                 batch_root=tmp_path)

    delivery_report = json.loads((out / "DELIVERY_REPORT.json").read_text(encoding="utf-8"))
    assert delivery_report["missing_units"] == ["CH01_U01"]
    assert delivery_report["assembled_units"] == 1
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "跨目录复用单元正文：负面结果保留" in draft, \
        "the BATCH_RUN-located unit must enter the final assembled text"
    summary = json.loads((out / "assembled" / "ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    row = next(r for r in summary["unit_rows"] if r["unit_id"] == "CH01_U02")
    assert row["result_path"] == str(tmp_path / "elsewhere" / "CH01_U02.json"), \
        "the actual resolved result path from BATCH_RUN must be preserved"


# ---------------------------------------------------------------------------
# rework 3: same-output-directory recovery
# ---------------------------------------------------------------------------


def test_plan_same_out_dir_changed_input_excludes_stale_body_others_reuse(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    out = tmp_path / "out"
    first = delivery.run_review_delivery(start="plan", out_dir=out,
                                         packet_path=packet,
                                         recordings_path=recordings)
    assert first["written_units"] == ["C1_U01", "C1_U02"]
    old_body = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "单元正文：机制段保留 80°C" in old_body

    # Change U01's owner input and drop its recording: the stale body must
    # not enter the draft; U02 (unchanged input) keeps reusing its result.
    changed = json.loads(packet.read_text(encoding="utf-8"))
    changed["chapter_plan"]["units"][0]["paragraph_briefs"][0]["development"] = \
        OWNER_A_DEV + "（输入已改变）"
    packet2 = tmp_path / "WRITER_PACKET_changed.json"
    _write_json(packet2, changed)
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"].pop("writer:C1_U01")
    _write_json(recordings, data)

    second = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet2,
                                          recordings_path=recordings)
    assert {"step": "writer:C1_U01",
            "status": delivery.PENDING_MISSING_RECORDING} in second["pending"]
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "单元正文：机制段保留 80°C" not in draft, "stale body must be excluded"
    assert "第二单元正文：背景与限制" in draft, "unchanged unit still reused"
    assert json.loads((out / "writer" / "C1_U01" / "UNIT_RESULT.json")
                      .read_text(encoding="utf-8"))["body_markdown"] \
        .startswith("单元正文：机制段保留 80°C"), \
        "the old result file itself is kept on disk, just not assembled"


def test_plan_partial_body_kept_then_completed_recording_updates_it(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    out = tmp_path / "out"
    # First run records a PARTIAL writer response (real truncated shape).
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"]["writer:C1_U01"]["response"] = {
        "content": "被长度截断的部分正文 [P0001]。",
        "finish_reason": "length", "complete": False, "usage": {}}
    _write_json(recordings, data)
    first = delivery.run_review_delivery(start="plan", out_dir=out,
                                         packet_path=packet,
                                         recordings_path=recordings)
    first_result = json.loads((out / "writer" / "C1_U01" / "UNIT_RESULT.json")
                              .read_text(encoding="utf-8"))
    assert first_result["complete"] is False
    first_draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "被长度截断的部分正文" in first_draft, "partial body kept in the restricted draft"
    assert first["assembly"]["problems_resolved"] is False

    # Same out dir, same input: now supply the COMPLETE recording.  The unit
    # must refresh locally and the complete body enters the final draft,
    # without rerunning anything else.
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"]["writer:C1_U01"]["response"] = {
        "content": json.dumps({
            "body_markdown": "完整正文：机制段保留 80°C 与 126 mAh/g（理论容量 70%）[P0001]。",
            "issues": []}, ensure_ascii=False),
        "finish_reason": "stop", "complete": True, "usage": {}}
    _write_json(recordings, data)
    second = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert second["pending"] == []
    final_result = json.loads((out / "writer" / "C1_U01" / "UNIT_RESULT.json")
                              .read_text(encoding="utf-8"))
    assert final_result["complete"] is True
    assert final_result["body_markdown"].startswith("完整正文：机制段保留 80°C")
    final_draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "完整正文：机制段保留 80°C" in final_draft
    assert "被长度截断的部分正文" not in final_draft
    # U02's recording is untouched and its result stays complete, so the
    # refreshed assembly is fully resolved again.
    assert second["assembly"]["status"] == "complete"
    assert second["assembly"]["problems_resolved"] is True


# ---------------------------------------------------------------------------
# plan start
# ---------------------------------------------------------------------------


def test_plan_replay_builds_real_messages_and_assembles(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert report["model_calls"] == 0 and report["external_requests"] == 0
    assert report["pending"] == []
    assert report["written_units"] == ["C1_U01", "C1_U02"]
    assert report["replay_modes"]["arrangement:C1"] == delivery.REPLAY_COMPATIBILITY

    # Real production message construction: the owner's original task text and
    # the source material reached the writer input verbatim.
    messages = json.loads((out / "messages" / "C1_U01_messages.json").read_text(encoding="utf-8"))
    blob = json.dumps(messages, ensure_ascii=False)
    assert OWNER_A_DEV in blob and OWNER_B_DEV in blob
    assert "source_brief_details" in blob and "P0001 的实际材料内容" in blob

    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "80°C 与 126 mAh/g（理论容量 70%）" in draft
    assert "160/107 属另一实验" in draft


def test_plan_missing_arrangement_recording_stops_chain_honestly(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path, include_arrangement=False)
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert report["pending"] == [{"step": "arrangement:C1",
                                  "status": delivery.PENDING_MISSING_RECORDING}]
    assert report["written_units"] == []
    assert (out / "messages" / "arrangement_messages.json").is_file(), \
        "the real arrangement input was still constructed and saved"


def test_plan_missing_writer_recording_keeps_other_units(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"].pop("writer:C1_U01")
    _write_json(recordings, data)

    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert {"step": "writer:C1_U01", "status": delivery.PENDING_MISSING_RECORDING} \
        in report["pending"]
    assert report["written_units"] == ["C1_U02"]
    draft = (out / "assembled" / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    assert "第二单元正文：背景与限制" in draft


def test_plan_rerun_reuses_results_and_changed_unit_only_affects_itself(tmp_path):
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    out = tmp_path / "out"
    first = delivery.run_review_delivery(start="plan", out_dir=out,
                                         packet_path=packet,
                                         recordings_path=recordings)
    assert first["written_units"] == ["C1_U01", "C1_U02"]
    first_result = (out / "writer" / "C1_U01" / "UNIT_RESULT.json").read_text(encoding="utf-8")

    # Rerun with NO writer recording at all: the unchanged input must reuse
    # the existing valid result instead of needing a replay.
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"].pop("writer:C1_U01")
    _write_json(recordings, data)
    second = delivery.run_review_delivery(start="plan", out_dir=out,
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert second["pending"] == []
    assert second["written_units"] == ["C1_U01", "C1_U02"]
    assert (out / "writer" / "C1_U01" / "UNIT_RESULT.json").read_text(encoding="utf-8") \
        == first_result, "unchanged input reuses the existing result verbatim"

    # Change one unit's input (different owner text) with no recording for it:
    # only that unit becomes pending; the rest of the chain still assembles.
    packet2_path = tmp_path / "changed" / "WRITER_PACKET.json"
    packet2 = json.loads(packet.read_text(encoding="utf-8"))
    packet2["chapter_plan"]["units"][0]["paragraph_briefs"][0]["development"] = \
        OWNER_A_DEV + "（输入已改变）"
    _write_json(packet2_path, packet2)
    third = delivery.run_review_delivery(start="plan", out_dir=tmp_path / "out3",
                                         packet_path=packet2_path,
                                         recordings_path=_recordings_without_writer(tmp_path))
    assert third["pending"] == [{"step": "writer:C1_U01",
                                 "status": delivery.PENDING_MISSING_RECORDING}]


def _recordings_without_writer(tmp_path: Path) -> Path:
    recordings = _plan_recordings(tmp_path / "rec3")
    data = json.loads(recordings.read_text(encoding="utf-8"))
    data["recordings"].pop("writer:C1_U01")
    _write_json(recordings, data)
    return recordings


def test_plan_delivery_makes_no_network_calls(tmp_path, monkeypatch):
    def _no_socket(*args, **kwargs):
        raise AssertionError("network attempted during offline delivery")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    packet = _writer_packet(tmp_path)
    recordings = _plan_recordings(tmp_path)
    report = delivery.run_review_delivery(start="plan", out_dir=tmp_path / "out",
                                          packet_path=packet,
                                          recordings_path=recordings)
    assert report["model_calls"] == 0 and report["external_requests"] == 0


def test_harness_parser_has_delivery_branch_and_help_is_offline():
    import subprocess
    result = subprocess.run(
        [sys.executable, "run_review_harness.py", "--help"],
        capture_output=True, text=True, cwd="F:/OptoMind-Review-2", timeout=120)
    assert result.returncode == 0
    assert "--delivery-start" in result.stdout
    assert "--delivery-recordings" in result.stdout
