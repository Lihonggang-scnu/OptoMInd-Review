"""Shared pytest fixtures for blueprint acceptance tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_visual_chunks_tagged():
    """Eight mock visual chunks — one per valid visual_argument_type (8-type protocol)."""
    from optomind_research.visual_argument_alignment import VALID_VISUAL_ARGUMENT_TYPES
    chunks = []
    for i, vtype in enumerate(sorted(VALID_VISUAL_ARGUMENT_TYPES)):
        chunks.append({
            "chunk_id": f"doi-10.100{i}-test.{i:03d}:visual:v{i:04d}",
            "doi": f"doi-10.100{i}-test.{i:03d}",
            "title": f"Test paper {i}: spectral emissivity study",
            "caption": f"Fig. {i+1}. Example of {vtype} in radiative cooling context.",
            "visual_argument_type": vtype,
            "visual_argument_status": "ok",
            "visual_argument_confidence": "high",
            "visual_argument_claim": f"Mock claim for {vtype}",
            "visual_argument_needs_human_review": False,
            "visual_argument_schema_version": "v1",
        })
    return chunks


@pytest.fixture
def sample_visual_blueprint_json():
    """Blueprint with candidate_visual_chunks for M4 section/claim mapping tests."""
    from optomind_research.claim_decomposer import decompose_blueprint

    def _make_visual_section(section_id: str, chunk_ids: list[str], visual_chunks: list[dict]) -> dict:
        return {
            "section_id": section_id,
            "title": f"Visual test section {section_id}: emissivity and radiative cooling",
            "argument_role": f"Evidence role for {section_id}.",
            "candidate_text_chunk_ids": [f"doi-10.9000-text.001:hybrid:s{j:04d}" for j in range(3)],
            "candidate_text_chunks": [
                {"chunk_id": f"doi-10.9000-text.001:hybrid:s{j:04d}",
                 "text_preview": "emissivity 8-13 μm radiative cooling atmospheric window.",
                 "section_path": "Results"}
                for j in range(3)
            ],
            "candidate_visual_chunks": visual_chunks,
            "candidate_visual_chunk_ids": [vc["chunk_id"] for vc in visual_chunks],
            "claim_graph_seed": {
                "central_claim_candidates": [
                    {
                        "claim_seed_id": f"{section_id}-seed-1",
                        "claim_seed": f"SiO2 emissivity >0.9 in {section_id}",
                        "supporting_text_chunk_ids": [f"doi-10.9000-text.001:hybrid:s0001"],
                        "supporting_visual_chunk_ids": [],
                        "status": "planning_seed_not_final_claim",
                    },
                ],
                "relation_types_to_check": ["support"],
                "claim_binding_rule": "Binder must attach each claim to exact text.",
            },
        }

    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            _make_visual_section("S01", [], [
                {"chunk_id": "vc-mech-001", "visual_argument_type": "mechanism_anchor", "visual_argument_status": "ok"},
                {"chunk_id": "vc-mech-002", "visual_argument_type": "mechanism_anchor", "visual_argument_status": "ok"},
                {"chunk_id": "vc-quant-001", "visual_argument_type": "quantitative_comparison", "visual_argument_status": "ok"},
                {"chunk_id": "vc-trend-001", "visual_argument_type": "trend_or_parameter_map", "visual_argument_status": "ok"},
            ]),
            _make_visual_section("S02", [], [
                {"chunk_id": "vc-rep-001", "visual_argument_type": "representative_example", "visual_argument_status": "ok"},
            ]),
            _make_visual_section("S03", [], []),  # no visual chunks → no_visual_support
        ],
    }
    return decompose_blueprint(blueprint, real_llm=False)


@pytest.fixture
def sample_dag_json():
    """Build a deterministic mock DAG over mock claims (real_llm=False)."""
    from optomind_research.claim_decomposer import decompose_blueprint
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            _make_section("S01", [
                "doi-10.1000-test.001:hybrid:s0001",
                "doi-10.1000-test.001:hybrid:s0002",
                "doi-10.1001-test.002:hybrid:s0001",
            ]),
            _make_section("S02", [
                "doi-10.1000-test.001:hybrid:s0003",
                "doi-10.1001-test.002:hybrid:s0002",
                "doi-10.1002-test.003:hybrid:s0001",
            ]),
            _make_section("S03", [
                "doi-10.1001-test.002:hybrid:s0003",
                "doi-10.1002-test.003:hybrid:s0002",
                "doi-10.1003-test.004:hybrid:s0001",
            ]),
        ],
    }
    blueprint = decompose_blueprint(blueprint, real_llm=False)

    all_claims: list[dict] = []
    section_order: list[str] = []
    for s in blueprint["sections"]:
        section_order.append(s["section_id"])
        for c in s.get("claims", []):
            c.setdefault("section_id", s["section_id"])
            all_claims.append(c)

    builder = ArgumentDAGBuilder(real_llm=False)
    dag = builder.build(all_claims, section_order)
    dag.propagate_saturation()
    return dag.to_dict()


@pytest.fixture
def sample_blueprint_json():
    """Minimal blueprint with claims already decomposed (mock mode)."""
    from optomind_research.claim_decomposer import decompose_blueprint

    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            _make_section("S01", [
                "chunk_S01_01", "chunk_S01_02", "chunk_S01_03",
                "chunk_S01_04", "chunk_S01_05",
            ]),
            _make_section("S02", [
                "chunk_S02_01", "chunk_S02_02", "chunk_S02_03",
                "chunk_S02_04",
            ]),
            _make_section("S03", [
                "chunk_S03_01", "chunk_S03_02", "chunk_S03_03",
            ]),
        ],
    }
    return decompose_blueprint(blueprint, real_llm=False)


@pytest.fixture
def sample_gap_blueprint_json():
    """Blueprint for gap-resolution tests.

    Claims start with saturation < 1.5 (single DOI), but each section's
    candidate pool includes chunks from 3+ different DOIs so that mock
    resolution can add distinct-DOI chunks and raise saturation to ≥ 1.5.
    """
    from optomind_research.claim_decomposer import decompose_blueprint

    def _gap_section(section_id: str, primary: list[str], extras: list[str]) -> dict:
        all_ids = primary + extras
        return {
            "section_id": section_id,
            "title": f"Gap test {section_id}: radiative cooling emissivity and spectral control",
            "argument_role": f"Evidence role for {section_id} in gap-resolution acceptance test.",
            "candidate_text_chunk_ids": all_ids,
            "candidate_text_chunks": [
                {
                    "chunk_id": cid,
                    "title": f"Paper on {cid[:30]}",
                    "text_preview": (
                        "emissivity spectral selective radiative cooling atmospheric window "
                        f"8-13 μm thermal management subambient. [{cid}]"
                    ),
                    "section_path": "Results / Spectral characterization",
                }
                for cid in all_ids
            ],
            "candidate_visual_chunks": [],
            "claim_graph_seed": {
                "central_claim_candidates": [
                    {
                        "claim_seed_id": f"{section_id}-seed-1",
                        "claim_seed": (
                            f"SiO2 emitters in {section_id} show emissivity >0.9 "
                            "across the 8-13 μm atmospheric window"
                        ),
                        # Only primary DOI → saturation = 1.0 after decomposition
                        "supporting_text_chunk_ids": primary[:2],
                        "supporting_visual_chunk_ids": [],
                        "status": "planning_seed_not_final_claim",
                    },
                    {
                        "claim_seed_id": f"{section_id}-seed-2",
                        "claim_seed": (
                            f"Subambient cooling of 5-10 K requires solar reflectance "
                            f">0.95 and thermal emittance >0.9 ({section_id})"
                        ),
                        "supporting_text_chunk_ids": primary[:1],
                        "supporting_visual_chunk_ids": [],
                        "status": "planning_seed_not_final_claim",
                    },
                    {
                        "claim_seed_id": f"{section_id}-seed-3",
                        "claim_seed": (
                            f"Polymer coolers outperform SiO2 in roll-to-roll scalability ({section_id})"
                        ),
                        "supporting_text_chunk_ids": primary,
                        "supporting_visual_chunk_ids": [],
                        "status": "planning_seed_not_final_claim",
                    },
                ],
                "relation_types_to_check": ["support", "contrast", "refine"],
                "claim_binding_rule": "Binder must attach each claim to exact text.",
            },
        }

    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            _gap_section(
                "S01",
                primary=["doi-10.1001-sio2.001:hybrid:s0001", "doi-10.1001-sio2.001:hybrid:s0002"],
                extras=["doi-10.1002-poly.002:hybrid:s0001", "doi-10.1003-photo.003:hybrid:s0001"],
            ),
            _gap_section(
                "S02",
                primary=["doi-10.1004-meas.004:hybrid:s0001"],
                extras=["doi-10.1005-comp.005:hybrid:s0001", "doi-10.1006-app.006:hybrid:s0001"],
            ),
            _gap_section(
                "S03",
                primary=["doi-10.1007-mech.007:hybrid:s0001", "doi-10.1007-mech.007:hybrid:s0002"],
                extras=["doi-10.1008-eval.008:hybrid:s0001", "doi-10.1009-sys.009:hybrid:s0001"],
            ),
        ],
    }
    return decompose_blueprint(blueprint, real_llm=False)


def _make_section(section_id: str, chunk_ids: list[str]) -> dict:
    return {
        "section_id": section_id,
        "title": f"Test section {section_id}: emissivity and thermal control mechanisms",
        "argument_role": f"Explain the role of {section_id} evidence in the review argument.",
        "candidate_text_chunk_ids": chunk_ids,
        "candidate_text_chunks": [
            {
                "chunk_id": cid,
                "text_preview": (
                    f"SiO2 thin film at 8-13 μm shows emissivity of 0.92 in atmospheric window. "
                    f"Subambient cooling achieved 8 K below ambient. "
                    f"Measurement via FTIR spectroscopy confirms spectral selectivity. [{cid}]"
                ),
                "section_path": "Results / Spectral characterization",
            }
            for cid in chunk_ids
        ],
        "claim_graph_seed": {
            "central_claim_candidates": [
                {
                    "claim_seed_id": f"{section_id}-seed-1",
                    "claim_seed": (
                        f"SiO2-based emitters in {section_id} show emissivity >0.9 "
                        f"across the 8-13 μm atmospheric window"
                    ),
                    "supporting_text_chunk_ids": chunk_ids[:2],
                    "supporting_visual_chunk_ids": [],
                    "status": "planning_seed_not_final_claim",
                },
                {
                    "claim_seed_id": f"{section_id}-seed-2",
                    "claim_seed": (
                        f"Subambient cooling of 5-10 K requires simultaneous solar "
                        f"reflectance >0.95 and thermal emittance >0.9 ({section_id})"
                    ),
                    "supporting_text_chunk_ids": chunk_ids[1:3],
                    "supporting_visual_chunk_ids": [],
                    "status": "planning_seed_not_final_claim",
                },
                {
                    "claim_seed_id": f"{section_id}-seed-3",
                    "claim_seed": (
                        f"Polymer-based radiative coolers outperform SiO2 in scalability "
                        f"for roll-to-roll manufacturing ({section_id})"
                    ),
                    "supporting_text_chunk_ids": chunk_ids[2:4] if len(chunk_ids) > 2 else chunk_ids,
                    "supporting_visual_chunk_ids": [],
                    "status": "planning_seed_not_final_claim",
                },
                {
                    "claim_seed_id": f"{section_id}-seed-4",
                    "claim_seed": (
                        f"Photonic multilayer structures achieve spectral selectivity "
                        f"exceeding bulk emitters by 15-30% ({section_id})"
                    ),
                    "supporting_text_chunk_ids": chunk_ids[:1],
                    "supporting_visual_chunk_ids": [],
                    "status": "planning_seed_not_final_claim",
                },
            ],
            "relation_types_to_check": ["support", "contrast", "refine"],
            "claim_binding_rule": "A later evidence binder must attach each claim to exact text.",
        },
    }


@pytest.fixture
def sample_kb_visual_chunks():
    """Pool of mock visual chunks simulating a small KB — diverse DOIs, all 8 types."""
    from optomind_research.visual_argument_alignment import VALID_VISUAL_ARGUMENT_TYPES
    chunks = []
    types = sorted(VALID_VISUAL_ARGUMENT_TYPES)
    for i, vtype in enumerate(types):
        # Make tokens overlap with section query words used in sample_no_visual_blueprint
        chunks.append({
            "chunk_id": f"doi-10.200{i}-kb.{i:03d}:visual:v{i:04d}",
            "doi": f"doi-10.200{i}-kb.{i:03d}",
            "title": f"Emissivity measurement study {i}",
            "caption": (
                f"Fig. {i+1}. Spectral emissivity of SiO2 thin film showing "
                f"{vtype.replace('_', ' ')} in radiative cooling context."
            ),
            "visual_argument_type": vtype,
            "visual_argument_status": "ok",
            "visual_argument_confidence": "high",
            "visual_argument_claim": (
                f"SiO2 emissivity exceeds 0.9 in 8-13 μm atmospheric window ({vtype})"
            ),
            "visual_argument_needs_human_review": False,
            "visual_argument_schema_version": "v1",
        })
    return chunks


@pytest.fixture
def sample_no_visual_blueprint():
    """Blueprint where ALL sections have NO candidate_visual_chunks — triggers auto-recommend."""
    from optomind_research.claim_decomposer import decompose_blueprint

    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            {
                "section_id": "S01",
                "title": "Spectral emissivity of SiO2 radiative cooling films",
                "argument_role": (
                    "Establish mechanism: SiO2 emits in the 8-13 μm atmospheric window "
                    "via phonon-polariton resonance, enabling subambient cooling."
                ),
                "candidate_text_chunk_ids": ["tc-001", "tc-002"],
                "candidate_text_chunks": [
                    {"chunk_id": "tc-001",
                     "text_preview": "SiO2 thin film emissivity 0.92 in 8-13 μm atmospheric window.",
                     "section_path": "Results"},
                    {"chunk_id": "tc-002",
                     "text_preview": "Subambient cooling 8 K below ambient measured by FTIR spectroscopy.",
                     "section_path": "Results"},
                ],
                "candidate_visual_chunks": [],
                "candidate_visual_chunk_ids": [],
                "claim_graph_seed": {
                    "central_claim_candidates": [
                        {
                            "claim_seed_id": "S01-seed-1",
                            "claim_seed": "SiO2 emissivity exceeds 0.9 across 8-13 μm window",
                            "supporting_text_chunk_ids": ["tc-001"],
                            "supporting_visual_chunk_ids": [],
                            "status": "planning_seed_not_final_claim",
                        }
                    ],
                    "relation_types_to_check": ["support"],
                    "claim_binding_rule": "Binder must attach each claim to exact text.",
                },
            },
        ],
    }
    return decompose_blueprint(blueprint, real_llm=False)
