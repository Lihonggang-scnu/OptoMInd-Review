"""Focused offline checks for adaptive chapter-details capacity handling."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view


class SyntheticCounter:
    """Deterministic record-weight counter for branch tests.

    Production uses QwenProgressivePlanner.counter.  This counter keeps the
    test fixtures small while making the complete-message gate deterministic.
    """

    def __call__(self, _encoding, messages):
        content = messages[-1]["content"]
        if "当前任务：chapter_details\n" in content:
            content = content.split("当前任务：chapter_details\n", 1)[1]
            content = content.split("\n\n【本轮交付】", 1)[0]
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            value = {}
        if isinstance(value, dict) and value.get("_weight") is not None:
            return int(value["_weight"])
        rows = value.get("source_materials") or [] if isinstance(value, dict) else []
        candidates = value.get("candidate_materials") or [] if isinstance(value, dict) else []
        weight = 100_000
        weight += sum(int(row.get("_weight") or 0) for row in rows if isinstance(row, dict))
        weight += sum(int(row.get("_weight") or 0) for row in candidates if isinstance(row, dict))
        return weight


class FakePlanner:
    counter = SyntheticCounter()
    output_tokens = 16_000

    def __init__(self, *, fail_on_batch_index=None):
        self.calls = []
        self.fail_on_batch_index = fail_on_batch_index

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(dict(payload))))
        if self.fail_on_batch_index is not None and payload.get("chapter_details_batch", {}).get("index") == self.fail_on_batch_index:
            raise RuntimeError("synthetic batch interruption")
        if payload.get("chapter_detail_batches"):
            source_handles = [
                handle
                for batch in payload["chapter_detail_batches"]
                for handle in batch.get("chapter_plan", {}).get("source_handles", [])
            ]
        else:
            source_handles = [row["source_handle"] for row in payload.get("source_materials") or []]
        return {
            "_planner_call": True,
            "response": {"chapter_plan": {
                "thesis": "test",
                "reader_objective": "test",
                "units": [{
                    "substantive_point": "findings",
                    "source_handles": source_handles,
                    "paragraph_briefs": [
                        {"point": f"finding {handle}", "development": "test", "source_handles": [handle]}
                        for handle in source_handles
                    ],
                }],
                "source_handles": source_handles,
            }},
            "telemetry": {"finish_reason": "stop"},
        }


def _payload(source_count=6, weight=200_000):
    return {
        "topic_id": "capacity-test",
        "research_question": "test question",
        "chapter": {"chapter_id": "CH01", "title": "Test"},
        "source_materials": [
            {"source_handle": f"P{index:04d}", "study_summary_A": {"finding": "A"}, "planning_material": {"finding": "B"}, "_weight": weight}
            for index in range(source_count)
        ],
        "candidate_materials": [
            {"source_handle": "P0001", "finding": "candidate assigned", "_weight": 5_000},
            {"source_handle": "P9999", "finding": "candidate unassigned", "_weight": 7_000},
        ],
    }


def test_under_limit_keeps_original_payload_and_call_semantics(tmp_path):
    planner = FakePlanner()
    payload = _payload(source_count=1, weight=1_000)
    before = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    record, meta = planning._chapter_details_adaptive_record(
        planner, payload, chapter_id="CH01", cache_root=tmp_path / "batches", resume=False
    )

    assert meta is None
    assert len(planner.calls) == 1
    assert planner.calls[0][0] == "chapter_details"
    assert "chapter_details_batch" not in planner.calls[0][1]
    assert "call_id" not in planner.calls[0][1]
    assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == before
    assert record["response"]["chapter_plan"]["units"]


def test_dynamic_two_batches_preserve_sources_candidates_and_resume_cache(tmp_path):
    payload = _payload(source_count=6, weight=150_000)
    planner = FakePlanner()
    first, meta = planning._chapter_details_adaptive_record(
        planner, payload, chapter_id="CH01", cache_root=tmp_path / "batches", resume=False
    )

    assert meta is not None
    assert meta["batch_count"] == 2
    assert all(item["estimate"]["fits"] for item in meta["batches"])
    assert len(planner.calls) == 3  # two complete source batches plus one normal-contract merge
    source_union = {
        handle
        for _stage, call_payload in planner.calls[:2]
        for handle in [row["source_handle"] for row in call_payload["source_materials"]]
    }
    candidate_union = {
        candidate["source_handle"]
        for _stage, call_payload in planner.calls[:2]
        for candidate in call_payload["candidate_materials"]
    }
    assert source_union == {f"P{index:04d}" for index in range(6)}
    assert candidate_union == {"P0001", "P9999"}
    sent_sources = {
        row["source_handle"]: row
        for _stage, call_payload in planner.calls[:2]
        for row in call_payload["source_materials"]
    }
    expected_sources = {row["source_handle"]: row for row in payload["source_materials"]}
    assert sent_sources == expected_sources
    sent_candidates = {
        row["source_handle"]: row
        for _stage, call_payload in planner.calls[:2]
        for row in call_payload["candidate_materials"]
    }
    assert sent_candidates == {row["source_handle"]: row for row in payload["candidate_materials"]}
    assert all(call_payload["chapter_details_batch"]["complete_source_records"] for _stage, call_payload in planner.calls[:2])
    assert {"P0000", "P0001", "P0002", "P0003", "P0004", "P0005"}.issubset(
        set(first["response"]["chapter_plan"]["units"][0]["source_handles"])
    )
    merge_payload = planner.calls[2][1]
    merged_findings = {
        brief["point"]
        for batch in merge_payload["chapter_detail_batches"]
        for brief in batch["chapter_plan"]["units"][0]["paragraph_briefs"]
    }
    assert {f"finding P{index:04d}" for index in range(6)} == merged_findings

    resumed = FakePlanner()
    second, resumed_meta = planning._chapter_details_adaptive_record(
        resumed, payload, chapter_id="CH01", cache_root=tmp_path / "batches", resume=True
    )
    assert resumed.calls == []
    assert resumed_meta["merge_reused"] is True
    assert all(item["reused"] for item in resumed_meta["batches"])
    assert second == first


def test_partial_batch_failure_resumes_only_missing_batch_and_merge(tmp_path):
    payload = _payload(source_count=6, weight=150_000)
    interrupted = FakePlanner(fail_on_batch_index=2)
    try:
        planning._chapter_details_adaptive_record(
            interrupted, payload, chapter_id="CH01", cache_root=tmp_path / "batches", resume=False
        )
    except RuntimeError as error:
        assert str(error) == "synthetic batch interruption"
    else:
        raise AssertionError("synthetic interruption did not occur")
    assert len(interrupted.calls) == 2

    resumed = FakePlanner()
    result, meta = planning._chapter_details_adaptive_record(
        resumed, payload, chapter_id="CH01", cache_root=tmp_path / "batches", resume=True
    )
    assert len(resumed.calls) == 2  # missing batch 2, then the merge
    assert resumed.calls[0][1]["chapter_details_batch"]["index"] == 2
    assert meta["batches"][0]["reused"] is True
    assert meta["batches"][1]["reused"] is False
    assert result["response"]["chapter_plan"]["units"]


def test_dynamic_three_batches_check_complete_messages(tmp_path):
    planner = FakePlanner()
    packages, estimates = planning._chapter_details_adaptive_batches(
        planner, _payload(source_count=9, weight=200_000)
    )

    assert len(packages) == 3
    assert len(estimates) == 3
    assert all(item["fits"] for item in estimates)
    assert sum(len(package["source_materials"]) for package in packages) == 9
    weights = [
        sum(int(row.get("_weight") or 0) for row in package["source_materials"])
        for package in packages
    ]
    assert max(weights) - min(weights) <= 250_000


def test_input_fit_but_total_overflow_is_not_treated_as_fit():
    class ConstantCounter:
        def __call__(self, _encoding, _messages):
            return 870_000

    class Planner:
        counter = ConstantCounter()
        output_tokens = 16_000

    estimate = planning._chapter_details_capacity(Planner(), {"source_materials": []})
    assert estimate["input_ok"] is True
    assert estimate["total_ok"] is False
    assert estimate["fits"] is False


def test_adaptive_plan_alias_is_consumable_by_arrangement(tmp_path):
    plan = {"thesis": "test", "substantive_units": [{"substantive_point": "point", "paragraph_briefs": []}]}
    normalized = planning._chapter_plan_with_arrangement_units(plan)
    assert "units" not in plan
    assert normalized["units"] == normalized["substantive_units"]
    packet = {"chapter": {"chapter_id": "CH01", "title": "Test"}, "chapter_plan": normalized, "source_materials": []}
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert len(view.units) == 1
