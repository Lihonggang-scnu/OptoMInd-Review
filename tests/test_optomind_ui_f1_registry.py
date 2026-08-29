"""F1 regression tests: the canonical stage registry as single source of truth."""

from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessOrchestrator,
)
from optomind_ui import stage_registry as reg


def test_all_stages_has_23_unique_keys() -> None:
    stages = reg.all_stages()
    keys = [record["key"] for record in stages]
    assert len(stages) == 27
    assert len(set(keys)) == 27
    for record in stages:
        assert set(record) == {"key", "label", "explain", "group"}
        assert 0 < len(record["label"]) <= 6
        assert 0 < len(record["explain"]) <= 30


def test_keys_match_canonical_query_planner_union() -> None:
    # Must compare against the REAL STAGES import, never a hand-copied tuple.
    expected = {"query_planner"} | set(ReviewHarnessOrchestrator.STAGES)
    assert {record["key"] for record in reg.all_stages()} == expected
    assert len(ReviewHarnessOrchestrator.STAGES) == 26


def test_every_key_in_exactly_one_group_and_order_stable() -> None:
    grouped = reg.groups()
    names = [name for name, _ in grouped]
    assert len(names) == len(set(names)) == 7
    flat = [key for _, keys in grouped for key in keys]
    assert sorted(flat) == sorted(record["key"] for record in reg.all_stages())
    assert len(flat) == len(set(flat)) == 27


def test_unknown_keys_do_not_raise() -> None:
    assert reg.stage_label("nope_not_a_stage") == "nope_not_a_stage"
    assert reg.stage_label("") == ""
    assert reg.stage_explain("nope_not_a_stage") == ""
    assert reg.status_label("mystery_code") == "mystery_code"
    # known values stay human
    assert reg.stage_label("review_lead")
    assert reg.status_label("running") != "running"
    assert reg.status_label("degraded") != "degraded"
    assert reg.status_label("waiting_for_human") != "waiting_for_human"


def test_status_labels_absorb_legacy_server_table() -> None:
    # The exact mappings that used to live in server._STATUS_LABELS.
    legacy = {
        "starting": "准备启动",
        "running": "正在研究",
        "completed": "已完成",
        "awaiting_human_review": "等待你的确认",
        "needs_model_recovery": "需要重新整理问题",
        "budget_exhausted": "预算已用完",
        "budget_rejected": "尚未开始",
        "failed": "运行失败",
        "partial": "部分完成",
        "unknown": "历史任务",
    }
    for code, label in legacy.items():
        assert reg.status_label(code) == label, code


def test_server_source_has_no_legacy_tables() -> None:
    server_src = Path("optomind_ui/server.py").read_text(encoding="utf-8")
    for legacy_name in (
        "_PROGRESS_STEPS",
        "_CURRENT_STAGE_LABELS",
        "_STAGE_LABELS",
        "_STATUS_LABELS",
    ):
        assert legacy_name not in server_src, legacy_name


def test_progress_endpoint_uses_registry_labels(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from optomind_ui.server import create_app

    # NOTE: run ids must match ^rhr_[a-z0-9]{8,32}$ -- "rhr_" then alnum only.
    run = tmp_path / "rhr_reg20260101"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_reg20260101",
                "status": "running",
                "current_stage": "review_lead",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(run_root=tmp_path))
    response = client.get("/api/tasks/rhr_reg20260101/progress")
    assert response.status_code == 200, response.text
    body = response.json()
    # review_lead used to be missing from the 13-step track entirely.
    steps = {step["stage"]: step for step in body["steps"]}
    assert len(steps) == 27
    assert steps["review_lead"]["status"] == "running"
    assert steps["review_lead"]["label"] == reg.stage_label("review_lead")
    assert body["current_label"] == reg.stage_label("review_lead")
    assert body["status_label"] == reg.status_label("running")
    # exactly one running dot even though s2_literature_intelligence is one key
    running = [step for step in body["steps"] if step["status"] == "running"]
    assert len(running) == 1
