"""Focused tests for forced reground of a reused Qwen architecture."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.review_blueprint_planner import (
    DynamicReviewBlueprintPlanner,
    build_parser,
)


@pytest.fixture()
def reground_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"force-reground-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _planner(reground_tmp: Path, **overrides) -> DynamicReviewBlueprintPlanner:
    defaults = dict(
        concept_map_path=reground_tmp / "concepts.json",
        output_dir=reground_tmp / "out",
        user_question="Compare radiative cooling mechanisms.",
        problem_understanding="Compare radiative cooling mechanisms.",
        scope_definition="Compare radiative cooling mechanisms.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
    )
    defaults.update(overrides)
    return DynamicReviewBlueprintPlanner(**defaults)


def _checkpoint(reground_tmp: Path, *, stale_id: str = "stale:001") -> Path:
    checkpoint = {
        "review_thesis": (
            "Radiative cooling materials improve building thermal management."
        ),
        "narrative_strategy": "Mechanism-to-application progression.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Radiative cooling mechanisms",
                "argument_role": (
                    "Explain the governing physics of radiative cooling."
                ),
                "unique_contribution": (
                    "Establish the physical basis of radiative cooling."
                ),
                "must_cover": ["physical mechanism"],
                "must_not_cover": ["application benchmarking"],
                "assigned_user_axes": ["mechanism_axis"],
                "handoff_from_previous": "introduction",
                "handoff_to_next": "material routes",
                "key_questions": [
                    "How does radiative cooling improve thermal management?"
                ],
                "claim_seeds": [
                    {
                        "claim_seed": (
                            "Radiative cooling materials improve building "
                            "thermal management."
                        ),
                        "relation_to_section": "support",
                    }
                ],
                "text_chunk_ids": [stale_id],
                "concept_node_ids": ["stale:node"],
                "visual_chunk_ids": ["stale:visual"],
                "candidate_text_chunk_ids": [stale_id],
                "candidate_material_pool": {
                    "candidate_chunk_ids": [stale_id],
                    "served_chunk_ids": [stale_id],
                    "served_limit": None,
                },
                "claim_graph_seed": [
                    {
                        "claim_seed": "Stale binding.",
                        "supporting_text_chunk_ids": [stale_id],
                    }
                ],
                "candidate_search_seeds": ["stale query"],
                "evidence_risks": ["stale risk"],
                "_grounding_status": "llm_grounded",
                "_text_candidate_admission_audit": {"route": "stale"},
            }
        ],
        "_grounding_summary": {
            "mode": "parallel_section_grounding",
            "sections": 1,
        },
        "high_value_gap_seeds": [{"gap": "stale gap", "query": "stale"}],
    }
    path = reground_tmp / "grounded_checkpoint.json"
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    return path


def _fresh_evidence() -> dict:
    preview = (
        "Radiative cooling materials improve thermal management of buildings "
        "under clear-sky conditions."
    )
    return {
        "selected_concept_nodes": [],
        "retrieved_text_chunks": [
            {
                "chunk_id": "c100",
                "paper_id": "p100",
                "title": "Fresh paper 100",
                "section_path": "results",
                "text_preview": preview,
                "material_binding_search_text": preview,
                "use_permission": "factual_support",
            },
            {
                "chunk_id": "c101",
                "paper_id": "p101",
                "title": "Fresh paper 101",
                "section_path": "results",
                "text_preview": preview,
                "material_binding_search_text": preview,
                "use_permission": "factual_support",
            },
        ],
        "retrieved_visual_chunks": [],
    }


def _final_v8_blueprint() -> dict:
    section = {
        "section_id": "S01",
        "title": "Radiative cooling mechanisms",
        "argument_role": "Explain the governing physics of radiative cooling.",
        "unique_contribution": "Establish the physical basis.",
        "must_cover": ["physical mechanism"],
        "must_not_cover": ["application benchmarking"],
        "assigned_user_axes": ["mechanism_axis"],
        "handoff_from_previous": "introduction",
        "handoff_to_next": "material routes",
        "key_questions": ["How does radiative cooling improve thermal management?"],
        "claim_seeds": [
            {
                "claim_seed": "Radiative cooling materials improve thermal management.",
                "relation_to_section": "support",
            }
        ],
        "visual_argument_goals": [
            {"goal": "Show the cooling mechanism", "purpose": "Inspect it."}
        ],
        "candidate_search_seeds": ["radiative cooling mechanism query"],
        "writing_requirements": ["Bind claims to exact source spans."],
        "evidence_risks": ["Original evidence still needs binding."],
        "transition_to_next": "Material routes follow.",
        # Material/downstream-derived state that must never reach the grounder.
        "candidate_claim_pool": {
            "claims": [
                {
                    "claim_id": "S02-POLD",
                    "statement": "Old pool statement.",
                    "supporting_text_chunk_ids": ["old:chunk"],
                }
            ]
        },
        "candidate_claim_pool_audit": {"pool_status": "old"},
        "candidate_claim_pool_shortlist_audit": {"selected_count": 1},
        "claims": [
            {
                "claim_id": "old-claim",
                "statement": "Old final claim.",
                "supporting_text_chunk_ids": ["old:chunk"],
            }
        ],
        "argument_contract": {"old": "contract"},
        "argument_structure": {"old": "structure"},
        "axis_assignments": [{"axis_id": "old-axis"}],
        "generated_from": {"planner": "old"},
        "concept_map_nodes": [{"node_id": "old-node"}],
        "checkpoint_rematerialization_audit": {"old": True},
        "claim_evidence_dossiers": [],
        "evidence_material_layer": {},
        "candidate_text_chunks": [{"chunk_id": "old:chunk"}],
        "candidate_text_context": [],
        "candidate_text_chunk_ids": ["old:chunk"],
        "candidate_evidence_digest": {},
        "candidate_text_model_policy": {},
        "candidate_material_pool": {
            "candidate_chunk_ids": ["old:chunk"],
            "served_chunk_ids": ["old:chunk"],
        },
        "candidate_visual_chunks": [],
        "claim_graph_seed": [{"claim_seed": "Old source binding."}],
        "visual_argument_slots": [],
        "_text_candidate_admission_audit": {"route": "old"},
        "_grounding_status": "llm_grounded",
    }
    return {
        "review_thesis": "Radiative cooling improves building thermal management.",
        "narrative_strategy": "Mechanism-to-application progression.",
        "high_value_gap_seeds": [{"gap": "Qwen architecture gap", "query": "q"}],
        "input_context": {"user_question": "q?"},
        "sections": [section],
    }


def test_constructor_and_cli_expose_force_reground_default_false(
    reground_tmp: Path,
) -> None:
    assert _planner(reground_tmp).force_reground is False
    assert _planner(reground_tmp, force_reground=True).force_reground is True
    args = build_parser().parse_args([])
    assert args.force_reground is False
    assert build_parser().parse_args(["--force-reground"]).force_reground is True
    assert (
        build_parser().parse_args(["--no-force-reground"]).force_reground
        is False
    )


def test_normal_reuse_unchanged_when_false(
    reground_tmp: Path, monkeypatch
) -> None:
    checkpoint_path = _checkpoint(reground_tmp)
    planner = _planner(
        reground_tmp, planner_architecture_path=checkpoint_path
    )

    def boom_ground(*args, **kwargs):
        raise AssertionError("grounder must not run for normal reuse")

    monkeypatch.setattr(planner, "_ground_blueprint_architecture", boom_ground)
    reused = planner._production_architecture({})
    assert reused["_reused_grounded_architecture"] is True
    assert "_forced_reground" not in reused
    section = reused["sections"][0]
    assert section["text_chunk_ids"] == ["stale:001"]
    assert section["candidate_material_pool"]["candidate_chunk_ids"] == [
        "stale:001"
    ]
    assert reused["high_value_gap_seeds"][0]["gap"] == "stale gap"


def test_force_reground_strips_stale_state_before_grounder(
    reground_tmp: Path, monkeypatch
) -> None:
    checkpoint_path = _checkpoint(reground_tmp)
    planner = _planner(
        reground_tmp,
        planner_architecture_path=checkpoint_path,
        force_reground=True,
    )
    captured: dict = {}

    def fake_ground(architecture, evidence):
        captured["architecture"] = architecture
        return dict(architecture)

    monkeypatch.setattr(planner, "_ground_blueprint_architecture", fake_ground)
    grounded = planner._production_architecture({})
    architecture = captured["architecture"]
    section = architecture["sections"][0]
    for stale_key in (
        "text_chunk_ids",
        "concept_node_ids",
        "visual_chunk_ids",
        "candidate_text_chunk_ids",
        "candidate_material_pool",
        "claim_graph_seed",
        "_grounding_status",
        "_text_candidate_admission_audit",
        "visual_argument_slots",
    ):
        assert stale_key not in section, stale_key
    for stale_key in (
        "_grounding_summary",
        "_reused_grounded_architecture",
    ):
        assert stale_key not in architecture, stale_key
    # Qwen intellectual fields are preserved exactly.
    assert section["candidate_search_seeds"] == ["stale query"]
    assert section["evidence_risks"] == ["stale risk"]
    assert architecture["high_value_gap_seeds"] == [
        {"gap": "stale gap", "query": "stale"}
    ]
    assert section["title"] == "Radiative cooling mechanisms"
    assert section["argument_role"].startswith(
        "Explain the governing physics"
    )
    assert section["unique_contribution"]
    assert section["must_cover"] == ["physical mechanism"]
    assert section["claim_seeds"]
    assert architecture["_forced_reground"]["requested"] is True
    assert architecture["_forced_reground"]["source_checkpoint"] == str(
        checkpoint_path.resolve()
    )
    assert grounded["_reused_grounded_architecture"] is False


def test_force_reground_rebuilds_pool_from_current_library(
    reground_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    checkpoint_path = _checkpoint(reground_tmp)
    planner = _planner(
        reground_tmp,
        planner_architecture_path=checkpoint_path,
        force_reground=True,
    )

    def fake_chat(agent_name, messages, **kwargs):
        return {
            "content": "{}",
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "mock_llm": True,
            },
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    grounded = planner._production_architecture(_fresh_evidence())

    assert grounded["_forced_reground"]["requested"] is True
    assert grounded["_forced_reground"]["source_checkpoint"] == str(
        checkpoint_path.resolve()
    )
    assert grounded["_grounding_summary"]["forced_reground"] is True
    assert grounded["_grounding_summary"]["source_checkpoint"] == str(
        checkpoint_path.resolve()
    )
    assert grounded["_reused_grounded_architecture"] is False
    section = grounded["sections"][0]
    pool_ids = section["candidate_material_pool"]["candidate_chunk_ids"]
    assert "stale:001" not in pool_ids
    assert set(section["text_chunk_ids"]) == {"c100", "c101"}
    assert section["candidate_search_seeds"] == ["stale query"]
    assert "stale risk" in section["evidence_risks"]
    assert "Stale binding." not in json.dumps(
        section["claim_graph_seed"], ensure_ascii=False
    )
    assert section["title"] == "Radiative cooling mechanisms"
    assert grounded["high_value_gap_seeds"] == [
        {"gap": "stale gap", "query": "stale"}
    ]
    checkpoint_path_written = (
        reground_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert checkpoint_path_written.exists()
    written = json.loads(checkpoint_path_written.read_text(encoding="utf-8"))
    assert written["_grounding_summary"]["forced_reground"] is True
    assert "stale:001" not in json.dumps(written, ensure_ascii=False)


def test_final_v8_section_stripped_to_intellectual_contract(
    reground_tmp: Path, monkeypatch
) -> None:
    final_blueprint = _final_v8_blueprint()
    path = reground_tmp / "final_review_blueprint.json"
    path.write_text(json.dumps(final_blueprint), encoding="utf-8")
    planner = _planner(
        reground_tmp,
        planner_architecture_path=path,
        force_reground=True,
    )
    captured: dict = {}

    def fake_ground(architecture, evidence):
        captured["architecture"] = architecture
        return dict(architecture)

    monkeypatch.setattr(planner, "_ground_blueprint_architecture", fake_ground)
    grounded = planner._production_architecture({})
    section = captured["architecture"]["sections"][0]
    intellectual_keys = {
        "section_id",
        "title",
        "argument_role",
        "unique_contribution",
        "must_cover",
        "must_not_cover",
        "assigned_user_axes",
        "handoff_from_previous",
        "handoff_to_next",
        "key_questions",
        "claim_seeds",
        "visual_argument_goals",
        "candidate_search_seeds",
        "writing_requirements",
        "evidence_risks",
        "transition_to_next",
    }
    assert set(section.keys()) == intellectual_keys
    for key in intellectual_keys:
        assert section[key] == final_blueprint["sections"][0][key], key
    for stale_key in (
        "candidate_claim_pool",
        "candidate_claim_pool_audit",
        "candidate_claim_pool_shortlist_audit",
        "claims",
        "argument_contract",
        "argument_structure",
        "axis_assignments",
        "generated_from",
        "concept_map_nodes",
        "checkpoint_rematerialization_audit",
        "claim_evidence_dossiers",
        "evidence_material_layer",
        "candidate_text_chunks",
        "candidate_text_context",
        "candidate_text_chunk_ids",
        "candidate_evidence_digest",
        "candidate_text_model_policy",
        "candidate_material_pool",
        "candidate_visual_chunks",
        "claim_graph_seed",
        "visual_argument_slots",
        "_text_candidate_admission_audit",
        "_grounding_status",
    ):
        assert stale_key not in section, stale_key
    serialized = json.dumps(captured["architecture"], ensure_ascii=False)
    for stale_fragment in (
        "old:chunk",
        "old claim",
        "Old pool statement.",
        "Old source binding.",
        "old contract",
        "old grounding",
        "old-axis",
        "old-node",
        "old-claim",
    ):
        assert stale_fragment not in serialized, stale_fragment
    assert captured["architecture"]["review_thesis"] == (
        "Radiative cooling improves building thermal management."
    )
    assert captured["architecture"]["narrative_strategy"] == (
        "Mechanism-to-application progression."
    )
    assert captured["architecture"]["high_value_gap_seeds"] == [
        {"gap": "Qwen architecture gap", "query": "q"}
    ]
    assert captured["architecture"]["input_context"] == {
        "user_question": "q?"
    }
    assert grounded["_reused_grounded_architecture"] is False
