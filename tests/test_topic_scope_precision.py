from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "optomind_research" / "runtime"
if "optomind_research.runtime" not in sys.modules:
    runtime_package = types.ModuleType("optomind_research.runtime")
    runtime_package.__path__ = [str(RUNTIME_DIR)]
    sys.modules["optomind_research.runtime"] = runtime_package
if "ftfy" not in sys.modules:
    try:
        import ftfy  # noqa: F401
    except ModuleNotFoundError:
        ftfy_module = types.ModuleType("ftfy")
        ftfy_module.fix_text = lambda value: value
        sys.modules["ftfy"] = ftfy_module

from optomind_research.runtime.topic_scoped_kb_stage import (  # noqa: E402
    build_topic_scoped_kb,
    derive_topic_scope_contract,
)
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk  # noqa: E402


QUESTION = (
    "Principles, fabrication methods, and application progress of metasurfaces "
    "in flat optics and imaging"
)


def _query_plan() -> dict:
    return {
        "input": {"user_query": QUESTION},
        "output": {
            "scope_definition": {
                "main_scope": "Metasurface principles, nanofabrication, flat optics, and imaging applications.",
                "scope_items": [
                    "Electromagnetic phase control",
                    "Metalens and computational imaging",
                    "Scalable nanofabrication",
                ],
            },
            "lenses": ["electromagnetic principles", "nanofabrication", "imaging performance"],
            "inclusion_boundaries": [
                "metasurface imaging",
                "metalens fabrication",
                "metasurface holography",
            ],
            "exclusion_boundaries": [],
            "keyword_decomposition": {
                "keywords": [
                    "metasurface imaging",
                    "dielectric metasurface",
                    "metalens fabrication",
                    "metasurface holography",
                    "metasurface polarization control",
                ]
            },
        },
    }


def _object_specific_inverse_design_plan(*, extra_notes: str = "") -> dict:
    question = (
        "How do physics-informed neural networks and differentiable electromagnetic "
        "solvers compare for inverse design of nanophotonic metasurfaces?"
    )
    return {
        "input": {"user_query": question},
        "output": {
            "problem_understanding": "Compare methods for nanophotonic metasurface inverse design.",
            "scope_definition": {
                "main_scope": "Nanophotonic metasurface inverse design.",
                "scope_items": [
                    "Physics-informed neural networks for nanophotonic metasurfaces",
                    "Differentiable electromagnetic solvers for metasurface design",
                ],
            },
            "keyword_decomposition": {
                "keywords": [
                    "physics-informed neural network nanophotonic inverse design",
                    "differentiable electromagnetic solver metasurface design",
                ]
            },
            "extra_notes": extra_notes,
        },
    }


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    query_path = tmp_path / "query_plan.json"
    query_path.write_text(
        json.dumps(_query_plan(), ensure_ascii=False), encoding="utf-8"
    )
    policy_path = tmp_path / "s2_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 1],
                    "graph_depth": 0,
                },
                "graph": {
                    "seed_count": 0,
                    "reference_limit_per_seed": 0,
                    "citation_limit_per_seed": 0,
                    "recommendation_limit": 0,
                },
                "evidence": {
                    "minimum_factual_papers": 1,
                    "minimum_factual_chunks": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return query_path, policy_path


def _raw(*, abstract: str, route: str = "legacy_fixture") -> str:
    return json.dumps(
        {
            "abstract": abstract,
            "content_depth": "fulltext",
            "use_permission": "factual_support",
            "scope_fit": "direct",
            "context_complete": True,
            "provenance": {"discovery_route": route},
        },
        ensure_ascii=False,
    )


def _make_base(
    tmp_path: Path,
    *,
    papers: list[tuple[str, str, str]],
    chunks: list[tuple[str, str, str, str]],
    visuals: bool = False,
) -> Path:
    path = tmp_path / "base.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE papers(
            paper_id TEXT PRIMARY KEY,
            doi TEXT,
            title TEXT,
            year INTEGER,
            venue TEXT,
            quality_tier TEXT,
            query_relevance TEXT,
            search_text TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE text_chunks(
            chunk_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            doi TEXT,
            title TEXT,
            ordinal INTEGER,
            section_path TEXT,
            char_start INTEGER,
            char_end INTEGER,
            char_count INTEGER,
            boilerplate_score REAL,
            text TEXT,
            search_text TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE concepts(label TEXT);
        """
    )
    if visuals:
        conn.executescript(
            """
            CREATE TABLE visual_assets(
                asset_id TEXT PRIMARY KEY,
                paper_id TEXT NOT NULL,
                title TEXT,
                label TEXT,
                caption TEXT,
                search_text TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE visual_chunks(
                chunk_id TEXT PRIMARY KEY,
                paper_id TEXT NOT NULL,
                title TEXT,
                visual_role TEXT,
                caption TEXT,
                search_text TEXT,
                raw_json TEXT NOT NULL
            );
            """
        )
    for paper_id, title, abstract in papers:
        conn.execute(
            "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
            (
                paper_id,
                f"10.1000/{paper_id}",
                title,
                2025,
                "Fixture Journal",
                "fixture",
                "unreviewed",
                abstract,
                _raw(abstract=abstract),
            ),
        )
    for chunk_id, paper_id, section, text in chunks:
        raw = json.dumps(
            {
                "content_depth": "fulltext",
                "use_permission": "factual_support",
                "scope_fit": "direct",
                "context_complete": True,
            }
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chunk_id,
                paper_id,
                f"10.1000/{paper_id}",
                "Fixture paper",
                0,
                section,
                0,
                len(text),
                len(text),
                0.0,
                text,
                text,
                raw,
            ),
        )
    if visuals:
        conn.execute(
            "INSERT INTO visual_assets VALUES (?,?,?,?,?,?,?)",
            (
                "asset-on-topic",
                "on-topic",
                "Metasurface imaging schematic",
                "Figure 1",
                "Optical path of the metasurface imaging device.",
                "metasurface imaging schematic",
                json.dumps({"content_depth": "fulltext"}),
            ),
        )
        conn.execute(
            "INSERT INTO visual_assets VALUES (?,?,?,?,?,?,?)",
            (
                "asset-cooling",
                "cooling-0",
                "Radiative cooling schematic",
                "Figure 1",
                "Thermal emission path.",
                "radiative cooling schematic",
                json.dumps({"content_depth": "fulltext"}),
            ),
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?)",
            (
                "visual-chunk-on-topic",
                "on-topic",
                "Metasurface imaging schematic",
                "schematic",
                "Optical path.",
                "metasurface imaging",
                json.dumps({"content_depth": "fulltext"}),
            ),
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES (?,?,?,?,?,?,?)",
            (
                "visual-chunk-cooling",
                "cooling-0",
                "Radiative cooling schematic",
                "schematic",
                "Thermal path.",
                "radiative cooling",
                json.dumps({"content_depth": "fulltext"}),
            ),
        )
    conn.commit()
    conn.close()
    return path


def _run(
    tmp_path: Path,
    *,
    papers: list[tuple[str, str, str]],
    chunks: list[tuple[str, str, str, str]],
    visuals: bool = False,
    current_papers: list[S2PaperRecord] | None = None,
    current_chunks: list[UnifiedTextChunk] | None = None,
) -> dict:
    query_path, policy_path = _write_inputs(tmp_path)
    base_path = _make_base(
        tmp_path, papers=papers, chunks=chunks, visuals=visuals
    )
    return build_topic_scoped_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base_path,
        work_dir=tmp_path / "run",
        policy_path=policy_path,
        papers=current_papers or [],
        chunks=current_chunks or [],
    )


def test_radiative_cooling_corpus_is_fail_closed_at_paper_level(tmp_path: Path):
    papers = [
        (
            f"cooling-{index}",
            "Designer metasurface for passive radiative cooling",
            "A structured polymer metasurface emits through the atmospheric window "
            "to reduce temperature and improve daytime thermal management.",
        )
        for index in range(12)
    ]
    chunks = [
        (
            f"cooling-chunk-{index}",
            f"cooling-{index}",
            "Results",
            "Radiative cooling performance, emissivity, and outdoor temperature drop.",
        )
        for index in range(12)
    ]
    result = _run(tmp_path, papers=papers, chunks=chunks)

    assert result["status"] == "needs_more_literature"
    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    assert overlay.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
    assert overlay.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
    overlay.close()

    manifest = json.loads(
        (tmp_path / "run" / "KB_MANIFEST.json").read_text(encoding="utf-8")
    )
    paper_selection = manifest["selection"]["papers"]
    assert paper_selection["source_count"] == 12
    assert paper_selection["selected_count"] == 0
    assert paper_selection["rejected_count"] == 12
    assert paper_selection["selected_paper_ids"] == []
    assert "no_base_papers_passed_topic_contract" in manifest["audit"]["paper_scope"]["contamination_indicators"]["flags"]
    assert all(
        sample["reason"] != "accepted_by_core_anchor_score"
        for sample in paper_selection["rejected_paper_samples"]
    )


def test_on_topic_paper_survives_and_chunks_visuals_follow_parent(tmp_path: Path):
    papers = [
        (
            "cooling-0",
            "Metasurface thermal emitter for radiative cooling",
            "A thermal emitter improves atmospheric-window emissivity for daytime cooling.",
        ),
        (
            "on-topic",
            "Dielectric metasurface metalens for computational imaging",
            "We fabricate a dielectric metasurface metalens for computational imaging. "
            "Subwavelength nanoantennas provide polarization control, and electron-beam "
            "lithography fabricates the flat-optical device.",
        ),
    ]
    chunks = [
        (
            "cooling-chunk",
            "cooling-0",
            "Results",
            "Radiative cooling and thermal emission measurements.",
        ),
        (
            "on-topic-evidence",
            "on-topic",
            "Mechanism",
            "The dielectric metasurface metalens maps phase to the focal field for imaging.",
        ),
        (
            "on-topic-generic",
            "on-topic",
            "Methods",
            "The samples were rinsed, dried, and measured under the same conditions.",
        ),
    ]
    result = _run(tmp_path, papers=papers, chunks=chunks, visuals=True)

    assert result["status"] == "completed"
    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    assert [row[0] for row in overlay.execute("SELECT paper_id FROM papers")] == [
        "on-topic"
    ]
    assert {
        row[0]
        for row in overlay.execute("SELECT chunk_id FROM text_chunks")
    } == {"on-topic-evidence", "on-topic-generic"}
    assert [row[0] for row in overlay.execute("SELECT asset_id FROM visual_assets")] == [
        "asset-on-topic"
    ]
    assert [
        row[0] for row in overlay.execute("SELECT chunk_id FROM visual_chunks")
    ] == ["visual-chunk-on-topic"]
    overlay.close()

    selection = result["selection"]["papers"]
    assert selection["selected_count"] == 1
    accepted = [item for item in selection["paper_decisions"] if item["accepted"]]
    assert accepted[0]["paper_id"] == "on-topic"
    assert accepted[0]["reason"] == "accepted_by_core_anchor_score"
    assert "imaging" in accepted[0]["focus_anchor_hits"]
    assert result["audit"]["paper_scope"]["selected_paper_ids"] == ["on-topic"]


def test_explicit_current_run_material_is_not_lost_after_zero_base_selection(
    tmp_path: Path,
):
    current_paper = S2PaperRecord(
        paper_id="current-run-paper",
        title="Metasurface imaging pilot",
        abstract="A run-local candidate returned by the current discovery wave.",
        discovery_route="s2_search",
        content_depth="fulltext",
        use_permission="factual_support",
        route_events=[{"event": "current_run_discovery"}],
    )
    current_chunk = UnifiedTextChunk(
        chunk_id="current-run-chunk",
        paper_id="current-run-paper",
        title=current_paper.title,
        text="The run-local candidate contains a measured image reconstruction result.",
        content_depth="fulltext",
        context_complete=True,
        use_permission="factual_support",
    )
    result = _run(
        tmp_path,
        papers=[
            (
                "cooling-0",
                "Metasurface thermal emitter for radiative cooling",
                "Thermal emission through the atmospheric window supports passive cooling.",
            )
        ],
        chunks=[
            ("cooling-chunk", "cooling-0", "Results", "Radiative cooling performance.")
        ],
        current_papers=[current_paper],
        current_chunks=[current_chunk],
    )

    assert result["status"] == "partial"
    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    assert overlay.execute(
        "SELECT COUNT(*) FROM papers WHERE paper_id='current-run-paper'"
    ).fetchone()[0] == 1
    assert overlay.execute(
        "SELECT COUNT(*) FROM text_chunks WHERE chunk_id='current-run-chunk'"
    ).fetchone()[0] == 1
    overlay.close()

    incoming = result["ingest"]["paper_decisions"]
    accepted = [item for item in incoming if item.get("paper_id") == "current-run-paper"]
    assert accepted[0]["reason"] == "explicit_current_run_discovery"
    assert accepted[0]["explicit_current_run"] is True
    assert result["ingest"]["chunks_accepted"] == 1


def test_forbidden_regime_paper_rejected_by_extra_notes_boundary(tmp_path: Path):
    """A microwave paper must be hard-rejected when output.extra_notes forbids that regime."""
    plan = {
        **_query_plan(),
        "output": {
            **_query_plan()["output"],
            "extra_notes": (
                "This review is limited to optical/near-IR metasurface flat optics. "
                "Do not include microwave or acoustic metamaterials."
            ),
        },
    }
    _, policy_path = _write_inputs(tmp_path)
    # Write custom query plan after _write_inputs so it is not overwritten.
    query_path = tmp_path / "query_plan.json"
    query_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    papers = [
        (
            "microwave-0",
            "Microwave metamaterial absorber for RF shielding",
            "We design a microwave metamaterial operating in the GHz range for RF shielding. "
            "Microwave absorbers reduce radar cross section at 10 GHz.",
        ),
        (
            "on-topic",
            "Dielectric metasurface metalens for computational imaging",
            "We fabricate a dielectric metasurface metalens for computational imaging. "
            "Subwavelength nanoantennas provide polarization control. Prior microwave "
            "work is mentioned once as historical background.",
        ),
    ]
    chunks = [
        (
            "mw-chunk",
            "microwave-0",
            "Results",
            "Microwave absorption efficiency at 10 GHz shows 30 dB reduction.",
        ),
        (
            "on-topic-chunk",
            "on-topic",
            "Mechanism",
            "The dielectric metasurface metalens maps phase to the focal field for imaging.",
        ),
    ]
    base = _make_base(tmp_path, papers=papers, chunks=chunks)
    result = build_topic_scoped_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base,
        work_dir=tmp_path / "run",
        policy_path=policy_path,
    )

    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    paper_ids = {row[0] for row in overlay.execute("SELECT paper_id FROM papers")}
    overlay.close()

    assert "microwave-0" not in paper_ids, "microwave paper must be excluded by regime boundary"
    assert "on-topic" in paper_ids, "on-topic optical paper must be accepted"

    decisions = result["selection"]["papers"]["paper_decisions"]
    mw_decision = next(d for d in decisions if d["paper_id"] == "microwave-0")
    assert mw_decision["accepted"] is False
    assert mw_decision["reason"] == "forbidden_regime_boundary_match"
    optical_decision = next(d for d in decisions if d["paper_id"] == "on-topic")
    assert optical_decision["accepted"] is True
    assert optical_decision["regime_decision"]["incompatible"] is False


def test_method_only_candidate_is_not_direct_for_object_specific_query(tmp_path: Path):
    query_path = tmp_path / "query_plan.json"
    query_path.write_text(
        json.dumps(_object_specific_inverse_design_plan(), ensure_ascii=False),
        encoding="utf-8",
    )
    policy_path = tmp_path / "s2_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 1],
                    "graph_depth": 0,
                },
                "graph": {
                    "seed_count": 0,
                    "reference_limit_per_seed": 0,
                    "citation_limit_per_seed": 0,
                    "recommendation_limit": 0,
                },
                "evidence": {
                    "minimum_factual_papers": 1,
                    "minimum_factual_chunks": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    base = _make_base(
        tmp_path,
        papers=[
            (
                "acoustic-method-only",
                "Inverse design of acoustic topological insulators based on a physics-informed neural network",
                "We use a physics-informed neural network for inverse design of an acoustic topological insulator.",
            )
        ],
        chunks=[],
    )

    result = build_topic_scoped_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base,
        work_dir=tmp_path / "run",
        policy_path=policy_path,
    )

    decision = result["selection"]["papers"]["paper_decisions"][0]
    assert decision["accepted"] is False
    assert decision["reason"] in {
        "topic_object_anchor_miss",
        "generic_shared_term_only",
    }
    assert decision["object_anchor_mode"] == "scientific_object"
    assert decision["scientific_object_anchor_required"] is True
    assert decision["method_anchor_hits"]


def test_object_modifier_only_method_review_is_contextual_not_direct(
    tmp_path: Path,
):
    query_path, policy_path = _write_inputs(tmp_path)
    query_path.write_text(
        json.dumps(_object_specific_inverse_design_plan(), ensure_ascii=False),
        encoding="utf-8",
    )
    base = _make_base(
        tmp_path,
        papers=[
            (
                "broad-method-review",
                "Physics-informed neural networks for multi-physics design and discovery",
                "This review compares differentiable solvers and physics-informed neural "
                "networks across fluid dynamics, biomedical engineering, astronomy, and "
                "nanophotonic inverse design.",
            ),
            (
                "metasurface-head",
                "Differentiable inverse design of dielectric metasurfaces",
                "An electromagnetic solver optimizes an optical metasurface response.",
            ),
            (
                "compound-object",
                "Physics-informed inverse design of nanophotonic metasurfaces",
                "A neural network solves a nanophotonic metasurface inverse problem.",
            ),
        ],
        chunks=[
            (
                "broad-method-chunk",
                "broad-method-review",
                "Overview",
                "PINNs and differentiable solvers are compared across many physical domains.",
            )
        ],
    )

    result = build_topic_scoped_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base,
        work_dir=tmp_path / "run",
        policy_path=policy_path,
    )

    decisions = {
        item["paper_id"]: item
        for item in result["selection"]["papers"]["paper_decisions"]
    }
    broad = decisions["broad-method-review"]
    assert broad["accepted"] is True
    assert broad["scope_fit"] == "contextual"
    assert broad["reason"] == "contextual_method_transfer_without_object_head"
    assert broad["object_identity_evidence_present"] is False
    assert broad["object_head_anchor_hits"] == []
    assert "nanophotonic" in broad["object_modifier_anchor_hits"]
    assert broad["contextual_method_transfer"] is True

    head = decisions["metasurface-head"]
    assert head["scope_fit"] == "direct"
    assert head["object_identity_evidence_present"] is True
    assert "metasurface" in head["object_head_anchor_hits"]

    compound = decisions["compound-object"]
    assert compound["scope_fit"] == "direct"
    assert compound["object_identity_evidence_present"] is True
    assert "nanophotonic metasurface" in compound["compound_object_phrase_hits"]

    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    broad_row = overlay.execute(
        "SELECT scope_fit, use_permission FROM papers WHERE paper_id=?",
        ("broad-method-review",),
    ).fetchone()
    broad_chunk = overlay.execute(
        "SELECT scope_fit, use_permission FROM text_chunks WHERE chunk_id=?",
        ("broad-method-chunk",),
    ).fetchone()
    overlay.close()
    assert broad_row == ("contextual", "contextual_or_qualified_support")
    assert broad_chunk == ("contextual", "contextual_or_qualified_support")


def test_current_run_route_cannot_bypass_scientific_object_gate(tmp_path: Path):
    query_path = tmp_path / "query_plan.json"
    query_path.write_text(
        json.dumps(_object_specific_inverse_design_plan(), ensure_ascii=False),
        encoding="utf-8",
    )
    policy_path = tmp_path / "s2_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 1],
                    "graph_depth": 0,
                },
                "graph": {
                    "seed_count": 0,
                    "reference_limit_per_seed": 0,
                    "citation_limit_per_seed": 0,
                    "recommendation_limit": 0,
                },
                "evidence": {
                    "minimum_factual_papers": 1,
                    "minimum_factual_chunks": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    base = _make_base(tmp_path, papers=[], chunks=[])
    current = S2PaperRecord(
        paper_id="current-acoustic-method-only",
        title="Acoustic inverse design with a physics-informed neural network",
        abstract="An acoustic topological-insulator inverse-design method.",
        discovery_route="s2_search",
        content_depth="fulltext",
        use_permission="factual_support",
    )

    result = build_topic_scoped_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base,
        work_dir=tmp_path / "run",
        policy_path=policy_path,
        papers=[current],
    )

    decision = next(
        item
        for item in result["ingest"]["paper_decisions"]
        if item.get("paper_id") == current.paper_id
    )
    assert decision["accepted"] is False
    assert decision["reason"] == "explicit_current_run_object_anchor_miss"
    overlay = sqlite3.connect(result["runtime_kb_sqlite"])
    assert overlay.execute(
        "SELECT COUNT(*) FROM papers WHERE paper_id=?", (current.paper_id,)
    ).fetchone()[0] == 0
    overlay.close()
