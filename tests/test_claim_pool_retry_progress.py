"""Focused tests for bounded claim-pool retries, per-section instances,
and incremental claim-pool progress artifacts."""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.claim_decomposer import ClaimDecomposer
from optomind_research.review_blueprint_planner import build_evidence_digest


@pytest.fixture()
def pool_retry_tmp() -> Path:
    root = (
        Path(__file__).resolve().parent.parent
        / f"claim-pool-retry-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _section(count: int, *, section_id: str = "S01") -> dict[str, Any]:
    chunks = [
        {
            "chunk_id": f"c{index:03d}",
            "paper_id": f"p{index:03d}",
            "title": f"Title {index}",
            "text_preview": f"Evidence body number {index}.",
            "material_card_binding": {
                "propositions": [
                    {
                        "proposition_id": f"prop-{index:03d}",
                        "statement": f"Proposition about chunk {index}.",
                    }
                ]
            },
        }
        for index in range(count)
    ]
    section = {
        "section_id": section_id,
        "title": "Radiative cooling mechanisms",
        "argument_role": "Explain the governing physics of radiative cooling.",
        "claim_graph_seed": {
            "central_claim_candidates": [
                {
                    "claim_seed": (
                        "Radiative cooling materials improve thermal "
                        "management of buildings."
                    )
                }
            ]
        },
        "candidate_text_chunks": chunks,
        "candidate_text_chunk_ids": [
            f"c{index:03d}" for index in range(count)
        ],
        "candidate_evidence_digest": build_evidence_digest(
            chunks, batch_size=12
        ),
        "candidate_visual_chunks": [],
    }
    section["section_id"] = section_id
    return section


def _planner(pool_retry_tmp: Path, **overrides):
    from optomind_research.review_blueprint_planner import (
        DynamicReviewBlueprintPlanner,
    )

    defaults = dict(
        concept_map_path=pool_retry_tmp / "concepts.json",
        output_dir=pool_retry_tmp / "out",
        user_question="Compare radiative cooling mechanisms.",
        problem_understanding="Compare radiative cooling mechanisms.",
        scope_definition="Compare radiative cooling mechanisms.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        real_llm_claims=True,
    )
    defaults.update(overrides)
    return DynamicReviewBlueprintPlanner(**defaults)


def _usage(success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "input_tokens": 10,
        "output_tokens": 5,
        "model_name": "qwen3.7-flash",
        "mock_llm": False,
    }


def _fake_batched_chat(calls: list[dict[str, Any]]):
    def _claim_json(
        statement: str, ref: str, proposal_id: str, *, idx: int
    ) -> dict[str, Any]:
        return {
            "claim_proposal_id": proposal_id,
            "statement": statement,
            "evidence_type": "mechanism",
            "supporting_text_refs": [ref],
            "counterevidence_refs": [],
            "boundary_refs": [],
            "background_refs": [],
            "author_reported_support_refs": [],
            "relation_roles": ["support"],
            "saturation_score": 1.5,
            "load_bearing": idx == 1,
        }

    def fake_chat(agent_name, messages, **kwargs):
        calls.append({"agent_name": agent_name, **kwargs})
        payload = json.loads(messages[-1]["content"])
        batch = payload["batch"]
        slots = batch["claim_proposal_ids"]
        refs = batch["chunk_index"][0]["ref"]
        claims = [
            _claim_json(
                (
                    f"Batch {batch['batch_index']} proposes atomic claim "
                    f"number {idx} about radiative cooling materials."
                ),
                refs,
                slot,
                idx=idx,
            )
            for idx, slot in enumerate(slots, start=1)
        ]
        return {
            "content": json.dumps({"claims": claims}),
            "_llm_usage": _usage(),
        }

    return fake_chat


def test_claim_pool_call_kwargs_single_model_no_retry_bounded_stream(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    pool = decomposer.build_candidate_claim_pool(_section(15))
    assert len(pool["audit"]["batches"]) == 2
    assert len(calls) == 2
    for call in calls:
        assert call["agent_name"] == "ClaimDecomposerAgent"
        assert call["model_tier"] == "b_plus_model"
        assert call["allow_model_fallback"] is False
        assert call["max_key_candidates"] == 1
        assert call["max_transport_key_candidates"] == 1
        assert call["max_retries"] == 1
        assert call["stream"] is True
        assert call["accept_partial_stream"] is False
        assert call["timeout_seconds"] == 120.0
        assert call["enable_thinking"] is False
    for attempt in pool["audit"]["attempts"]:
        assert attempt["max_retries"] == 1
        assert attempt["failed"] is False
        assert attempt["usage_recorded"] is True


def test_claim_pool_timeout_env_override(pool_retry_tmp: Path, monkeypatch) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    monkeypatch.setenv("QWEN_CLAIM_POOL_HTTP_TIMEOUT_SEC", "77")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    decomposer.build_candidate_claim_pool(_section(5))
    assert calls[0]["timeout_seconds"] == 77.0


def test_claim_pool_progress_jsonl_writes_batch_events(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )
    progress_path = pool_retry_tmp / "claim_pool_progress.jsonl"
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
        claim_pool_progress_path=progress_path,
    )

    pool = decomposer.build_candidate_claim_pool(_section(15))

    assert progress_path.exists()
    events = [
        json.loads(line)
        for line in progress_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_names = [event["event"] for event in events]
    assert event_names[0] == "claim_pool_started"
    assert event_names[-1] == "claim_pool_finished"
    assert event_names.count("batch_started") == 2
    assert event_names.count("batch_finished") == 2
    assert events[0]["planned_batch_count"] == 2
    assert events[-1]["completed_batch_count"] == 2
    assert all(event["section_id"] == "S01" for event in events)
    assert all(
        event["success"] is True
        for event in events
        if event["event"] == "batch_finished"
    )
    assert pool["audit"]["batch_count"] == 2


def test_claim_pool_attempt_records_failure_truthfully(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    def failing_chat(agent_name, messages, **kwargs):
        return {
            "content": "[fallback] Qwen chat failed.",
            "_llm_usage": _usage(success=False),
            "error": "timeout",
        }

    monkeypatch.setattr(decomposer_module, "call_qwen_chat", failing_chat)
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    pool = decomposer.build_candidate_claim_pool(_section(5))
    assert pool["audit"]["attempts"][0]["failed"] is True
    assert pool["audit"]["attempts"][0]["max_retries"] == 1


def test_claim_pool_batch_failure_stops_later_batches(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []

    def failing_first_batch(agent_name, messages, **kwargs):
        calls.append(kwargs)
        return {
            "content": "[fallback] Qwen chat failed.",
            "_llm_usage": _usage(success=False),
            "error": "timeout",
        }

    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", failing_first_batch
    )
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    pool = decomposer.build_candidate_claim_pool(_section(15))
    assert len(calls) == 1  # second batch must never be launched
    audit = pool["audit"]
    assert audit["section_aborted"] is True
    assert audit["abort_reason"] == "batch_transport_failure"
    assert audit["abort_error"] == "timeout"
    assert audit["batches_completed_before_abort"] == 0
    assert pool["pool_status"] == "aborted"


def test_partial_recovery_keeps_last_valid_claims_and_checkpoint_audit(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []
    successful_chat = _fake_batched_chat(calls)

    def fail_second_batch(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(kwargs)
        if int(payload["batch"]["batch_index"]) == 2:
            return {
                "content": "[fallback] Qwen chat failed.",
                "_llm_usage": _usage(success=False),
                "error": "timeout",
            }
        return successful_chat(agent_name, messages, **kwargs)

    monkeypatch.setattr(decomposer_module, "call_qwen_chat", fail_second_batch)
    progress_path = pool_retry_tmp / "claim_pool_progress.jsonl"
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
        claim_pool_progress_path=progress_path,
    )

    section = _section(15)
    claims = decomposer.decompose_section(section)

    assert claims, "a failed later batch must not discard the valid first batch"
    shortlist = section["candidate_claim_pool_shortlist_audit"]
    assert shortlist["section_aborted"] is True
    assert shortlist["partial_recovery"] is True
    assert shortlist["completed_batch_count"] == 1
    assert shortlist["failed_batch_id"]
    assert shortlist["selected_count"] == len(claims)

    checkpoint = progress_path.parent / "claim_pool_last_valid" / "S01.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    audit = payload["last_valid_audit"]
    assert audit["completed_batch_count"] == 1
    assert audit["failed_batch_id"] == shortlist["failed_batch_id"]
    assert audit["abort_reason"] == "batch_transport_failure"
    assert audit["abort_error"] == "timeout"
    assert audit["selected_count"] == len(claims)


def test_claim_pool_batch_exception_aborts_section_without_claims(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []

    def raising_chat(agent_name, messages, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("socket reset")

    monkeypatch.setattr(decomposer_module, "call_qwen_chat", raising_chat)
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    section = _section(15)
    claims = decomposer.decompose_section(section)
    assert claims == []
    assert len(calls) == 1
    audit = section["candidate_claim_pool_audit"]
    assert audit["section_aborted"] is True
    assert audit["abort_reason"] == "batch_exception"
    assert audit["abort_error"] == "RuntimeError"
    assert section["candidate_claim_pool_shortlist_audit"][
        "section_aborted"
    ] is True
    assert section["candidate_claim_pool_shortlist_audit"]["selected_count"] == 0


def test_decompose_claims_uses_independent_instances_and_progress(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module
    from optomind_research import review_blueprint_planner as planner_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )

    real_decomposer_cls = decomposer_module.ClaimDecomposer
    instances: list[ClaimDecomposer] = []

    class TrackingDecomposer(real_decomposer_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def _verify_and_arbitrate(self, section, claims, text_chunks, repair=None):
            raise AssertionError(
                "formal verification must not run during deferred "
                "candidate-pool generation"
            )

    monkeypatch.setattr(
        decomposer_module, "ClaimDecomposer", TrackingDecomposer
    )

    writes: list[tuple[str, Any]] = []
    real_write_json = planner_module.write_json

    def recording_write_json(path, value):
        writes.append((str(path), copy.deepcopy(value)))
        real_write_json(path, value)

    monkeypatch.setattr(planner_module, "write_json", recording_write_json)
    planner = _planner(pool_retry_tmp)
    sections = [
        _section(15, section_id="S01"),
        _section(5, section_id="S02"),
    ]
    returned_sections, network = planner._decompose_claims(sections)

    assert len(instances) == 2
    attempts_per_instance = sorted(
        len(instance.last_audit.get("claim_pool_generation_attempts") or [])
        for instance in instances
    )
    assert attempts_per_instance == [1, 2]  # per-section, not cumulative

    progress_writes = [
        value
        for path, value in writes
        if path.endswith("review_blueprint_claim_pool_progress.json")
    ]
    assert len(progress_writes) == 3  # initial + one per section
    assert [row["completed_count"] for row in progress_writes] == [0, 1, 2]
    final_progress = progress_writes[-1]
    assert final_progress["remaining_count"] == 0
    assert final_progress["completed_section_ids"] == ["S01", "S02"]
    assert len(final_progress["sections"]) == 2
    s01_row = next(
        row for row in final_progress["sections"]
        if row["section_id"] == "S01"
    )
    assert s01_row["stored_pool_claim_count"] > 0
    assert s01_row["final_selected_claim_count"] > 0
    assert s01_row["candidate_pool_audit"]["attempts"][0]["max_retries"] == 1

    progress_path = (
        pool_retry_tmp / "out" / "review_blueprint_claim_pool_progress.json"
    )
    assert progress_path.exists()
    on_disk = json.loads(progress_path.read_text(encoding="utf-8"))
    assert on_disk["completed_count"] == 2
    assert on_disk["created_at"]
    assert on_disk["updated_at"]
    assert all(
        "candidate_claim_pool" in section for section in returned_sections
    )
    assert all(section["claims"] for section in returned_sections)
    assert len(calls) == 3  # 2 batches for S01 + 1 batch for S02


def test_deferred_planner_path_never_calls_verify_and_preserves_state(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module
    from optomind_research import review_blueprint_planner as planner_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )

    real_decomposer_cls = decomposer_module.ClaimDecomposer
    instances: list[ClaimDecomposer] = []

    class TrackingDecomposer(real_decomposer_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def _verify_and_arbitrate(self, section, claims, text_chunks, repair=None):
            raise AssertionError(
                "formal verification must not run during deferred "
                "candidate-pool generation"
            )

    monkeypatch.setattr(
        decomposer_module, "ClaimDecomposer", TrackingDecomposer
    )
    planner = _planner(pool_retry_tmp)
    sections = [
        _section(15, section_id="S01"),
        _section(5, section_id="S02"),
    ]
    returned_sections, _ = planner._decompose_claims(sections)

    assert len(instances) == 2
    assert all(instance.last_audit["formal_verification_deferred"] is True
               for instance in instances)
    for section in returned_sections:
        assert section["claims"]
        assert "candidate_claim_pool" in section
        shortlist = section["candidate_claim_pool_shortlist_audit"]
        assert shortlist["formal_verification_deferred"] is True
        assert shortlist["formal_verification_policy"] == (
            "deferred_to_later_explicit_stage"
        )
        assert all(
            "formal_verification_deferred" in claim["critic_flags"]
            for claim in section["claims"]
        )

    progress_path = (
        pool_retry_tmp / "out" / "review_blueprint_claim_pool_progress.json"
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["sections"][0][
        "candidate_claim_pool_shortlist_audit"
    ]["formal_verification_deferred"] is True


def test_default_verification_still_called(pool_retry_tmp: Path, monkeypatch) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_batched_chat(calls)
    )
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
    )
    verify_calls: list[tuple[Any, ...]] = []

    def fake_verify(section, claims, text_chunks, repair=None):
        verify_calls.append((section["section_id"], len(claims)))
        return claims

    decomposer._verify_and_arbitrate = fake_verify
    claims = decomposer.decompose_section(_section(5))
    assert len(verify_calls) == 1
    assert verify_calls[0][0] == "S01"
    assert len(claims) > 0
    assert decomposer.last_audit.get("formal_verification_deferred") is None


def test_transport_abort_with_deferred_flag_remains_fail_closed(
    pool_retry_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as decomposer_module

    def failing_chat(agent_name, messages, **kwargs):
        return {
            "content": "[fallback] Qwen chat failed.",
            "_llm_usage": _usage(success=False),
            "error": "timeout",
        }

    monkeypatch.setattr(decomposer_module, "call_qwen_chat", failing_chat)
    decomposer = ClaimDecomposer(
        model_tier="b_plus_model",
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=12,
        verify_candidate_pool_claims=False,
    )
    section = _section(15)
    claims = decomposer.decompose_section(section)
    assert claims == []
    audit = section["candidate_claim_pool_audit"]
    assert audit["section_aborted"] is True
    shortlist = section["candidate_claim_pool_shortlist_audit"]
    assert shortlist["section_aborted"] is True
    assert shortlist["selected_count"] == 0
    assert "formal_verification_deferred" not in shortlist
