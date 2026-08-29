from __future__ import annotations

from optomind_research.literature_resource_builder import (
    AbstractPaperRecord,
    LiteratureResourceBuilder,
)
from optomind_research.concept_map_builder import ConceptMapInputs, VisualAwareConceptMapBuilder
import sqlite3


def _metalens_plan() -> dict:
    return {
        "input": {"user_query": "Chinese source input retained only at the boundary."},
        "output": {
            "problem_understanding": "Survey broadband achromatic metalenses and their dispersion compensation mechanisms.",
            "scope_definition": {
                "main_scope": "Achromatic metalens physics, imaging, and manufacturing.",
                "scope_items": ["Group delay engineering", "Large-aperture metalens fabrication"],
            },
            "keyword_decomposition": {
                "keywords": [
                    "broadband achromatic metalens",
                    "achromatic metasurface lens",
                    "metalens group delay dispersion",
                    "metalens imaging efficiency",
                    "large aperture metalens fabrication",
                ]
            },
        },
    }


def test_topic_anchor_tokens_keep_scientific_identity():
    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    anchors = builder.query_topic_anchor_tokens(_metalens_plan())
    assert "metalens" in anchors
    assert "achromatic" in anchors
    assert "review" not in anchors
    assert "fabrication" not in anchors


def test_tokenizer_preserves_initial_capital_letter():
    from optomind_research.literature_resource_builder import tokenize
    assert tokenize("Acoustic Metalens") == ["acoustic", "metalens"]


def test_supplemental_facets_reject_other_domains():
    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    plan = {
        "supplemental_facet_plan": {
            "supplemental_features": [
                {
                    "feature_name": "Metalens tolerance to fabrication errors",
                    "description": "Achromatic metalens phase errors under process variation.",
                    "retrieval_terms": ["metalens fabrication tolerance"],
                    "positive_keywords": ["achromatic metalens"],
                },
                {
                    "feature_name": "Passive radiative cooling films",
                    "description": "Transparent cooling polymers for buildings.",
                    "retrieval_terms": ["radiative cooling polymer film"],
                    "positive_keywords": ["daytime cooling"],
                },
                {
                    "feature_name": "Microbial fuel-cell scale-up",
                    "description": "Wastewater energy recovery reactors.",
                    "retrieval_terms": ["microbial fuel cell"],
                    "positive_keywords": ["wastewater reactor"],
                },
            ]
        }
    }
    filtered = builder.filter_supplemental_facets_by_topic(plan, _metalens_plan())
    root = filtered["supplemental_facet_plan"]
    assert [row["feature_name"] for row in root["supplemental_features"]] == [
        "Metalens tolerance to fabrication errors"
    ]
    assert root["topic_gate"]["rejected"] == 2


def test_primary_facet_plan_rejects_prompt_example_leakage():
    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    plan = {
        "atomic_relevance_plan": {
            "atomic_features": [
                {
                    "feature_id": "F01",
                    "feature_name": "Metalens group-delay engineering",
                    "description": "Dispersion compensation in achromatic metalenses.",
                    "positive_keywords": ["achromatic metalens"],
                    "retrieval_terms": ["metalens group delay"],
                },
                {
                    "feature_id": "F02",
                    "feature_name": "Broadband solar reflection in cooling films",
                    "description": "Passive daytime radiative cooling polymers.",
                    "positive_keywords": ["radiative cooling"],
                    "retrieval_terms": ["solar reflective cooling film"],
                },
                {
                    "feature_id": "F03",
                    "feature_name": "Acoustic achromatic metasurface",
                    "description": "Achromatic acoustic beam engineering.",
                    "positive_keywords": ["acoustic achromatic metasurface"],
                    "retrieval_terms": ["acoustic metalens"],
                },
            ]
        }
    }
    filtered = builder.filter_atomic_plan_by_topic(plan, _metalens_plan())
    root = filtered["atomic_relevance_plan"]
    assert [row["feature_id"] for row in root["atomic_features"]] == ["F01"]
    assert root["topic_gate"]["rejected"] == 2


def test_optical_domain_guard_rejects_pure_acoustic_analogue():
    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    guard = builder.query_domain_guard(_metalens_plan())
    assert builder.text_has_domain_conflict(
        "Achromatic acoustic metasurface for beam steering",
        "An elastic-wave device for sound focusing.",
        guard,
    )
    assert not builder.text_has_domain_conflict(
        "Comparison of optical and acoustic metasurfaces",
        "A cross-domain methods paper.",
        guard,
    )


def test_persistent_library_channels_cannot_override_topic_identity():
    relevant = AbstractPaperRecord(
        paper_id="metalens-paper",
        title="Broadband achromatic metalens imaging by group-delay engineering",
        abstract="A metasurface lens compensates chromatic dispersion across the visible band.",
        doi="10.0000/metalens",
        citation_count=10,
        open_access=True,
        topic_tags=["scholar_facet:old-run:SF01"],
    )
    cooling = AbstractPaperRecord(
        paper_id="cooling-paper",
        title="Passive daytime radiative cooling polymer films",
        abstract="Transparent thermal emitters cool buildings under sunlight.",
        doi="10.0000/cooling",
        citation_count=1000,
        open_access=True,
        topic_tags=["scholar_facet:old-run:SF01", "facet_role:citation_landmark"],
    )
    microbial = AbstractPaperRecord(
        paper_id="microbial-paper",
        title="Microbial fuel cell scale-up for wastewater treatment",
        abstract="Reactor optimization improves energy recovery.",
        doi="10.0000/microbial",
        citation_count=500,
        open_access=True,
        topic_tags=["facet_role:review_perspective"],
    )

    class FakeLibrary:
        def search_abstracts(self, terms, limit):
            return [relevant, cooling, microbial]

        def all_abstracts(self, limit):
            return [relevant, cooling, microbial]

    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    builder.library = FakeLibrary()
    builder.target_journals = []
    builder._emit = lambda *args, **kwargs: None
    selected = builder.select_relevant_abstract_pool(
        _metalens_plan(), limit=10, atomic_plan=None
    )
    assert [paper.paper_id for paper in selected] == ["metalens-paper"]


def test_fulltext_upgrade_cannot_reintroduce_off_topic_facet_candidate():
    cooling = AbstractPaperRecord(
        paper_id="cooling-paper",
        title="Passive daytime radiative cooling polymer films",
        abstract="Transparent emitters cool buildings.",
        doi="10.0000/cooling",
        open_access=True,
    )

    class FakeLibrary:
        def get_abstract(self, paper_id):
            return cooling if paper_id == cooling.paper_id else None

    builder = LiteratureResourceBuilder.__new__(LiteratureResourceBuilder)
    builder.library = FakeLibrary()
    builder.target_journals = []
    builder.diagnostics = []
    atomic_plan = {
        "atomic_relevance_plan": {
            "topic_gate": {"anchor_tokens": ["metalens", "achromatic", "metasurface"]},
            "atomic_features": [{
                "feature_id": "F01",
                "feature_name": "Achromatic metalens physics",
                "feature_type": "mechanism",
                "description": "Metalens dispersion.",
                "positive_keywords": ["achromatic metalens"],
                "negative_keywords": [],
                "retrieval_terms": ["metalens dispersion"],
                "weight": 0.8,
            }],
        }
    }
    facet_map = {"facets": [{
        "facet_id": "F01",
        "facet_name": "Achromatic metalens physics",
        "citation_landmark_papers": [{"paper_id": "cooling-paper"}],
        "review_perspective_papers": [],
        "recent_frontier_papers": [],
    }]}
    result = builder.decide_fulltext_upgrade(
        [], atomic_plan, [], query_plan=_metalens_plan(),
        facet_literature_map=facet_map, overall_top_n=10
    )
    assert result["selected_for_fulltext_upgrade"] == []


def test_concept_map_specs_come_from_current_kb_not_old_topic(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE concepts (kind TEXT, label TEXT, source_count INTEGER)")
    conn.executemany(
        "INSERT INTO concepts VALUES ('retrieval_feature', ?, ?)",
        [
            ("F01: Group delay mechanisms in achromatic metalenses", 8),
            ("F02: Practical imaging system integration gaps", 6),
            ("F03: Large-aperture fabrication and scalability limits", 5),
        ],
    )
    builder = VisualAwareConceptMapBuilder(ConceptMapInputs(kb_dir=tmp_path), tmp_path / "out")
    builder.conn = conn
    specs = sum((builder._retrieval_feature_specs(role) for role in ("mechanism", "application", "bottleneck")), [])
    joined = " ".join(row["label"].lower() for row in specs)
    assert "metalens" in joined
    assert "radiative cooling" not in joined
    assert "greenhouse" not in joined
