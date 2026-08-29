"""Offline tests for the bounded claim/section evidence-dossier input layer.

The dossier layer is a parallel input for blueprint planning/grounding: it must
be deterministic, preserve original claims and exact quotes, bound surrounding
context and material understanding with explicit truncation metadata, and keep
existing evidence digests/candidate pools intact.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.review_blueprint_planner import (
    PREFERRED_SECTION_TEXT_CANDIDATE_RANGE,
    DynamicReviewBlueprintPlanner,
    build_claim_evidence_dossiers,
    build_section_evidence_material_layer,
)


@pytest.fixture()
def dossier_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"dossier-test-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _unit(
    chunk_id: str,
    raw_text: str,
    *,
    permission: str = "factual_support",
) -> dict:
    return {
        "unit_id": f"unit:{chunk_id}",
        "work_id": f"work:{chunk_id}",
        "identity": {
            "chunk_id": chunk_id,
            "paper_id": f"paper:{chunk_id}",
            "doi": f"10.0000/{chunk_id}",
            "title": f"Title for {chunk_id}",
            "locator": {"section": "results"},
        },
        "durable_content": {
            "raw_text": raw_text,
            "content_depth": "structured_snippet",
        },
        "durable_content_card": {
            "content_quality": {
                "source_kind": "s2_body_snippet",
                "evidence_ceiling": permission,
                "context_complete": True,
            }
        },
        "query_annotations": [
            {
                "query_id": "query:test",
                "question_relevance": "substantial",
                "propositions": [
                    {
                        "proposition_id": f"prop:{chunk_id}",
                        "statement": f"Proposition about {chunk_id}.",
                        "question_function": "comparison_input",
                        "evidence_permissions": {chunk_id: permission},
                    }
                ],
                "background_contexts": [
                    {"statement": f"Background context for {chunk_id}."}
                ],
            }
        ],
        "audit": {
            "source_provenance": {
                "use_permission": permission,
                "content_depth": "structured_snippet",
            }
        },
    }


def _claim(**overrides) -> dict:
    claim = {
        "claim_id": "C1",
        "statement": (
            "The reviewed study reports a bounded comparison between the two methods."
        ),
        "evidence_relations": [
            {
                "chunk_id": "CID_Q",
                "paper_id": "paper:CID_Q",
                "relation_type": "direct_support",
                "exact_span": (
                    "The solver reaches lower error on the scalar benchmark."
                ),
                "limitations": ["Scalar benchmark only."],
            }
        ],
        "evidence_spans": [
            {
                "chunk_id": "CID_Q",
                "quote": "The solver reaches lower error on the scalar benchmark.",
                "quote_verified": True,
                "permission_ceiling": "factual_support",
                "scope_fit": "in_domain",
            }
        ],
        "boundary_conditions": ["Limited to the reported scalar benchmark."],
        "missing_evidence_components": [],
        "evidence_binding_status": "direct",
        "claim_state": "grounded",
        "evidence_requirement": "factual",
        "importance": "load_bearing",
    }
    claim.update(overrides)
    return claim


def _planner(tmp_path: Path) -> DynamicReviewBlueprintPlanner:
    return DynamicReviewBlueprintPlanner(
        tmp_path / "concepts.json",
        tmp_path / "out",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
    )


def test_claim_dossier_preserves_claim_and_source_fields() -> None:
    units = {"CID_Q": _unit("CID_Q", "Long raw source text for CID_Q. " * 40)}
    dossiers = build_claim_evidence_dossiers([_claim()], units)

    assert len(dossiers) == 1
    dossier = dossiers[0]
    assert dossier["claim_id"] == "C1"
    assert dossier["original_claim"] == _claim()["statement"]
    assert dossier["evidence_binding_status"] == "direct"
    assert dossier["boundary_conditions"] == [
        "Limited to the reported scalar benchmark."
    ]

    source = dossier["sources"][0]
    assert source["chunk_id"] == "CID_Q"
    assert source["paper_id"] == "paper:CID_Q"
    assert source["doi"] == "10.0000/CID_Q"
    assert source["title"] == "Title for CID_Q"
    assert source["locator"] == {"section": "results"}
    assert source["exact_quote"] == (
        "The solver reaches lower error on the scalar benchmark."
    )
    assert source["quote_available"] is True
    assert source["context"].startswith("Long raw source text for CID_Q.")
    assert source["permission"] == "factual_support"
    assert source["permission_source"] == "material_content_quality"
    assert source["limitations"] == ["Scalar benchmark only."]
    assert source["scope_fit"] == "in_domain"
    understanding = source["material_understanding"]
    assert understanding["included_proposition_count"] == 1
    assert understanding["entries"][0]["proposition_id"] == "prop:CID_Q"
    assert understanding["included_background_count"] == 1
    assert dossier["truncation"]["context_limit"] == 900

    # Deterministic: identical inputs produce identical dossiers.
    assert build_claim_evidence_dossiers([_claim()], units) == dossiers


def test_claim_dossier_handles_missing_material_gracefully() -> None:
    claim = _claim(
        claim_id="",
        statement="",
        evidence_relations=[],
        evidence_spans=[],
        supporting_text_chunk_ids=["MISSING_CHUNK"],
        boundary_conditions=None,
        missing_evidence_components=None,
    )
    dossiers = build_claim_evidence_dossiers([claim])

    dossier = dossiers[0]
    assert dossier["claim_id"].startswith("claim-")
    assert dossier["original_claim"] == ""
    assert dossier["material_available"] is False
    assert dossier["boundary_conditions"] == []
    source = dossier["sources"][0]
    assert source["chunk_id"] == "MISSING_CHUNK"
    assert source["material_available"] is False
    assert source["provenance_available"] is False
    assert source["quote_available"] is False
    assert source["context_available"] is False
    assert source["context_kind"] == "not_available"
    assert source["permission"] == "discovery_only"
    assert source["material_understanding"]["included_proposition_count"] == 0
    assert source["material_understanding"]["included_background_count"] == 0
    assert build_claim_evidence_dossiers(None) == []
    assert build_section_evidence_material_layer(None)["material_dossier_count"] == 0


def test_claim_dossier_bounds_chunks_and_context_with_metadata() -> None:
    chunk_ids = ["CID_1", "CID_2", "CID_3", "CID_4"]
    units = {
        cid: _unit(cid, f"Source sentence for {cid}. " * 60)
        for cid in chunk_ids
    }
    claim = _claim(
        claim_id="C_BOUNDED",
        evidence_relations=[],
        evidence_spans=[],
        supporting_text_chunk_ids=chunk_ids,
    )
    dossiers = build_claim_evidence_dossiers(
        [claim], units, per_claim_chunk_limit=2, context_limit=100
    )

    dossier = dossiers[0]
    assert dossier["source_count"] == 2
    assert dossier["excluded_chunk_count"] == 2
    assert dossier["truncation"]["excluded_chunk_ids"] == ["CID_3", "CID_4"]
    assert dossier["truncation"]["per_claim_chunk_limit"] == 2
    assert dossier["truncation"]["context_limit"] == 100
    assert dossier["truncation"]["context_truncated_sources"] == ["CID_1", "CID_2"]
    for source in dossier["sources"]:
        assert source["context_truncated"] is True
        assert source["context_limit"] == 100
        assert len(source["context"]) <= 100


def test_multi_claim_dossiers_are_independent() -> None:
    units = {
        "CID_1": _unit("CID_1", "Raw text one."),
        "CID_2": _unit("CID_2", "Raw text two."),
    }
    claims = [
        _claim(
            claim_id="C1",
            evidence_relations=[],
            evidence_spans=[],
            supporting_text_chunk_ids=["CID_1"],
        ),
        _claim(
            claim_id="C2",
            evidence_relations=[],
            evidence_spans=[],
            supporting_text_chunk_ids=["CID_2"],
        ),
    ]
    dossiers = build_claim_evidence_dossiers(claims, units)

    assert [dossier["claim_id"] for dossier in dossiers] == ["C1", "C2"]
    assert dossiers[0]["sources"][0]["chunk_id"] == "CID_1"
    assert dossiers[1]["sources"][0]["chunk_id"] == "CID_2"


def test_section_evidence_material_layer_is_bounded_and_records_drops() -> None:
    chunks = [
        {
            "chunk_id": f"C{i:02d}",
            "paper_id": f"p{i}",
            "text_preview": "preview text",
            "use_permission": "factual_support",
        }
        for i in range(15)
    ]
    layer = build_section_evidence_material_layer(chunks, chunk_limit=3)

    assert layer["schema_version"] == "review_blueprint.evidence_material_layer.v1"
    assert layer["material_dossier_count"] == 3
    assert layer["excluded_chunk_count"] == 12
    assert len(layer["excluded_chunk_ids"]) == 12
    assert layer["limits"]["chunk_limit"] == 3
    assert layer["raw_text_policy"]
    dossier = layer["material_dossiers"][0]
    assert dossier["role"] == "section_candidate"
    assert dossier["chunk_id"] == "C00"
    assert dossier["paper_id"] == "p0"
    assert dossier["permission"] == "factual_support"
    assert dossier["context_kind"] == "text_preview"


def test_attach_claim_evidence_dossiers_wires_blueprint_sections(
    dossier_tmp: Path,
) -> None:
    planner = _planner(dossier_tmp)
    planner.material_units_by_chunk_id = {
        "CID_1": _unit("CID_1", "Raw text for CID_1.")
    }
    sections = [
        {
            "section_id": "S01",
            "claims": [
                _claim(
                    claim_id="C1",
                    evidence_relations=[],
                    evidence_spans=[],
                    supporting_text_chunk_ids=["CID_1"],
                )
            ],
            "candidate_text_chunks": [
                {
                    "chunk_id": "CID_1",
                    "paper_id": "paper:CID_1",
                    "text_preview": "preview",
                    "use_permission": "factual_support",
                }
            ],
        }
    ]

    planner._attach_claim_evidence_dossiers(sections)

    section = sections[0]
    assert section["claim_evidence_dossiers"][0]["claim_id"] == "C1"
    assert section["claim_evidence_dossiers"][0]["sources"][0]["context"].startswith(
        "Raw text for CID_1."
    )
    assert section["evidence_material_layer"]["material_dossier_count"] == 1


def test_planner_payload_receives_evidence_material_layer(
    dossier_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    captured: list[dict] = []

    def fake_call(agent_name: str, messages: list[dict], **kwargs) -> dict:
        captured.append(
            {
                "agent_name": agent_name,
                "messages": list(messages),
                "enable_thinking": kwargs.get("enable_thinking"),
            }
        )
        if agent_name == "DynamicReviewBlueprintPlannerAgent":
            content = json.dumps({
                "review_thesis": "A bounded comparison thesis.",
                "narrative_strategy": "Compare methods.",
                "sections": [
                    {
                        "section_id": f"S{i:02d}",
                        "title": f"Section {i}",
                        "argument_role": "Compare.",
                        "unique_contribution": "Compare the two methods.",
                        "must_cover": ["method comparison"],
                        "must_not_cover": ["application benchmarking"],
                        "assigned_user_axes": ["method_axis"],
                        "handoff_from_previous": "previous section",
                        "handoff_to_next": "next section",
                        "key_questions": ["Question"],
                        "claim_seeds": [
                            {"claim_seed": "Seed", "relation_to_section": "support"}
                        ],
                        "transition_to_next": "Next.",
                    }
                    for i in range(1, 5)
                ],
            })
        else:
            content = "{}"
        return {
            "content": content,
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "model_tier": "premium_model",
                "mock_llm": True,
                "error_type": None,
            },
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_call)
    prompt_path = dossier_tmp / "planner_prompt.txt"
    prompt_path.write_text("Plan a review.", encoding="utf-8")
    planner = DynamicReviewBlueprintPlanner(
        dossier_tmp / "concepts.json",
        dossier_tmp / "out",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        planner_prompt_path=prompt_path,
    )
    planner.material_units_by_chunk_id = {
        "CID_1": _unit("CID_1", "Raw text for CID_1.")
    }
    evidence = {
        "retrieved_text_chunks": [
            {
                "chunk_id": "CID_1",
                "paper_id": "paper:CID_1",
                "text_preview": "preview",
                "use_permission": "factual_support",
            },
            {
                "chunk_id": "CID_2",
                "paper_id": "paper:CID_2",
                "text_preview": "preview two",
            },
        ],
        "retrieved_visual_chunks": [],
        "cluster_candidates": [],
        "selected_concept_nodes": [],
    }

    planner._llm_plan_blueprint(evidence)

    planner_call = next(
        call for call in captured
        if call["agent_name"] == "DynamicReviewBlueprintPlannerAgent"
    )
    payload = json.loads(planner_call["messages"][1]["content"])
    assert "evidence_landscape" in payload
    assert "evidence_digest" in payload["evidence_landscape"]
    layer = payload["evidence_material_layer"]
    assert layer["material_dossier_count"] == 2
    cid1 = next(
        dossier for dossier in layer["material_dossiers"]
        if dossier["chunk_id"] == "CID_1"
    )
    assert cid1["context"].startswith("Raw text for CID_1.")
    assert cid1["permission"] == "factual_support"
    assert {dossier["chunk_id"] for dossier in layer["material_dossiers"]} == {
        "CID_1",
        "CID_2",
    }
    assert captured[0]["enable_thinking"] is False


def test_architecture_material_layer_serves_at_most_20_diverse_raw_dossiers(
    dossier_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    captured: list[dict] = []
    sections = [
        {
            "section_id": f"S{i:02d}",
            "title": f"Section {i}",
            "argument_role": "Compare methods.",
            "unique_contribution": f"Contribution {i}",
            "must_cover": ["method comparison"],
            "must_not_cover": ["application benchmarking"],
            "assigned_user_axes": ["method_axis"],
            "handoff_from_previous": "previous",
            "handoff_to_next": "next",
            "key_questions": ["Question"],
            "concept_node_ids": [],
            "text_chunk_ids": [],
            "visual_chunk_ids": [],
        }
        for i in range(1, 9)
    ]

    def fake_call(agent_name: str, messages: list[dict], **kwargs) -> dict:
        captured.append(
            {
                "agent_name": agent_name,
                "messages": list(messages),
                "enable_thinking": kwargs.get("enable_thinking"),
            }
        )
        if agent_name == "DynamicReviewBlueprintPlannerAgent":
            content = json.dumps(
                {
                    "review_thesis": "A bounded comparison thesis.",
                    "narrative_strategy": "Compare methods.",
                    "sections": sections,
                }
            )
        else:
            content = "{}"
        return {
            "content": content,
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "model_tier": "premium_model",
                "mock_llm": True,
                "error_type": None,
            },
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_call)
    prompt_path = dossier_tmp / "planner_prompt_70.txt"
    prompt_path.write_text("Plan a review.", encoding="utf-8")
    planner = DynamicReviewBlueprintPlanner(
        dossier_tmp / "concepts.json",
        dossier_tmp / "out70",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        planner_prompt_path=prompt_path,
    )
    chunks = [
        {
            "chunk_id": f"c{index:03d}",
            "paper_id": f"p{index:03d}",
            "text_preview": f"preview {index}",
            "use_permission": "factual_support",
        }
        for index in range(70)
    ]
    planner.material_units_by_chunk_id = {
        f"c{index:03d}": _unit(f"c{index:03d}", f"raw text {index}")
        for index in range(70)
    }
    monkeypatch.setattr(
        planner,
        "_ground_blueprint_architecture",
        lambda parsed, evidence: dict(parsed),
    )
    evidence = {
        "retrieved_text_chunks": chunks,
        "retrieved_visual_chunks": [],
        "cluster_candidates": [],
        "selected_concept_nodes": [],
    }
    planner._llm_plan_blueprint(evidence)

    planner_call = next(
        call
        for call in captured
        if call["agent_name"] == "DynamicReviewBlueprintPlannerAgent"
    )
    assert planner_call["enable_thinking"] is False
    payload = json.loads(planner_call["messages"][1]["content"])
    layer = payload["evidence_material_layer"]
    serving = layer["raw_dossier_serving"]
    served_ids = {
        str(dossier["chunk_id"]) for dossier in layer["material_dossiers"]
    }
    assert layer["material_dossier_count"] <= 20
    assert serving["served_raw_dossier_count"] == layer["material_dossier_count"]
    assert serving["total_candidate_count"] == 70
    assert serving["omitted_count"] == 70 - len(served_ids)
    assert serving["selection_strategy"] == "diverse_ranked_batch_paper_sample"
    assert served_ids <= {f"c{index:03d}" for index in range(70)}
    assert len(
        {
            str(dossier["paper_id"]) for dossier in layer["material_dossiers"]
        }
    ) == len(served_ids)
    digest_chunk_ids = {
        str(item["chunk_id"])
        for item in payload["evidence_landscape"]["evidence_digest"][
            "chunk_index"
        ]
    }
    assert digest_chunk_ids == {f"c{index:03d}" for index in range(70)}


def test_grounder_menu_receives_evidence_material_layer(
    dossier_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    captured: list[dict] = []

    def fake_call(agent_name: str, messages: list[dict], **kwargs) -> dict:
        captured.append({"agent_name": agent_name, "messages": list(messages)})
        return {
            "content": "{}",
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "model_tier": "advanced_model",
                "mock_llm": True,
                "error_type": None,
            },
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_call)
    grounder_prompt = dossier_tmp / "grounder_prompt.txt"
    grounder_prompt.write_text("Ground evidence.", encoding="utf-8")
    monkeypatch.setattr(
        planner_module, "DEFAULT_BLUEPRINT_GROUNDER_PROMPT", grounder_prompt
    )
    planner = _planner(dossier_tmp)
    planner.material_units_by_chunk_id = {
        "CID_1": _unit("CID_1", "Raw text for CID_1.")
    }
    architecture = {
        "sections": [
            {
                "section_id": "S01",
                "title": "Compare methods",
                "argument_role": "Compare.",
                "key_questions": ["Question"],
                "claim_seeds": [
                    {"claim_seed": "Seed", "relation_to_section": "support"}
                ],
            }
        ]
    }
    evidence = {
        "retrieved_text_chunks": [
                {
                    "chunk_id": "CID_1",
                    "paper_id": "paper:CID_1",
                    "text_preview": "Compare methods preview",
                    "use_permission": "factual_support",
                    "source_kind": "s2_body_snippet",
                    "content_depth": "structured_snippet",
                },
                {
                    "chunk_id": "CID_2",
                    "paper_id": "paper:CID_2",
                    "text_preview": "Compare methods preview two",
                },
        ],
        "retrieved_visual_chunks": [],
        "selected_concept_nodes": [],
    }

    planner._ground_blueprint_architecture(architecture, evidence)

    grounder_call = next(
        call for call in captured
        if call["agent_name"] == "ReviewBlueprintEvidenceGrounderAgent"
    )
    payload = json.loads(grounder_call["messages"][1]["content"])
    menu = payload["candidate_menu"]
    assert "evidence_digest" in menu
    assert "text_inventory_policy" in menu
    layer = menu["evidence_material_layer"]
    assert layer["material_dossier_count"] == 2
    cid1 = next(
        dossier for dossier in layer["material_dossiers"]
        if dossier["chunk_id"] == "CID_1"
    )
    assert cid1["context"].startswith("Raw text for CID_1.")
    assert cid1["permission"] == "factual_support"


def _large_candidate_pool(count: int = 205) -> list[dict]:
    return [
        {
            "chunk_id": f"c{index:03d}",
            "paper_id": f"p{index:03d}",
            "title": f"Title {index}",
            "text_preview": f"RAW_BODY_{index:03d} " + ("X" * 2000),
            "material_card_binding": {
                "propositions": [
                    {
                        "proposition_id": f"prop-{index:03d}",
                        "statement": f"Proposition summary for chunk {index:03d}.",
                    }
                ]
            },
        }
        for index in range(count)
    ]


def _llm_parsed_section(count: int = 205) -> dict:
    return {
        "sections": [
            {
                "section_id": "S01",
                "title": "Method comparison",
                "argument_role": "Compare methods.",
                "concept_node_ids": [],
                "text_chunk_ids": ["c000", "c001"],
                "candidate_text_pool_ids": [
                    f"c{index:03d}" for index in range(count)
                ],
            }
        ]
    }


def test_default_pool_retains_205_candidates_with_full_batch_audit(
    dossier_tmp: Path,
) -> None:
    from optomind_research.claim_decomposer import ClaimDecomposer

    planner = _planner(dossier_tmp)
    assert planner.served_text_limit is None
    chunks = _large_candidate_pool()
    parsed = _llm_parsed_section()
    sections = planner._sections_from_llm_plan(
        parsed, {"retrieved_text_chunks": chunks, "retrieved_visual_chunks": []}
    )
    assert len(sections) == 1
    section = sections[0]
    assert len(section["candidate_text_chunks"]) == 205
    assert len(section["candidate_text_chunk_ids"]) == 205
    assert len(section["candidate_text_context"]) == 205

    pool = section["candidate_material_pool"]
    assert len(pool["candidate_chunk_ids"]) == 205
    assert len(pool["served_chunk_ids"]) == 205
    assert pool["retained_candidate_count"] == 205
    assert pool["served_candidate_count"] == 205
    assert pool["preferred_section_text_candidate_range"] == [150, 200]
    assert pool["preferred_section_text_candidate_range"] == (
        PREFERRED_SECTION_TEXT_CANDIDATE_RANGE
    )
    assert pool["candidate_pool_status"] == "above_target_range"
    assert pool["hard_cut"] is False
    assert pool["hard_200th_cutoff"] is False
    assert pool["explicit_limit_applied"] is False

    digest = section["candidate_evidence_digest"]
    expected_ids = {f"c{index:03d}" for index in range(205)}
    assert digest["chunk_count"] == 205
    assert digest["retained_chunk_count"] == 205
    assert digest["batch_count"] == 18
    assert digest["candidate_pool_status"] == "above_target_range"
    assert {str(row["chunk_id"]) for row in digest["chunk_index"]} == expected_ids
    assert {
        str(chunk_id)
        for batch in digest["batches"]
        for chunk_id in batch["chunk_ids"]
    } == expected_ids

    payload = ClaimDecomposer(real_llm=False)._build_input_payload(section)
    payload_text = json.dumps(payload, ensure_ascii=False)
    assert len(payload["candidate_text_chunks"]) == 205
    assert len(payload["evidence_batches"]) == 18
    assert {
        chunk_id
        for batch in payload["evidence_batches"]
        for chunk_id in batch["chunk_ids"]
    } == expected_ids
    # Batch summaries + compact index only: no one-shot raw body context.
    assert "RAW_BODY_" not in payload_text
    assert ("X" * 100) not in payload_text


def test_explicit_served_limit_still_truncates_audibly(
    dossier_tmp: Path,
) -> None:
    planner = DynamicReviewBlueprintPlanner(
        dossier_tmp / "concepts.json",
        dossier_tmp / "out",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        served_text_limit=40,
        model_text_context_limit=None,
    )
    chunks = _large_candidate_pool()
    parsed = _llm_parsed_section()
    sections = planner._sections_from_llm_plan(
        parsed, {"retrieved_text_chunks": chunks, "retrieved_visual_chunks": []}
    )
    section = sections[0]
    assert len(section["candidate_text_chunks"]) == 40
    assert len(section["candidate_text_chunk_ids"]) == 40
    assert len(section["candidate_text_context"]) == 40

    pool = section["candidate_material_pool"]
    assert pool["served_limit"] == 40
    assert pool["retained_candidate_count"] == 205
    assert pool["served_candidate_count"] == 40
    assert len(pool["candidate_chunk_ids"]) == 205
    assert len(pool["served_chunk_ids"]) == 40
    assert pool["explicit_limit_applied"] is True
    assert pool["explicit_limit_truncated"] is True
    assert pool["candidate_pool_status"] == "above_target_range"
    # The digest still batches every retained candidate; only the actively
    # served/context slice is constrained.
    assert section["candidate_evidence_digest"]["chunk_count"] == 205
    assert section["candidate_evidence_digest"]["batch_count"] == 18


def test_architecture_call_never_sends_all_205_raw_bodies(
    dossier_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    captured: list[dict] = []

    def fake_call(agent_name: str, messages: list[dict], **kwargs) -> dict:
        captured.append(
            {
                "agent_name": agent_name,
                "messages": list(messages),
                "enable_thinking": kwargs.get("enable_thinking"),
            }
        )
        if agent_name == "DynamicReviewBlueprintPlannerAgent":
            content = json.dumps(
                {
                    "review_thesis": "A bounded comparison thesis.",
                    "narrative_strategy": "Compare methods.",
                    "sections": [
                        {
                            "section_id": "S01",
                            "title": "Compare",
                            "argument_role": "Compare methods.",
                            "unique_contribution": "Compare the methods.",
                            "must_cover": ["comparison"],
                            "must_not_cover": ["application"],
                            "assigned_user_axes": ["method_axis"],
                            "handoff_from_previous": "previous",
                            "handoff_to_next": "next",
                            "key_questions": ["Question"],
                        }
                    ],
                }
            )
        else:
            content = "{}"
        return {
            "content": content,
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "model_tier": "premium_model",
                "mock_llm": True,
                "error_type": None,
            },
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_call)
    prompt_path = dossier_tmp / "planner_prompt_205.txt"
    prompt_path.write_text("Plan a review.", encoding="utf-8")
    planner = DynamicReviewBlueprintPlanner(
        dossier_tmp / "concepts.json",
        dossier_tmp / "out205",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        planner_prompt_path=prompt_path,
    )
    monkeypatch.setattr(
        planner,
        "_ground_blueprint_architecture",
        lambda parsed, evidence: dict(parsed),
    )
    evidence = {
        "retrieved_text_chunks": _large_candidate_pool(),
        "retrieved_visual_chunks": [],
        "cluster_candidates": [],
        "selected_concept_nodes": [],
    }
    planner._llm_plan_blueprint(evidence)

    planner_call = next(
        call
        for call in captured
        if call["agent_name"] == "DynamicReviewBlueprintPlannerAgent"
    )
    payload = json.loads(planner_call["messages"][1]["content"])
    landscape = payload["evidence_landscape"]
    expected_ids = {f"c{index:03d}" for index in range(205)}
    assert landscape["evidence_digest"]["chunk_count"] == 205
    assert landscape["evidence_digest"]["retained_chunk_count"] == 205
    assert landscape["evidence_digest"]["batch_count"] == 18
    assert {
        str(row["chunk_id"]) for row in landscape["evidence_digest"]["chunk_index"]
    } == expected_ids
    # Only a compact preview menu plus a bounded raw-dossier layer is sent.
    assert len(landscape["retrieved_text_chunks"]) == 12
    layer = payload["evidence_material_layer"]
    assert layer["material_dossier_count"] <= 20
    serving = layer["raw_dossier_serving"]
    assert serving["total_candidate_count"] == 205
    assert serving["served_raw_dossier_count"] == layer["material_dossier_count"]
    payload_text = json.dumps(payload, ensure_ascii=False)
    assert ("X" * 2000) not in payload_text
    assert payload["rules"]["preferred_section_text_candidate_range"] == [150, 200]
    assert payload["rules"]["no_hard_200th_cut"] is True
