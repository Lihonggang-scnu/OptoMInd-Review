"""Focused tests for section-specific semantic candidate admission.

Covers: >200 strong candidates all survive with off-topic candidates
excluded, sparse relevant sets are not padded, semantic failure falls back to
audited lexical admission (never all-global), explicit served limits still cap
audibly, and grounder transport is reduced to the admitted inventory.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.review_blueprint_planner import (
    DynamicReviewBlueprintPlanner,
)


@pytest.fixture()
def admission_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"grounding-admission-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _planner(admission_tmp: Path, *, served_text_limit: int | None = None):
    return DynamicReviewBlueprintPlanner(
        admission_tmp / "concepts.json",
        admission_tmp / "out",
        user_question="Compare radiative cooling mechanisms.",
        problem_understanding="Compare radiative cooling mechanisms.",
        scope_definition="Compare radiative cooling mechanisms.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        served_text_limit=served_text_limit,
    )


def _install_material_units(
    planner, chunk_ids: list[str], *, preview: str | None = None
) -> None:
    """Install realistic durable material units for semantic materialization."""
    planner.material_units = [
        {
            "unit_id": f"unit:{chunk_id}",
            "identity": {
                "chunk_id": chunk_id,
                "paper_id": f"paper:{chunk_id}",
                "doi": f"10.0000/{chunk_id}",
                "title": f"Title {chunk_id}",
                "locator": {"section": "results"},
            },
            "durable_content": {
                "normalized_text": preview
                or (
                    "Radiative cooling materials improve thermal management "
                    "of buildings under clear-sky conditions."
                ),
                "raw_text": "Raw source text.",
                "section_path": "results",
                "content_hash": f"hash:{chunk_id}",
            },
            "durable_content_card": {
                "content_quality": {
                    "source_kind": "s2_body_snippet",
                    "evidence_ceiling": "factual_support",
                    "context_complete": True,
                }
            },
            "audit": {
                "source_provenance": {
                    "use_permission": "factual_support",
                    "content_depth": "structured_snippet",
                }
            },
            "query_annotations": [
                {
                    "query_id": "query:admission-test",
                    "question_relevance": "substantial",
                    "propositions": [
                        {
                            "proposition_id": f"prop:{chunk_id}",
                            "statement": f"Proposition about {chunk_id}.",
                            "question_function": "comparison_input",
                            "evidence_permissions": {
                                chunk_id: "factual_support"
                            },
                        }
                    ],
                }
            ],
        }
        for chunk_id in chunk_ids
    ]


def _chunk(index: int, *, relevant: bool) -> dict:
    if relevant:
        preview = (
            "Radiative cooling materials improve thermal management of "
            "buildings under clear-sky conditions."
        )
    else:
        preview = (
            "Unrelated agricultural irrigation scheduling and soil moisture "
            "sensing in a different domain."
        )
    return {
        "chunk_id": f"c{index:03d}",
        "paper_id": f"p{index:03d}",
        "title": f"Paper {index}",
        "section_path": "results",
        "text_preview": preview,
        "material_binding_search_text": preview,
        "use_permission": "factual_support",
    }


def _score_map(strong_count: int, off_count: int) -> dict[str, float]:
    scores = {
        f"c{index:03d}": 0.85 for index in range(strong_count)
    }
    scores.update(
        {
            f"c{index:03d}": 0.05
            for index in range(strong_count, strong_count + off_count)
        }
    )
    return scores


def _architecture() -> dict:
    return {
        "sections": [
            {
                "section_id": "S01",
                "title": "Radiative cooling mechanisms",
                "argument_role": (
                    "Explain the governing physics of radiative cooling."
                ),
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
            }
        ]
    }


def test_semantic_admission_keeps_all_strong_and_excludes_off_topic(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    score_map = _score_map(strong_count=220, off_count=60)
    _install_material_units(
        planner, [f"c{index:03d}" for index in range(280)]
    )
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {
                "route": "material_semantic_cache",
                "model": "text-embedding-v4",
                "scored_units": len(score_map),
                "error": "",
            },
        ),
    )
    all_text = [
        _chunk(index, relevant=index < 220)
        for index in range(280)
    ]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )

    admitted_ids = {row["chunk_id"] for row in admitted}
    assert len(admitted) == 220
    assert admitted_ids == {f"c{index:03d}" for index in range(220)}
    assert not (admitted_ids & {f"c{index:03d}" for index in range(220, 280)})
    assert audit["route"] == "material_semantic_cache"
    assert audit["best_score"] == 0.85
    assert audit["threshold"] == 0.85
    assert audit["target_anchor_score"] == 0.85
    assert audit["tie_extension_count"] == 20
    assert audit["admitted_count"] == 220
    assert audit["excluded_count"] == 60
    assert audit["library_scores_count"] == 280
    assert audit["materialized_count"] == 280
    assert audit["advisory_status"] == "above_target_range"
    assert audit["score_distribution"]["admitted_ge_0_70"] == 220
    assert all(row["admission_route"] == "material_semantic_cache" for row in admitted)
    assert all(row["admission_score"] == 0.85 for row in admitted)


def test_semantic_failure_falls_back_to_audited_lexical_admission(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            {},
            {"route": "semantic_failed", "reason": "test_failure"},
        ),
    )
    all_text = [
        _chunk(index, relevant=index < 30)
        for index in range(80)
    ]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )

    assert audit["route"] == "lexical_material_card_fallback"
    assert audit["semantic_route"]["reason"] == "test_failure"
    admitted_ids = {row["chunk_id"] for row in admitted}
    assert len(admitted) == 30
    assert admitted_ids == {f"c{index:03d}" for index in range(30)}
    assert not (admitted_ids & {f"c{index:03d}" for index in range(30, 80)})
    assert audit["admitted_count"] == 30
    assert audit["zero_or_negative_count"] == 50
    assert audit["threshold_components"]["target_anchor_score"] > 0
    assert audit["score_distribution"]["excluded_below_threshold"] == 0
    assert all(row["admission_route"] == "lexical_material_card_fallback" for row in admitted)


def test_sparse_relevant_set_is_not_padded(admission_tmp: Path, monkeypatch) -> None:
    planner = _planner(admission_tmp)
    score_map = _score_map(strong_count=5, off_count=20)
    _install_material_units(
        planner, [f"c{index:03d}" for index in range(25)]
    )
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (score_map, {"route": "material_semantic_cache"}),
    )
    all_text = [_chunk(index, relevant=index < 5) for index in range(25)]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )

    assert len(admitted) == 5
    assert audit["admitted_count"] == 5
    assert audit["advisory_status"] == "below_target_range"
    assert audit["excluded_count"] == 20
    assert audit["policy"] == (
        "semantic score floor: max(absolute, best*relative, "
        "score at rank 200); every score >= threshold retained "
        "including ties; no top-N and no 200-item slice; sparse "
        "sections are not padded"
    )
    assert audit["threshold_components"]["target_anchor_score"] == 0.05
    assert audit["tie_extension_count"] == 0


def test_section_semantic_scores_read_material_vector_cache(
    admission_tmp: Path, monkeypatch
) -> None:
    import optomind_research.runtime.material_semantic_cache as cache_module

    planner = _planner(admission_tmp)
    cache_path = admission_tmp / "semantic_cache.sqlite"
    cache_path.touch()
    planner.material_vectors_path = cache_path
    planner.material_units = [
        {
            "unit_id": "unit:c001",
            # Realistic material units store unit_id at the top level only.
            "identity": {"chunk_id": "c001"},
            "durable_content": {"content_hash": "h1"},
        }
    ]
    usage = {"input_tokens": 12, "request_count": 1}
    monkeypatch.setattr(
        cache_module,
        "dashscope_embedder",
        lambda texts, **kwargs: [[0.1, 0.2]],
    )

    class FakeCache:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def count(self):
            return 1

        def search(self, vector, *, top_k, embedding_model, **kwargs):
            assert top_k == 1
            assert embedding_model == "text-embedding-v4"
            return [{"unit_id": "unit:c001", "score": 0.91}]

    monkeypatch.setattr(cache_module, "MaterialSemanticCache", FakeCache)
    monkeypatch.setattr(
        planner,
        "material_embedding_model",
        "text-embedding-v4",
    )
    scores, audit = planner._section_semantic_text_scores(
        "radiative cooling thermal management"
    )
    assert scores == {"c001": 0.91}
    assert audit["route"] == "material_semantic_cache"
    assert audit["model"] == "text-embedding-v4"
    assert audit["scored_units"] == 1
    assert audit["cache_units"] == 1
    assert audit["error"] == ""


def test_section_semantic_scores_map_top_level_unit_id_for_3449_units(
    admission_tmp: Path, monkeypatch
) -> None:
    import optomind_research.runtime.material_semantic_cache as cache_module

    planner = _planner(admission_tmp)
    cache_path = admission_tmp / "semantic_cache_3449.sqlite"
    cache_path.touch()
    planner.material_vectors_path = cache_path
    planner.material_units = [
        {
            "unit_id": f"unit:{index:04d}",
            "identity": {"chunk_id": f"c{index:04d}"},
            "durable_content": {"content_hash": f"h{index}"},
        }
        for index in range(3449)
    ]
    monkeypatch.setattr(
        cache_module,
        "dashscope_embedder",
        lambda texts, **kwargs: [[0.1, 0.2, 0.3]],
    )

    class FakeCache3449:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def count(self):
            return 3449

        def search(self, vector, *, top_k, embedding_model, **kwargs):
            assert top_k == 3449
            return [
                {
                    "unit_id": f"unit:{index:04d}",
                    "score": round(0.50 + (index % 100) * 0.002, 6),
                }
                for index in range(3449)
            ]

    monkeypatch.setattr(cache_module, "MaterialSemanticCache", FakeCache3449)
    scores, audit = planner._section_semantic_text_scores(
        "radiative cooling thermal management"
    )
    assert len(scores) == 3449
    assert audit["route"] == "material_semantic_cache"
    assert audit["scored_units"] == 3449
    assert audit["cache_units"] == 3449
    assert audit["error"] == ""
    assert scores["c0000"] == pytest.approx(0.50)


_S02_SCORE_ANCHORS = [
    (1, 0.756959),
    (100, 0.596883),
    (150, 0.569099),
    (175, 0.552461),
    (200, 0.539576),
    (250, 0.512104),
    (300, 0.492510),
    (400, 0.469846),
]


def _realistic_s02_curve(count: int = 400) -> list[float]:
    """Piecewise-linear surrogate of the real S02 score distribution."""
    curve: list[float] = []
    for rank in range(1, count + 1):
        lo_rank, lo_score = _S02_SCORE_ANCHORS[0]
        hi_rank, hi_score = _S02_SCORE_ANCHORS[-1]
        for (r0, s0), (r1, s1) in zip(
            _S02_SCORE_ANCHORS, _S02_SCORE_ANCHORS[1:]
        ):
            if r0 <= rank <= r1:
                lo_rank, lo_score, hi_rank, hi_score = r0, s0, r1, s1
                break
        fraction = (rank - lo_rank) / max(1, hi_rank - lo_rank)
        curve.append(round(lo_score + (hi_score - lo_score) * fraction, 6))
    return curve


def _realistic_score_map(
    strong_count: int = 400, off_count: int = 400
) -> dict[str, float]:
    curve = _realistic_s02_curve(strong_count)
    scores = {
        f"c{index:03d}": curve[index] for index in range(strong_count)
    }
    scores.update(
        {
            f"c{index:03d}": round(
                0.20 + (index - strong_count) * 0.0005, 6
            )
            for index in range(strong_count, strong_count + off_count)
        }
    )
    return scores


def test_realistic_s02_distribution_uses_semantic_route_and_admits_150_220(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    score_map = _realistic_score_map(strong_count=400, off_count=400)
    _install_material_units(
        planner, [f"c{index:03d}" for index in range(800)]
    )
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {
                "route": "material_semantic_cache",
                "model": "text-embedding-v4",
                "scored_units": 3449,
                "cache_units": 3449,
                "error": "",
            },
        ),
    )
    all_text = [
        _chunk(index, relevant=index < 400)
        for index in range(800)
    ]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )

    raw_threshold = 0.756959 * 0.72
    threshold = round(raw_threshold, 4)
    assert threshold == pytest.approx(0.5450, abs=0.0001)
    expected = sum(
        1 for score in _realistic_s02_curve() if score >= raw_threshold
    )
    assert 150 <= expected <= 220
    assert len(admitted) == expected
    assert audit["route"] == "material_semantic_cache"
    assert audit["semantic_route"]["scored_units"] == 3449
    assert audit["threshold"] == threshold
    assert audit["target_anchor_score"] == round(0.539576, 4)
    assert audit["threshold_components"]["target_anchor_score"] == round(
        0.539576, 4
    )
    assert audit["admitted_count"] == expected
    assert audit["excluded_count"] == 800 - expected
    assert audit["tie_extension_count"] == 0
    assert audit["library_scores_count"] == 800
    assert audit["materialized_count"] == 800
    admitted_ids = {row["chunk_id"] for row in admitted}
    assert not (admitted_ids & {f"c{index:03d}" for index in range(400, 800)})
    assert audit["score_distribution"]["admitted_under_0_50"] == 0
    assert (
        audit["score_distribution"]["excluded_below_threshold"]
        == 800 - expected
    )
    assert sum(audit["score_distribution"].values()) == 800
    assert audit["advisory_status"] == "within_target_range"


def test_all_over_200_candidates_survive_when_genuinely_above_threshold(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    score_map = {
        **{f"c{index:03d}": 0.60 for index in range(230)},
        **{f"c{index:03d}": 0.20 for index in range(230, 280)},
    }
    _install_material_units(
        planner, [f"c{index:03d}" for index in range(280)]
    )
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {"route": "material_semantic_cache", "scored_units": 280},
        ),
    )
    all_text = [
        _chunk(index, relevant=index < 230)
        for index in range(280)
    ]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )
    assert len(admitted) == 230
    assert audit["admitted_count"] == 230
    assert audit["threshold"] == 0.60
    assert audit["target_anchor_score"] == 0.60
    assert audit["tie_extension_count"] == 30
    assert audit["advisory_status"] == "above_target_range"


def test_full_library_semantic_rows_enter_even_when_not_in_global_all_text(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    chunk_ids = [f"c{index:03d}" for index in range(300)]
    _install_material_units(planner, chunk_ids)
    score_map = {chunk_id: 0.62 for chunk_id in chunk_ids}
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {"route": "material_semantic_cache", "scored_units": 300},
        ),
    )
    # Global planning inventory contains only 10 rows; the full library has
    # 300 scored units.  Admission must not be gated by all_text membership.
    all_text = [_chunk(index, relevant=True) for index in range(10)]
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", all_text
    )

    admitted_ids = {row["chunk_id"] for row in admitted}
    assert len(admitted) == 300
    assert set(chunk_ids) <= admitted_ids
    assert {"c000", "c009"} <= admitted_ids
    assert audit["route"] == "material_semantic_cache"
    assert audit["materialized_count"] == 300
    assert audit["library_scores_count"] == 300
    assert audit["threshold"] == 0.62
    assert audit["tie_extension_count"] == 100
    assert audit["supplement"]["admitted_count"] == 0


def _s06_flat_score_map(count: int = 926, tie_extra: int = 0) -> dict[str, float]:
    scores: dict[str, float] = {"c000": 0.55}
    for index in range(1, 200 + tie_extra):
        scores[f"c{index:03d}"] = 0.53
    for index in range(200 + tie_extra, count):
        scores[f"c{index:03d}"] = 0.51
    return scores


def test_flat_s06_like_distribution_stays_around_target(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    chunk_ids = [f"c{index:03d}" for index in range(926)]
    _install_material_units(planner, chunk_ids)
    score_map = _s06_flat_score_map(count=926, tie_extra=0)
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {"route": "material_semantic_cache", "scored_units": 926},
        ),
    )
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", []
    )

    assert len(admitted) == 200
    assert audit["route"] == "material_semantic_cache"
    assert audit["threshold"] == 0.53
    assert audit["target_anchor_score"] == 0.53
    assert audit["threshold_components"]["target_anchor_score"] == 0.53
    assert audit["tie_extension_count"] == 0
    assert audit["admitted_count"] == 200
    assert audit["excluded_count"] == 726
    assert audit["advisory_status"] == "within_target_range"
    assert all(
        row["admission_score"] >= audit["threshold"] for row in admitted
    )


def test_flat_s06_like_distribution_keeps_all_exact_ties_past_200(
    admission_tmp: Path, monkeypatch
) -> None:
    planner = _planner(admission_tmp)
    chunk_ids = [f"c{index:03d}" for index in range(926)]
    _install_material_units(planner, chunk_ids)
    score_map = _s06_flat_score_map(count=926, tie_extra=30)
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {"route": "material_semantic_cache", "scored_units": 926},
        ),
    )
    admitted, audit = planner._admit_section_text_candidates(
        "radiative cooling thermal management", []
    )

    assert len(admitted) == 230
    assert audit["admitted_count"] == 230
    assert audit["threshold"] == 0.53
    assert audit["target_anchor_score"] == 0.53
    assert audit["tie_extension_count"] == 30
    assert audit["excluded_count"] == 696
    assert len({row["admission_score"] for row in admitted}) == 2


def test_realistic_distribution_explicit_served_limit_stays_separate(
    admission_tmp: Path, monkeypatch
) -> None:
    score_map = _realistic_score_map(strong_count=400, off_count=400)
    _, grounded, _ = _ground_with_scores(
        admission_tmp,
        monkeypatch,
        served_text_limit=40,
        strong_count=400,
        off_count=400,
        score_map=score_map,
    )
    section = grounded["sections"][0]
    pool = section["candidate_material_pool"]
    raw_threshold = 0.756959 * 0.72
    expected = sum(
        1 for score in _realistic_s02_curve() if score >= raw_threshold
    )
    assert 150 <= expected <= 220
    assert pool["retained_candidate_count"] == expected
    assert len(pool["served_chunk_ids"]) == 40
    assert pool["explicit_limit_applied"] is True
    assert pool["explicit_limit_truncated"] is True


def _ground_with_scores(
    admission_tmp: Path,
    monkeypatch,
    *,
    served_text_limit: int | None,
    strong_count: int,
    off_count: int,
    score_map: dict[str, float] | None = None,
):
    from optomind_research import review_blueprint_planner as planner_module

    planner = _planner(admission_tmp, served_text_limit=served_text_limit)
    _install_material_units(
        planner,
        [f"c{index:03d}" for index in range(strong_count + off_count)],
    )
    if score_map is None:
        score_map = _score_map(strong_count=strong_count, off_count=off_count)
    monkeypatch.setattr(
        planner,
        "_section_semantic_text_scores",
        lambda query: (
            score_map,
            {
                "route": "material_semantic_cache",
                "model": "text-embedding-v4",
                "scored_units": len(score_map),
                "error": "",
            },
        ),
    )
    captured: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        captured.append(json.loads(messages[-1]["content"]))
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
    evidence = {
        "selected_concept_nodes": [],
        "retrieved_text_chunks": [
            _chunk(index, relevant=index < strong_count)
            for index in range(strong_count + off_count)
        ],
        "retrieved_visual_chunks": [],
    }
    grounded = planner._ground_blueprint_architecture(
        _architecture(), evidence
    )
    return planner, grounded, captured


def test_grounder_transport_is_admitted_inventory_not_global(
    admission_tmp: Path, monkeypatch
) -> None:
    _, grounded, captured = _ground_with_scores(
        admission_tmp,
        monkeypatch,
        served_text_limit=None,
        strong_count=220,
        off_count=580,
    )
    section = grounded["sections"][0]
    pool = section["candidate_material_pool"]
    strong_ids = {f"c{index:03d}" for index in range(220)}
    off_ids = {f"c{index:03d}" for index in range(220, 800)}
    assert set(pool["candidate_chunk_ids"]) == strong_ids
    assert set(pool["served_chunk_ids"]) == strong_ids
    assert not (set(pool["candidate_chunk_ids"]) & off_ids)
    assert section["_text_candidate_admission_audit"]["admitted_count"] == 220
    assert section["_text_candidate_admission_audit"]["route"] == (
        "material_semantic_cache"
    )
    menu = captured[0]["candidate_menu"]
    assert len(menu["text_chunks"]) == 220
    assert menu["evidence_digest"]["chunk_count"] == 220
    assert menu["text_inventory_policy"]["admission_route"] == (
        "material_semantic_cache"
    )


def test_explicit_served_limit_caps_admitted_inventory_audibly(
    admission_tmp: Path, monkeypatch
) -> None:
    _, grounded, _ = _ground_with_scores(
        admission_tmp,
        monkeypatch,
        served_text_limit=40,
        strong_count=220,
        off_count=60,
    )
    section = grounded["sections"][0]
    pool = section["candidate_material_pool"]
    assert len(pool["candidate_chunk_ids"]) == 220
    assert len(pool["served_chunk_ids"]) == 40
    assert pool["served_limit"] == 40
    assert pool["explicit_limit_applied"] is True
    assert pool["explicit_limit_truncated"] is True
    assert pool["retained_candidate_count"] == 220
    audit = section["_text_candidate_admission_audit"]
    assert audit["admitted_count"] == 220
    assert audit["threshold"] > 0
