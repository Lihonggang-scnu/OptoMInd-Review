"""P2-2 regression tests: local read-only UI server.

Covers run enumeration/detail from the fake run root, EVENTS.jsonl
offset pagination backed by the (mtime, size) line-index cache, the
read-only guarantee across every GET endpoint (byte-level file
snapshot unchanged), and the single sanctioned write path: answering a
decision through human_decision_gate.resolve_decision.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optomind_research.runtime.human_decision_gate import (
    decision_state,
    request_decision,
)
from optomind_ui.server import create_app


@pytest.fixture()
def run_root(tmp_path: Path) -> Path:
    run = tmp_path / "rhr_demo_20260101"
    (run / "visual_editor" / "final").mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps({
            "run_id": "rhr_demo_20260101",
            "status": "awaiting_human_review",
            "current_stage": "delivery_gate",
            "error_count": 0,
            "stages": {
                "discovery": {"wall_time_seconds": 12.5},
                "visual_materialization": {"wall_time_seconds": 30.0},
            },
        }),
        encoding="utf-8",
    )
    (run / "query_planner").mkdir(parents=True)
    (run / "query_planner" / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps({"user_question": "请综述日间辐射制冷的光学机制与应用。"}),
        encoding="utf-8",
    )
    (run / "HARNESS_COST.json").write_text(
        json.dumps({
            "cost_cny": 0.42,
            "model_call_count": 7,
            "canonical_totals": {
                "stages": {"discovery": {"cost_cny": 0.01}},
            },
        }),
        encoding="utf-8",
    )
    (run / "DELIVERY_GATE.json").write_text(
        json.dumps({
            "status": "degraded",
            "passed": False,
            "blocking_checks": [],
            "awaiting_human_checks": ["research_plan_audit"],
            "checks": {
                "latex_audit": {
                    "ok": True,
                    "status": "passed",
                    "path": str(run / "x.pdf"),
                    "awaiting_human": False,
                },
            },
        }),
        encoding="utf-8",
    )
    lines = [
        {"ts": f"2026-01-01T00:00:{i:02d}+00:00",
         "event": "stage_started", "stage": f"stage_{i}"}
        for i in range(7)
    ]
    (run / "HARNESS_EVENTS.jsonl").write_text(
        "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines),
        encoding="utf-8",
    )
    (run / "visual_editor" / "final" / "FINAL_VISUAL_PACKAGE.json").write_text(
        json.dumps({
            "figures": [
                {"figure_id": "F1", "section_id": "S01",
                 "figure_type": "mechanism_schematic",
                 "caption_en": "cap", "local_path": "a.png",
                 "generation_status": "completed"},
                {"figure_id": "F2", "section_id": "S04",
                 "figure_type": "comparison_diagram",
                 "caption_en": "cap2", "local_path": "b.png",
                 "generation_status": "model_approved_human_pending"},
            ],
            "unfilled_visual_opportunities": [
                {"section_id": "S02", "reason": "deliberate_no_figure_abstract"},
                {"section_id": "S09", "reason": "generation_budget_exhausted"},
            ],
        }),
        encoding="utf-8",
    )
    request_decision(
        run_dir=run,
        kind="delivery_gate",
        subject_id="rhr_demo_20260101",
        context={"gate_status": "degraded"},
        options=["accept", "reject"],
    )
    # P3-3: an already-expired pending decision must survive GET reads.
    expired_id = hashlib.sha256(
        b"visual_review:S-EXPIRED"
    ).hexdigest()[:12]
    pending_dir = run / "PENDING_DECISIONS"
    pending_dir.mkdir(exist_ok=True)
    (pending_dir / f"{expired_id}.json").write_text(
        json.dumps({
            "decision_id": expired_id,
            "kind": "visual_review",
            "subject_id": "S-EXPIRED",
            "context": {},
            "options": ["accept", "reject"],
            "auto_accept_after_seconds": 1,
            "requested_default_option": "accept",
            "created_ts": time.time() - 9999,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def client(run_root: Path) -> TestClient:
    return TestClient(create_app(run_root=run_root))


def test_runs_endpoint_lists_run_summaries(client: TestClient) -> None:
    payload = client.get("/api/runs")
    assert payload.status_code == 200
    runs = payload.json()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "rhr_demo_20260101"
    assert runs[0]["status"] == "awaiting_human_review"
    assert runs[0]["question"] == "请综述日间辐射制冷的光学机制与应用。"
    assert runs[0]["status_label"] == "等待你的确认"


def test_run_detail_merges_state_cost_and_gate(
    client: TestClient,
) -> None:
    payload = client.get("/api/runs/rhr_demo_20260101")
    assert payload.status_code == 200
    detail = payload.json()
    assert detail["status"] == "awaiting_human_review"
    assert detail["cost_cny"] == 0.42
    timeline = {row["stage"]: row["wall_time_seconds"]
                for row in detail["timeline"]}
    assert timeline["discovery"] == 12.5
    assert detail["delivery_gate"]["status"] == "degraded"
    assert detail["delivery_gate"]["awaiting_human_checks"] == [
        "research_plan_audit",
    ]


def test_events_pagination_with_offset_cache(client: TestClient) -> None:
    first = client.get("/api/runs/rhr_demo_20260101/events?offset=0&limit=3")
    assert first.status_code == 200
    body = first.json()
    assert body["total_lines"] == 7
    assert body["returned"] == 3
    second = client.get("/api/runs/rhr_demo_20260101/events?offset=6&limit=5")
    tail_body = second.json()
    assert tail_body["returned"] == 1
    assert tail_body["events"][0]["event"] == "stage_started"
    # Cache is warm after the first call and reused while mtime+size hold.
    from optomind_ui import server as ui_server
    cached = [
        entry for entry in ui_server._LINE_INDEX_CACHE.values()
        if entry["offsets"]
    ]
    assert cached, "line-index cache should be populated after a page read"


def test_events_legacy_fallback(run_root: Path) -> None:
    """Runs carrying only the legacy EVENTS.jsonl must still serve."""
    legacy = run_root / "rhr_legacy_20260101"
    legacy.mkdir()
    (legacy / "EVENTS.jsonl").write_text(
        json.dumps({"ts": "t", "event": "stage_started", "stage": "s"})
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(run_root=run_root))
    response = client.get("/api/runs/rhr_legacy_20260101/events")
    assert response.status_code == 200
    body = response.json()
    assert body["total_lines"] == 1
    assert body["events"][0]["event"] == "stage_started"


def test_visuals_split_classification(client: TestClient) -> None:
    payload = client.get("/api/runs/rhr_demo_20260101/visuals")
    assert payload.status_code == 200
    data = payload.json()
    assert data["available"] is True
    assert [f["figure_id"] for f in data["delivered_figures"]] == ["F1"]
    assert [f["figure_id"] for f in data["pending_review_figures"]] == ["F2"]
    assert [o["reason"] for o in data["blocked_opportunities"]] == [
        "generation_budget_exhausted",
    ]
    assert [o["reason"] for o in data["deliberate_no_figure"]] == [
        "deliberate_no_figure_abstract",
    ]


def test_decisions_endpoints_list_and_answer(client: TestClient, run_root: Path) -> None:
    listing = client.get("/api/runs/rhr_demo_20260101/decisions")
    assert listing.status_code == 200
    pending = listing.json()["pending"]
    # Two items: the mandatory delivery_gate decision and the already-
    # expired visual_review file added by the fixture (P3-3: reads must
    # NOT auto-expire it).
    assert len(pending) == 2
    delivery_rows = [row for row in pending if row["kind"] == "delivery_gate"]
    assert len(delivery_rows) == 1
    decision_id = delivery_rows[0]["decision_id"]
    assert set(delivery_rows[0]["options"]) == {"accept", "reject"}
    answer = client.post(
        f"/api/runs/rhr_demo_20260101/decisions/{decision_id}",
        json={"chosen": "accept", "actor": "human:tester", "note": "ok"},
    )
    assert answer.status_code == 200
    state = decision_state(run_root / "rhr_demo_20260101", decision_id)
    assert state["state"] == "resolved"
    assert state["actor"] == "human:tester"
    # Double answer must fail cleanly.
    repeat = client.post(
        f"/api/runs/rhr_demo_20260101/decisions/{decision_id}",
        json={"chosen": "accept", "actor": "human:tester"},
    )
    assert repeat.status_code == 409


def test_ui_does_not_modify_run_dir(client: TestClient, run_root: Path) -> None:
    """Every GET endpoint leaves the run tree byte-identical."""

    def snapshot(root: Path) -> dict:
        result: dict = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat_result = path.stat()
                result[str(path)] = (stat_result.st_mtime_ns,
                                     stat_result.st_size)
        return result

    before = snapshot(run_root)
    endpoints = [
        "/",
        "/api/runs",
        "/api/runs/rhr_demo_20260101",
        "/api/runs/rhr_demo_20260101/events",
        "/api/runs/rhr_demo_20260101/cost",
        "/api/runs/rhr_demo_20260101/deliverables",
        "/api/runs/rhr_demo_20260101/visuals",
        "/api/runs/rhr_demo_20260101/decisions",
    ]
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
    assert snapshot(run_root) == before


def test_invalid_run_ids_are_rejected(client: TestClient) -> None:
    assert client.get("/api/runs/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/api/runs/nope").status_code == 404
