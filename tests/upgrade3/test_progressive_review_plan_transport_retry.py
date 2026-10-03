"""Focused offline checks for transport retry and source-routing stop behavior."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.module4 import runtime


class FakeLedger:
    def __init__(self):
        self.rows = []

    def reserve(self, amount_cny, call_id):
        row = {"reservation_id": f"res-{len(self.rows)}", "amount_cny": amount_cny, "call_id": call_id}
        self.rows.append(row)
        return row

    def settle(self, reservation_id, actual_cny, *, uncertain=False, telemetry=None):
        row = next(item for item in self.rows if item["reservation_id"] == reservation_id)
        row.update({"actual_cny": actual_cny, "status": "uncertain" if uncertain else "settled", "telemetry": telemetry or {}})


class FailingOpener:
    def __init__(self, error_factory):
        self.error_factory = error_factory
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request.headers.get("Authorization"))
        raise self.error_factory()


def make_client(monkeypatch, opener, ledger, *, max_retries=1):
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_args, **_kwargs: opener)
    client = runtime.QwenDirectClient(
        model="qwen3.7-flash",
        max_retries=max_retries,
        timeout_seconds=5,
        max_output_tokens=64,
        thinking=False,
        json_mode=False,
        budget_ledger=ledger,
    )
    client._keys = lambda: ["key-0", "key-1", "key-2"]
    return client


def test_transport_retry_stays_on_current_key_and_records_sanitized_reason(monkeypatch):
    opener = FailingOpener(lambda: urllib.error.URLError(ConnectionResetError(10054, "connection unavailable")))
    ledger = FakeLedger()
    client = make_client(monkeypatch, opener, ledger)

    with pytest.raises(runtime.QwenTransportError) as caught:
        client([{"role": "user", "content": "return JSON"}], model="qwen3.7-flash")

    assert caught.value.reason_code == "URLError__ConnectionResetError__errno_10054"
    assert opener.calls == ["Bearer key-0", "Bearer key-0"]
    assert len(ledger.rows) == 2
    assert {row["status"] for row in ledger.rows} == {"uncertain"}
    assert all(
        row["telemetry"]["provider_error_code"]
        == "QwenTransportError:URLError__ConnectionResetError__errno_10054"
        for row in ledger.rows
    )


def test_server_error_retries_same_key_without_rotating(monkeypatch):
    def server_error():
        return urllib.error.HTTPError(
            "https://example.invalid/chat/completions", 503, "busy", {}, io.BytesIO(b"{}")
        )

    opener = FailingOpener(server_error)
    ledger = FakeLedger()
    client = make_client(monkeypatch, opener, ledger)

    with pytest.raises(runtime.QwenTransportError):
        client([{"role": "user", "content": "return JSON"}], model="qwen3.7-flash")

    assert opener.calls == ["Bearer key-0", "Bearer key-0"]
    assert len(ledger.rows) == 2
    assert {row["status"] for row in ledger.rows} == {"uncertain"}


def test_account_rejection_rotates_keys(monkeypatch):
    def auth_error():
        return urllib.error.HTTPError(
            "https://example.invalid/chat/completions", 401, "unauthorized", {}, io.BytesIO(b"{}")
        )

    opener = FailingOpener(auth_error)
    ledger = FakeLedger()
    client = make_client(monkeypatch, opener, ledger)

    with pytest.raises(runtime.QwenTransportError) as caught:
        client([{"role": "user", "content": "return JSON"}], model="qwen3.7-flash")

    assert caught.value.record["rotate_key"] is True
    assert opener.calls == ["Bearer key-0", "Bearer key-1", "Bearer key-2"]
    assert len(ledger.rows) == 3
    assert {row["status"] for row in ledger.rows} == {"settled"}
    assert {row["actual_cny"] for row in ledger.rows} == {0.0}


def _config(tmp_path, *, workers=3):
    return planning.ProgressivePlannerConfig(
        topic_id="topic",
        pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json",
        output_dir=tmp_path / "output",
        chapter_workers=workers,
    )


def _pool_rows(count):
    return [
        {
            "_source_handle": f"P{index:04d}",
            "_b_summary": {"title": f"Paper {index}", "year": "2024"},
        }
        for index in range(count)
    ]


def _outline():
    return {"chapters": [{"chapter_id": "C1", "title": "Mechanism", "purpose": "Explain mechanism."}]}


def test_source_transport_failure_skips_repair_and_cancels_queued_batches(monkeypatch, tmp_path):
    monkeypatch.setattr(planning, "SOURCE_ROUTING_BATCH_SIZE", 1)
    planner_calls = []

    def planner(stage, payload):
        call_id = payload["call_id"]
        planner_calls.append(call_id)
        if "batch_001" in call_id:
            time.sleep(0.05)
            raise runtime.QwenTransportError("offline", transient=True, record={"error": "URLError"})
        time.sleep(0.20)
        return {
            "_planner_call": True,
            "response": {
                "source_routes": [
                    {"source_handle": row["source_handle"], "chapter_ids": ["C1"], "specific_usable_material": "use"}
                    for row in payload["candidate_batch"]
                ]
            },
        }

    flow = planning.ProgressiveReviewPlanner(_config(tmp_path), planner=planner)
    with pytest.raises(runtime.QwenTransportError):
        flow._route_sources(_pool_rows(5), shared_outline=_outline(), resume=False, state={"topic": "topic"})
    time.sleep(0.25)

    assert planner_calls
    assert not any("repair-001" in call_id for call_id in planner_calls)
    assert not any("batch_004" in call_id or "batch_005" in call_id for call_id in planner_calls)


def test_valid_partial_routes_still_trigger_content_repair(tmp_path):
    calls = []

    def planner(stage, payload):
        calls.append(payload["call_id"])
        rows = payload["candidate_batch"]
        selected = rows if "repair-001" in payload["call_id"] else rows[:1]
        return {
            "_planner_call": True,
            "response": {
                "source_routes": [
                    {"source_handle": row["source_handle"], "chapter_ids": ["C1"], "specific_usable_material": "use"}
                    for row in selected
                ]
            },
        }

    flow = planning.ProgressiveReviewPlanner(_config(tmp_path, workers=1), planner=planner)
    result = flow._route_sources(_pool_rows(2), shared_outline=_outline(), resume=False, state={"topic": "topic"})

    assert calls == ["source-routing-batch_001-of-001", "source-routing-batch_001-of-001-repair-001"]
    assert [row["route_status"] for row in result["source_routes"]] == ["assigned", "assigned"]


def test_completed_source_batch_cache_is_reused_without_planner_call(monkeypatch, tmp_path):
    monkeypatch.setattr(planning, "SOURCE_ROUTING_BATCH_SIZE", 2)
    output = tmp_path / "output" / "stages" / "source_routing"
    output.mkdir(parents=True)
    rows = _pool_rows(2)
    def initial_planner(stage, payload):
        return {"source_routes": [
            {"source_handle": row["source_handle"], "chapter_ids": ["C1"],
             "specific_usable_material": "compatible material"}
            for row in payload["candidate_batch"]
        ]}

    initial = planning.ProgressiveReviewPlanner(_config(tmp_path, workers=1), planner=initial_planner)
    initial._route_sources(rows, shared_outline=_outline(), resume=False, state={"topic": "topic"})
    cached = json.loads((output / "batch_001.json").read_text(encoding="utf-8"))
    assert cached["cache_contract"]

    def fail_planner(*_args, **_kwargs):
        raise AssertionError("completed source batch was not reused")

    flow = planning.ProgressiveReviewPlanner(_config(tmp_path, workers=1), planner=fail_planner)
    result = flow._route_sources(rows, shared_outline=_outline(), resume=True, state={"topic": "topic"})

    assert result["source_routes"] == cached["source_routes"]
