"""F1 regression tests: narrator whitelist, truncation, fidelity, bridging."""

from __future__ import annotations

import json
from pathlib import Path

from optomind_ui.narrator import build


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "rhr_narr20260101"
    s2 = run / "s2_literature_intelligence"
    s2.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_narr20260101",
                "status": "running",
                "current_stage": "s2_literature_intelligence",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (run / "HARNESS_COST.json").write_text(
        json.dumps(
            {
                "cost_cny": 1.234,
                "global_cost_budget_cny": 120.0,
                "remaining_budget_cny": 118.766,
                "model_call_count": 42,
                "stages": {
                    "authoring_revision": {"wall_time_seconds": 42.5},
                    "query_planner": {"wall_time_seconds": 7.25},
                },
            }
        ),
        encoding="utf-8",
    )
    (s2 / "S2_MATERIAL_FLOW_LEDGER.json").write_text(
        json.dumps(
            {
                "papers": [{"paper_id": f"p{i}"} for i in range(12)],
                "summary": {
                    "paper_count": 12,
                    "admitted_paper_count": 11,
                    "s2_body_paper_count": 5,
                    "oa_fulltext_paper_count": 3,
                    "abstract_claim_paper_count": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    (s2 / "S2_QUERY_TELEMETRY.json").write_text(
        json.dumps(
            {
                "total_query_count": 57,
                "graph_query_count": 6,
                "category_counts": {"snippet_search": 40, "discovery_search": 10, "citations": 7},
                "cache_hit_counts": {"citations": 2},
                "failed_counts": {"references": 1},
            }
        ),
        encoding="utf-8",
    )
    (s2 / "S2_LITERATURE_GRAPH.json").write_text(
        json.dumps(
            {
                "nodes": [1] * 21,
                "edges": [1] * 9,
                "query_runs": [1] * 6,
                "summary": {"node_count": 21, "edge_count": 9},
            }
        ),
        encoding="utf-8",
    )
    giant = "X" * 6000
    events = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event": "stage_started",
            "stage": "s2_literature_intelligence",
        },
        {
            "timestamp": "2026-01-01T00:01:00+00:00",
            "event": "mystery_custom_event",
            "stage": "review_lead",
            "secret_internal_blob": giant,  # not whitelisted -> dropped
        },
        {
            "timestamp": "2026-01-01T00:02:00+00:00",
            "event": "stage_finished",
            "stage": "topic_scoped_kb",
            "selection": giant,  # the W0 monster field -> dropped by whitelist
            "error": giant,      # whitelisted but oversized -> truncated
            "wall_time_seconds": 12.5,
            "cost_cny": 0.5,
            "status": "completed",
        },
    ]
    (run / "HARNESS_EVENTS.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    return run


def test_metrics_match_disk_values_exactly(tmp_path: Path) -> None:
    metrics = build(_make_run(tmp_path))["metrics"]
    assert metrics["cost_cny"] == 1.234
    assert metrics["global_cost_budget_cny"] == 120.0
    assert metrics["remaining_budget_cny"] == 118.766
    assert metrics["model_call_count"] == 42
    assert metrics["papers_ingested"] == 12
    assert metrics["admitted_paper_count"] == 11
    assert metrics["s2_body_paper_count"] == 5
    assert metrics["oa_fulltext_paper_count"] == 3
    assert metrics["abstract_claim_paper_count"] == 4
    assert metrics["total_query_count"] == 57
    assert metrics["graph_query_count"] == 6
    assert metrics["cache_hit_total"] == 2
    assert metrics["failed_query_total"] == 1
    assert metrics["literature_node_count"] == 21
    assert metrics["literature_edge_count"] == 9
    assert metrics["query_runs"] == 6
    assert metrics["current_stage_wall_time_seconds"] is None  # s2: absent from BOTH ledgers
    assert metrics["total_wall_time_seconds"] == 49.75  # 42.5 + 7.25


def _make_wall_run(tmp_path: Path) -> Path:
    """Fixture mirroring the reference run's two-ledger wall-time reality.

    Shapes taken from rhr_metasurface_broadband_20260823:
    * ``packaging``   completed, state-only (96.094) -- no cost entry
    * ``query_planner`` cost-only (20.86) -- no state entry
    * ``authoring_revision`` state 0.0 vs cost 1202.0 -- cost must win
    * ``latex_publication_zh`` disabled, in neither -- must stay None
    """

    run = tmp_path / "rhr_wall20260101"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "current_stage": "packaging",
                "stages": {
                    "packaging": {"status": "completed", "wall_time_seconds": 96.094},
                    "authoring_revision": {
                        "status": "awaiting_human_review",
                        "wall_time_seconds": 0.0,
                    },
                    "latex_publication_zh": {"status": "disabled_translation_failed"},
                },
            }
        ),
        encoding="utf-8",
    )
    (run / "HARNESS_COST.json").write_text(
        json.dumps(
            {
                "stages": {
                    "query_planner": {"wall_time_seconds": 20.86},
                    "authoring_revision": {"wall_time_seconds": 1202.0},
                }
            }
        ),
        encoding="utf-8",
    )
    return run


def test_current_stage_wall_time_falls_back_to_state_ledger(tmp_path: Path) -> None:
    # Regression: `packaging` completed in 96.094s but never billed, so a
    # cost-ledger-only lookup reported None for the displayed stage.
    metrics = build(_make_wall_run(tmp_path))["metrics"]
    assert metrics["current_stage_wall_time_seconds"] == 96.094


def test_cost_ledger_wins_over_state_ledger(tmp_path: Path) -> None:
    from optomind_ui.narrator import _stage_wall_time

    cost = {"authoring_revision": {"wall_time_seconds": 1202.0}}
    state = {"authoring_revision": {"wall_time_seconds": 0.0}}
    assert _stage_wall_time("authoring_revision", cost, state) == 1202.0


def test_stage_absent_from_both_ledgers_is_none_not_zero(tmp_path: Path) -> None:
    from optomind_ui.narrator import _stage_wall_time

    run = _make_wall_run(tmp_path)
    state = json.loads((run / "HARNESS_STATE.json").read_text(encoding="utf-8"))
    cost = json.loads((run / "HARNESS_COST.json").read_text(encoding="utf-8"))
    # Never ran -> no fabricated 0.0, which would read as "instant" in the UI.
    assert _stage_wall_time(
        "latex_publication_zh", cost["stages"], state["stages"]
    ) is None
    assert _stage_wall_time("", cost["stages"], state["stages"]) is None


def test_total_wall_time_includes_state_only_stages(tmp_path: Path) -> None:
    # Regression: summing the cost ledger alone silently dropped packaging.
    metrics = build(_make_wall_run(tmp_path))["metrics"]
    assert metrics["total_wall_time_seconds"] == 1318.954  # 20.86 + 1202.0 + 96.094


def test_headline_uses_top_retrieval_category(tmp_path: Path) -> None:
    projection = build(_make_run(tmp_path))
    assert projection["headline"] == "正在用 snippet_search 检索文献"
    assert projection["detail"] == "已发起 57 次查询 · 已入库 12 篇 · 命中缓存 2 次"


def test_whitelist_drops_unknown_keys(tmp_path: Path) -> None:
    lines = build(_make_run(tmp_path))["lines"]
    mystery = next(line for line in lines if line["raw_event"] == "mystery_custom_event")
    assert "secret_internal_blob" not in mystery["data"]
    finished = next(line for line in lines if line["raw_event"] == "stage_finished")
    assert "selection" not in finished["data"]


def test_oversized_whitelisted_value_is_truncated(tmp_path: Path) -> None:
    lines = build(_make_run(tmp_path))["lines"]
    finished = next(line for line in lines if line["raw_event"] == "stage_finished")
    assert finished["truncated"] is True
    assert finished["raw_bytes"] >= 6000
    assert "已截断" in finished["data"]["error"]


def test_large_dropped_field_is_reported_as_withheld(tmp_path: Path) -> None:
    # Regression: a whitelist DROP was invisible. The reference run's line 6
    # is 3,386,117 B but `selection`/`evidence` are dropped, not clipped, so
    # the row claimed 293 B with truncated=false and the UI could not say a
    # multi-MB payload had been withheld on purpose.
    from optomind_ui.narrator import project_line

    row = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event": "stage_finished",
        "stage": "topic_scoped_kb",
        "status": "completed",
        "selection": [{"blob": "y" * 200_000}],  # dropped by whitelist
    }
    line = project_line(row)
    assert "selection" not in line["data"]
    assert line["withheld_fields"] == 1
    assert line["withheld_bytes"] > 200_000
    # sizes only -- not one byte of the payload may appear
    assert "yyyy" not in json.dumps(line, ensure_ascii=False)


def test_small_dropped_fields_are_not_badged(tmp_path: Path) -> None:
    # Internal bookkeeping drops must stay silent: flagging them badged 41
    # of the reference run's 49 rows with noise like "8 字节".
    from optomind_ui.narrator import project_line

    line = project_line(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event": "stage_finished",
            "stage": "review_lead",
            "reused_flag": True,
            "runtime_kb_sqlite": "kb.sqlite",
        }
    )
    assert "withheld_bytes" not in line
    assert "withheld_fields" not in line


def test_projection_output_stays_under_64kb(tmp_path: Path) -> None:
    payload = json.dumps(build(_make_run(tmp_path)), ensure_ascii=False)
    assert len(payload.encode("utf-8")) < 64 * 1024


def test_unknown_events_do_not_raise(tmp_path: Path) -> None:
    lines = build(_make_run(tmp_path))["lines"]
    mystery = next(line for line in lines if line["raw_event"] == "mystery_custom_event")
    assert mystery["text"] == "mystery_custom_event（设计结构）"


def test_progress_never_blanks_current_stage(tmp_path: Path) -> None:
    # Ticket bug B: a just-finished stage used to blank the current stage.
    from fastapi.testclient import TestClient

    from optomind_ui.server import create_app

    run = tmp_path / "rhr_bridging202601"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_bridging202601",
                "status": "running",
                "current_stage": "review_lead",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    events = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "event": "stage_started", "stage": "review_lead"},
        {
            "timestamp": "2026-01-01T00:05:00+00:00",
            "event": "stage_finished",
            "stage": "review_lead",
            "status": "completed",
        },
    ]
    (run / "HARNESS_EVENTS.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    client = TestClient(create_app(run_root=tmp_path))
    response = client.get("/api/tasks/rhr_bridging202601/progress")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_stage"] == "review_lead"  # never empty
    assert body["bridging"] is True
    assert "正在衔接" in body["current_label"]
    steps = {step["stage"]: step for step in body["steps"]}
    assert steps["review_lead"]["status"] == "completed"


def test_progress_running_stage_not_flagged_bridging(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from optomind_ui.server import create_app

    run = tmp_path / "rhr_active20260101"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_active20260101",
                "status": "running",
                "current_stage": "section_coverage",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (run / "HARNESS_EVENTS.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "event": "stage_started",
                "stage": "section_coverage",
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/tasks/rhr_active20260101/progress").json()
    assert body["current_stage"] == "section_coverage"
    assert body["bridging"] is False
    steps = {step["stage"]: step for step in body["steps"]}
    assert steps["section_coverage"]["status"] == "running"
