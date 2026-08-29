"""Offline tests for the batched section candidate claim-pool mechanism.

Covers: bounded per-batch processing with full chunk coverage, cross-batch
deterministic merging, sparse-section no-padding, explicit bound auditing,
order-sensitive dedup, and bounded final selection from the pool.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.claim_decomposer import (
    ClaimDecomposer,
    merge_candidate_claim_proposals,
)
from optomind_research.review_blueprint_planner import build_evidence_digest


@pytest.fixture()
def pool_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"claim-pool-test-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _chunks(count: int) -> list[dict]:
    return [
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


def _section(count: int, *, batch_size: int = 12) -> dict:
    chunks = _chunks(count)
    return {
        "section_id": "S01",
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
            chunks, batch_size=batch_size
        ),
        "candidate_visual_chunks": [],
    }


def _claim_json(statement: str, ref: str, proposal_id: str, *, idx: int) -> dict:
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


def _batched_fake_chat(calls: list[dict], *, proposal_factory=None):
    def fake_chat(agent_name: str, messages: list[dict], **kwargs) -> dict:
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if "batch" not in payload:
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "statement": (
                                    f"Legacy atomic claim number {idx} about "
                                    "radiative cooling."
                                ),
                                "evidence_type": "mechanism",
                                "supporting_text_refs": ["T01"],
                                "saturation_score": 1.0,
                                "load_bearing": idx == 1,
                            }
                            for idx in (1, 2)
                        ]
                    }
                )
            }
        batch = payload["batch"]
        slots = batch["claim_proposal_ids"]
        refs = batch["chunk_index"][0]["ref"]
        if proposal_factory is not None:
            claims = proposal_factory(payload, slots, refs)
        else:
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
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "mock_llm": True,
            },
        }

    return fake_chat


def test_claim_pool_covers_181_chunks_across_batches_without_loss(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls),
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(181, batch_size=12)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
    )
    pool = decomposer.build_candidate_claim_pool(section)

    audit = pool["audit"]
    expected_ids = {f"c{index:03d}" for index in range(181)}
    assert len(calls) == 16
    assert audit["batch_count"] == 16
    assert audit["chunks_total"] == 181
    assert audit["chunks_covered"] == 181
    assert audit["missing_chunk_ids"] == []
    assert set(audit["chunk_to_batch"]) == expected_ids
    covered_ids = {
        chunk_id
        for row in audit["batches"]
        for chunk_id in row["chunk_ids"]
    }
    assert covered_ids == expected_ids
    assert all(row["claim_count"] <= 8 for row in audit["batches"])
    assert all(
        len(calls[index]["batch"]["chunk_ids"]) <= 12
        for index in range(len(calls))
    )
    assert len(pool["claims"]) == len(calls) * 8
    assert audit["pool_status"] == "above_target_range"
    assert audit["padded"] is False


def test_cross_batch_duplicates_merge_and_unique_claims_survive(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module

    calls: list[dict] = []
    duplicate_statement = (
        "Radiative cooling materials reduce building envelope heat gain."
    )

    def proposal_factory(payload, slots, refs):
        batch_index = payload["batch"]["batch_index"]
        if batch_index == 1:
            return [
                _claim_json(duplicate_statement, refs, slots[0], idx=1),
                _claim_json(
                    "Wearable radiative cooling textiles improve comfort.",
                    refs,
                    slots[1],
                    idx=2,
                ),
            ]
        return [
            _claim_json(duplicate_statement, refs, slots[0], idx=1),
            _claim_json(
                "Agricultural radiative cooling films protect crops.",
                refs,
                slots[1],
                idx=2,
            ),
        ]

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls, proposal_factory=proposal_factory),
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(13, batch_size=12)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_claims_per_batch=2,
    )
    pool = decomposer.build_candidate_claim_pool(section)

    audit = pool["audit"]
    statements = [entry["statement"] for entry in pool["claims"]]
    assert audit["proposals_returned"] == 4
    assert audit["claims_after_merge"] == 3
    assert audit["duplicates_merged"] == 1
    assert statements.count(duplicate_statement) == 1
    assert any("Wearable radiative cooling textiles" in s for s in statements)
    assert any("Agricultural radiative cooling films" in s for s in statements)
    merged_dup = next(
        entry for entry in pool["claims"]
        if entry["statement"] == duplicate_statement
    )
    assert merged_dup["supporting_text_chunk_ids"] == ["c000", "c012"]
    assert merged_dup["merged_from_proposal_ids"] == [
        "S01-P001",
        "S01-P003",
    ]
    assert "merged_across_batches" in merged_dup["critic_flags"]


def test_sparse_section_is_not_padded(pool_tmp: Path, monkeypatch) -> None:
    from optomind_research import claim_decomposer as module

    calls: list[dict] = []

    def proposal_factory(payload, slots, refs):
        return [
            _claim_json(
                "Radiative cooling materials improve thermal management.",
                refs,
                slots[0],
                idx=1,
            ),
            _claim_json(
                "Measured cooling power depends on atmospheric conditions.",
                refs,
                slots[1],
                idx=2,
            ),
        ]

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls, proposal_factory=proposal_factory),
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(3)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
    )
    pool = decomposer.build_candidate_claim_pool(section)

    audit = pool["audit"]
    assert len(calls) == 1
    assert audit["batch_count"] == 1
    assert audit["claims_after_merge"] == 2
    assert audit["proposals_returned"] == 2
    assert audit["pool_status"] == "below_target_range"
    assert audit["padded"] is False
    assert len(pool["claims"]) == 2
    assert all(entry["statement"] for entry in pool["claims"])


def test_batch_pool_drops_empty_reference_gap_proposals(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module

    calls: list[dict] = []

    def proposal_factory(payload, slots, refs):
        return [
            _claim_json(
                "Measured cooling power depends on atmospheric conditions.",
                refs,
                slots[0],
                idx=1,
            ),
            _claim_json(
                "A missing contract role should be retrieved later.",
                [],
                slots[1],
                idx=2,
            ),
        ]

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls, proposal_factory=proposal_factory),
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(3)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_claims_per_batch=2,
    )

    pool = decomposer.build_candidate_claim_pool(section)

    assert len(pool["claims"]) == 1
    assert pool["claims"][0]["supporting_text_chunk_ids"]
    assert pool["audit"]["proposals_parsed"] == 2
    assert pool["audit"]["proposals_returned"] == 1
    assert pool["audit"]["ungrounded_proposals_dropped"] == 1
    assert pool["audit"]["batches"][0][
        "ungrounded_proposals_dropped"
    ] == 1


def test_shortlist_defers_near_duplicates_before_filling_limit() -> None:
    statements = [
        "Manufacturability data on yield and wafer-scale uniformity are missing for quasi-BIC devices.",
        "Quasi-BIC devices lack manufacturability evidence for wafer-scale uniformity and production yield.",
        "Environmental stability limits the operational lifetime of perovskite quasi-BIC lasers.",
        "Cross-platform benchmarking requires common Q-factor and linewidth measurement protocols.",
    ]
    pool = {
        "claims": [
            {
                "claim_id": f"S08-P{index:03d}",
                "statement": statement,
                "evidence_type": "comparison",
                "claim_kind": "methodological_critique",
                "supporting_text_chunk_ids": [f"c{index:03d}"],
                "saturation_score": 2.0 - (index * 0.1),
                "importance": "supporting",
            }
            for index, statement in enumerate(statements, start=1)
        ],
        "batches": [],
    }
    decomposer = ClaimDecomposer(
        real_llm=False,
        final_claim_selection_limit=3,
    )

    selected = decomposer._select_final_claims_from_pool(
        pool,
        {"section_id": "S08"},
    )

    selected_ids = [claim.claim_id for claim in selected]
    assert selected_ids == ["S08-P001", "S08-P003", "S08-P004"]
    assert decomposer.last_audit["candidate_claim_shortlist_diversity"][
        "near_duplicate_candidates_deferred"
    ] == 1


def test_explicit_bounds_are_audited(pool_tmp: Path, monkeypatch) -> None:
    from optomind_research import claim_decomposer as module

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls),
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(13, batch_size=5)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_batch_size=5,
        claim_pool_claims_per_batch=3,
    )
    pool = decomposer.build_candidate_claim_pool(section)

    audit = pool["audit"]
    assert len(calls) == 3
    assert audit["batch_count"] == 3
    assert audit["batch_size"] == 5
    assert audit["claims_per_batch_limit"] == 3
    assert audit["explicit_limits"]["batch_size"] == 5
    assert audit["explicit_limits"]["claims_per_batch_limit"] == 3
    assert [len(call["batch"]["claim_proposal_ids"]) for call in calls] == [
        3,
        3,
        3,
    ]
    assert [call["batch"]["chunk_ids"] for call in calls] == [
        [f"c{index:03d}" for index in range(0, 5)],
        [f"c{index:03d}" for index in range(5, 10)],
        [f"c{index:03d}" for index in range(10, 13)],
    ]
    assert all(row["claim_count"] <= 3 for row in audit["batches"])
    assert audit["chunks_covered"] == 13


def test_normalized_dedup_preserves_order_sensitive_statements() -> None:
    entries = [
        {
            "claim_id": "S01-P001",
            "claim_proposal_id": "S01-P001",
            "statement": "Radiative cooling improves building performance.",
            "evidence_type": "mechanism",
            "claim_kind": "mechanism_synthesis",
            "supporting_text_chunk_ids": ["c000"],
            "saturation_score": 1.0,
            "load_bearing": False,
            "importance": "supporting",
            "critic_flags": [],
            "merged_from_proposal_ids": ["S01-P001"],
        },
        {
            "claim_id": "S01-P002",
            "claim_proposal_id": "S01-P002",
            "statement": "Building performance improves radiative cooling.",
            "evidence_type": "mechanism",
            "claim_kind": "mechanism_synthesis",
            "supporting_text_chunk_ids": ["c001"],
            "saturation_score": 1.0,
            "load_bearing": False,
            "importance": "supporting",
            "critic_flags": [],
            "merged_from_proposal_ids": ["S01-P002"],
        },
        {
            "claim_id": "S01-P003",
            "claim_proposal_id": "S01-P003",
            "statement": "Radiative cooling improves building performance.",
            "evidence_type": "mechanism",
            "claim_kind": "mechanism_synthesis",
            "supporting_text_chunk_ids": ["c002"],
            "saturation_score": 2.0,
            "load_bearing": True,
            "importance": "load_bearing",
            "critic_flags": [],
            "merged_from_proposal_ids": ["S01-P003"],
        },
    ]
    merged, audit = merge_candidate_claim_proposals(entries)
    assert len(merged) == 2
    assert audit["duplicates_merged"] == 1
    merged_exact = next(
        entry for entry in merged
        if entry["statement"] == "Radiative cooling improves building performance."
    )
    assert merged_exact["supporting_text_chunk_ids"] == ["c000", "c002"]
    assert merged_exact["saturation_score"] == 2.0
    assert merged_exact["load_bearing"] is True
    assert any(
        "improves radiative cooling" in entry["statement"]
        for entry in merged
    )


def test_final_selection_keeps_pool_out_of_chapter_draft(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls),
    )

    def fake_verify(self, claims, section):
        for claim in claims:
            claim.section_fit = "central"
            claim.evidence_binding_status = "direct"
            claim.evidence_binding_confidence = "high"
        return claims

    def fake_arbitrate(self, claims, section):
        for claim in claims:
            claim.evidence_type_confidence = "high"
        return claims

    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(
        EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(24, batch_size=12)
    decomposer = ClaimDecomposer(
        prompt_path=prompt,
        real_llm=True,
        claim_pool_enabled=True,
        claim_pool_claims_per_batch=4,
        final_claim_selection_limit=5,
    )
    claims = decomposer.decompose_section(section)

    pool = section["candidate_claim_pool"]
    audit = section["candidate_claim_pool_audit"]
    assert pool["audit"]["claims_after_merge"] == 8
    assert len(claims) <= 5
    assert decomposer.last_audit["legacy_single_call_used"] is False
    assert decomposer.last_audit["claim_pool_claims_selected"] == len(claims)
    assert audit["pool_status"] in {
        "below_target_range",
        "within_target_range",
        "above_target_range",
    }
    assert all(claim.evidence_type_confidence == "high" for claim in claims)
    shortlist = section["candidate_claim_pool_shortlist_audit"]
    assert shortlist["selection_limit"] == 5
    assert "downstream shortlist" in shortlist["policy"]
    assert shortlist["pool_claim_count"] == 8


def test_default_shortlist_is_32_and_stored_pool_stays_complete(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls),
    )

    def fake_verify(self, claims, section):
        for claim in claims:
            claim.section_fit = "central"
            claim.evidence_binding_status = "direct"
            claim.evidence_binding_confidence = "high"
        return claims

    def fake_arbitrate(self, claims, section):
        for claim in claims:
            claim.evidence_type_confidence = "high"
        return claims

    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(
        EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    section = _section(96, batch_size=12)
    decomposer = ClaimDecomposer(prompt_path=prompt, real_llm=True)
    assert decomposer.final_claim_selection_limit == 32
    claims = decomposer.decompose_section(section)

    pool = section["candidate_claim_pool"]
    pool_audit = pool["audit"]
    assert pool_audit["claims_after_merge"] == 64
    assert pool_audit["stored_pool_count"] == 64
    assert pool_audit["final_selection_limit"] == 32
    assert (
        pool_audit["final_selection_policy"]
        == "downstream_shortlist_only_stored_pool_never_truncated"
    )
    assert 0 < len(claims) <= 32
    shortlist = section["candidate_claim_pool_shortlist_audit"]
    assert shortlist["pool_claim_count"] == 64
    assert shortlist["selected_count"] == len(claims)
    assert shortlist["selection_limit"] == 32
    assert "shortlist" in shortlist["policy"]


def test_planner_claim_decomposer_uses_b_plus_model(monkeypatch) -> None:
    import optomind_research.claim_decomposer as claim_module
    import optomind_research.review_blueprint_planner as planner_module

    captured: dict = {}

    class FakeClaimDecomposer:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def _load_prompt(self):
            return ""

        def decompose_section(self, section):
            return []

    monkeypatch.setattr(claim_module, "ClaimDecomposer", FakeClaimDecomposer)
    planner = planner_module.DynamicReviewBlueprintPlanner(
        Path("concepts.json"),
        Path("out"),
        user_question="Compare methods.",
        problem_understanding="Compare methods.",
        scope_definition="Compare methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
    )
    planner.real_llm_claims = True
    sections, network = planner._decompose_claims([{"section_id": "S01"}])
    assert sections[0]["section_id"] == "S01"
    assert network is not None
    assert planner_module.CLAIM_DECOMPOSER_MODEL_TIER == "b_plus_model"
    assert captured["model_tier"] == "b_plus_model"
    assert captured["claim_pool_enabled"] is True


def test_auto_pool_activation_follows_digest_presence(
    pool_tmp: Path, monkeypatch
) -> None:
    from optomind_research import claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    def fake_verify(self, claims, section):
        for claim in claims:
            claim.section_fit = "central"
            claim.evidence_binding_status = "direct"
            claim.evidence_binding_confidence = "high"
        return claims

    def fake_arbitrate(self, claims, section):
        for claim in claims:
            claim.evidence_type_confidence = "high"
        return claims

    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(
        EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate
    )
    prompt = pool_tmp / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        _batched_fake_chat(calls, proposal_factory=lambda payload, slots, refs: [
            _claim_json(
                f"Atomic claim {idx} about radiative cooling.",
                refs,
                slot,
                idx=idx,
            )
            for idx, slot in enumerate(slots, start=1)
        ]),
    )
    digest_section = _section(13, batch_size=12)
    decomposer = ClaimDecomposer(prompt_path=prompt, real_llm=True)
    claims = decomposer.decompose_section(digest_section)
    assert len(claims) > 0
    assert digest_section.get("candidate_claim_pool")
    assert decomposer.last_audit["legacy_single_call_used"] is False

    calls.clear()
    legacy_section = {
        "section_id": "S02",
        "title": "Mechanism",
        "argument_role": "Explain the governing physics.",
        "candidate_text_chunks": [
            {"chunk_id": "chunk:1", "text_preview": "Evidence text."}
        ],
        "candidate_text_chunk_ids": ["chunk:1"],
    }
    legacy_decomposer = ClaimDecomposer(prompt_path=prompt, real_llm=True)
    legacy_claims = legacy_decomposer.decompose_section(legacy_section)
    assert len(legacy_claims) > 0
    assert "candidate_claim_pool" not in legacy_section
    assert legacy_decomposer.last_audit["legacy_single_call_used"] is True
