"""Offline controls for preserving concrete unresolved gaps across resume.

These tests use the existing synthetic local-index fixture.  The triage
boundary is replaced with deterministic results; no provider or scientific
answer is involved.
"""
from __future__ import annotations

import socket

import pytest

from optomind_research.runtime.upgrade3 import planning_material_search as search
from optomind_research.runtime.upgrade3 import planning_supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.planning_retrieval_loop import (
    InformationNeed,
    LoopConfig,
    run_retrieval_loop,
)
from test_body03_local_lookup_contract import make_fixture, request


def test_provider_failure_cumulative_content_cannot_reset_specific_gap():
    from optomind_research.runtime.upgrade3.planning_retrieval_loop import _merge_still_missing

    assert _merge_still_missing(
        "Specific unresolved condition", "Original broad question", "Original broad question",
        useful_content="Retained material from the earlier successful attempt", status="provider_retry",
    ) == "Specific unresolved condition"
    assert _merge_still_missing(
        "Specific unresolved condition", "", "Original broad question",
        useful_content="Already supplied material newly judged sufficient", status="fulfilled",
    ) == ""


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("still-missing recovery controls are offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _runner(fixture):
    return planning.make_retrieval_loop_runner(
        fixture.config,
        key_file=fixture.config.output_dir / "NEVER_READ.key",
        budget_ledger_path=fixture.config.output_dir / "offline-ledger.sqlite",
        budget_limit_cny=1,
        local_index_path=fixture.index_path,
        allow_external=False,
    )


def _run(fixture, runner):
    return runner(
        phase="level1",
        supplement_requests=[request(fixture)],
        pool_rows=fixture.pool,
        plan=fixture.plan,
        source_handle_map=fixture.handle_map,
        resume=True,
    )


def _need(result):
    return result["retrieval_loop"]["needs"][0]


def _bump_unrelated_index_generation(fixture):
    """Change only the explicit index generation, not the source pool."""

    with search.PlanningMaterialIndex(fixture.index_path) as index:
        index.add_segments(
            "neighbor-01",
            [{
                "segment_kind": "diagnostic",
                "ordinal": 99,
                "section_path": ["Meta"],
                "text": "Synthetic index-generation marker; no source answer.",
            }],
        )
        index.record_term_document_frequency()
        index.commit()


def test_resume_with_changed_need_id_keeps_specific_gap_after_empty_attempt(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, distractors=1)
    calls = []

    def fake_triage(gap, **kwargs):
        calls.append(gap)
        if len(calls) == 1:
            return {
                "decision": "external_research",
                "usable_content": "OLD_PARTIAL_MATERIAL",
                "still_missing": "SPECIFIC_OLD_REMAINING_POINT",
            }
        return {
            "decision": "external_research",
            "usable_content": "",
            "still_missing": gap["gap_question"],
        }

    monkeypatch.setattr(planning_supplement, "run_gap_local_triage", fake_triage)
    runner = _runner(fixture)
    first = _run(fixture, runner)
    assert _need(first)["still_missing"] == "SPECIFIC_OLD_REMAINING_POINT"

    _bump_unrelated_index_generation(fixture)
    second = _run(fixture, runner)
    assert _need(second)["still_missing"] == "SPECIFIC_OLD_REMAINING_POINT"
    assert "OLD_PARTIAL_MATERIAL" in _need(second)["usable_content"]
    assert len(calls) >= 2


def test_new_material_and_explicit_remaining_point_replace_prior_gap(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, distractors=1)
    phase = {"value": "initial"}

    def fake_triage(gap, **kwargs):
        if phase["value"] == "initial":
            return {
                "decision": "external_research",
                "usable_content": "OLD_PARTIAL_MATERIAL",
                "still_missing": "SPECIFIC_OLD_REMAINING_POINT",
            }
        return {
            "decision": "external_research",
            "usable_content": "NEW_MATERIAL",
            "still_missing": "SPECIFIC_NEW_REMAINING_POINT",
        }

    monkeypatch.setattr(planning_supplement, "run_gap_local_triage", fake_triage)
    runner = _runner(fixture)
    first = _run(fixture, runner)
    assert _need(first)["still_missing"] == "SPECIFIC_OLD_REMAINING_POINT"

    _bump_unrelated_index_generation(fixture)
    phase["value"] = "updated"
    second = _run(fixture, runner)
    assert _need(second)["still_missing"] == "SPECIFIC_NEW_REMAINING_POINT"
    assert "OLD_PARTIAL_MATERIAL" in _need(second)["usable_content"]
    assert "NEW_MATERIAL" in _need(second)["usable_content"]


def test_empty_attempt_without_prior_partial_keeps_normal_question_fallback(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, distractors=0)

    def fake_triage(gap, **kwargs):
        return {
            "decision": "external_research",
            "usable_content": "",
            "still_missing": gap["gap_question"],
        }

    monkeypatch.setattr(planning_supplement, "run_gap_local_triage", fake_triage)
    result = _run(fixture, _runner(fixture))
    assert _need(result)["still_missing"] == fixture.domain["query"]
    assert not _need(result)["usable_content"]


def test_provider_retry_journal_replay_ignores_cumulative_old_material(tmp_path):
    index_path = tmp_path / "material.sqlite"
    with search.PlanningMaterialIndex(index_path) as index:
        index.commit()
    config = LoopConfig(
        index_path=index_path,
        journal_path=tmp_path / "retrieval_loop.jsonl",
        max_rounds=1,
        max_provider_retries=0,
    )
    need = InformationNeed(
        "N-provider-replay",
        "BROAD_CURRENT_QUESTION",
        owners=("CH01",),
        round_specs=({"round": 1, "targeted_queries": [{"query_type": "keyword", "query_text": "synthetic"}]},),
    )
    calls = []

    def local_triage(*, gap, round_index):
        return {
            "decision": "external_research",
            "usable_content": "",
            "still_missing": gap.question,
        }

    def external(**kwargs):
        calls.append(kwargs)
        return {
            "status": "failed",
            "provider_failed": True,
            "usable_content": "OLD_PROVIDER_PARTIAL",
            "still_missing": "SPECIFIC_OLD_PROVIDER_GAP" if len(calls) == 1 else "BROAD_CURRENT_QUESTION",
            "error": "controlled provider failure",
        }

    first = run_retrieval_loop(
        [need], config, local_triage=local_triage, external_closure=external,
    )
    second = run_retrieval_loop(
        [need], config, local_triage=local_triage, external_closure=external,
    )
    assert first["needs"][0]["still_missing"] == "SPECIFIC_OLD_PROVIDER_GAP"
    assert second["needs"][0]["still_missing"] == "SPECIFIC_OLD_PROVIDER_GAP"
    assert second["needs"][0]["usable_content"] == "OLD_PROVIDER_PARTIAL"
    assert len(calls) == 2
