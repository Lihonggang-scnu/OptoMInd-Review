"""Focused local-fixture tests for the staged visual remount bridge."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.staged_visual_remount import (
    STAGED_SECTION_IDS,
    StagedVisualRemountError,
    build_commander_enrichment,
    build_planner_section_inputs,
    parse_staged_manuscript,
    run_staged_visual_remount,
)
from optomind_research.runtime.visual_editor_tool_provider import (
    validate_visual_editorial_plan_file,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-staged-visual-remount"

_SECTION_TITLES = {
    "S01": "Foundations of Physics-Constrained Inverse Design",
    "S02": "Forward Modeling: Accuracy, Scalability, and Mesh-Free Advantages",
    "S03": "Inverse Design: Gradient Fidelity and Optimization Dynamics",
    "S04": "Simulation Credibility: Error Sources and Validation Protocols",
    "S05": "Closing the Gap: Robust Design and Uncertainty Quantification",
    "S06": "Experimental Validation: Measurement Challenges and Discrepancies",
    "S07": "Synthesis: Decision Framework for Method Selection",
    "S08": "Future Directions and Unresolved Research Gaps",
}

_SECTION_BODIES = {
    "S01": (
        "The optical resonance mechanism confines the electromagnetic field "
        "inside the spectral emitter cavity. Physics-informed neural "
        "networks embed the governing equations as soft constraints."
    ),
    "S02": (
        "Forward modeling accuracy of differentiable solvers depends on "
        "mesh scalability and discretization error control for spectral "
        "emitter structures."
    ),
    "S03": (
        "Inverse design gradient fidelity drives optimization dynamics in "
        "high dimensional landscapes with local minima and spectral bias."
    ),
    "S04": (
        "Simulation credibility requires validation protocols that "
        "decompose approximation, optimization, and discretization error "
        "sources."
    ),
    "S05": (
        "Robust design closes the simulation to experiment gap through "
        "Bayesian uncertainty quantification and ensemble strategies."
    ),
    "S06": (
        "Experimental validation reveals measurement challenges and "
        "discrepancies from near-field coupling and fabrication tolerance."
    ),
    "S07": (
        "The synthesis builds a decision framework for method selection "
        "across mechanism, forward modeling, and inverse design axes."
    ),
    "S08": (
        "Future directions address unresolved research gaps including "
        "multi-physics coupling, surrogate acceleration, and standardized "
        "benchmark suites."
    ),
}


@pytest.fixture()
def work_dir() -> Path:
    """Workspace-local temp dir; sandbox denies tempfile.mkdtemp ACLs here."""

    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEMP_ROOT / f"run-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            _TEMP_ROOT.rmdir()
        except OSError:
            pass


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 200), color=color).save(path)
    return path


def _unit(
    *,
    unit_id: str,
    caption: str,
    tags: list[str],
    nearby_text: str = "",
    vtype: str = "mechanism_anchor",
    confidence: str = "high",
    utility: str = "high",
    license_text: str = "cc-by-4.0",
    use_permission: str = "factual_support",
    reuse_permission: str = "allowed",
    approval_state: str = "approved",
    approval_marker: str = "human_approved",
    paper_id: str = "paper-1",
    doi: str = "10.1/visual",
) -> dict:
    return {
        "schema_version": "optomind.visual_unit.v1",
        "unit_id": unit_id,
        "unit_kind": "single_figure",
        "unit_role": "",
        "source_identity": {
            "paper_id": paper_id,
            "doi": doi,
            "title": f"Paper {paper_id}",
        },
        "figure_identity": {
            "asset_id": f"{paper_id}:Figure:{unit_id}",
            "figure_label": "Figure 1",
            "parent_label": "Figure 1",
            "subfigure_label": "",
            "page": None,
            "bbox_px": None,
            "bbox_original_px": None,
        },
        "caption": {
            "clean": caption,
            "original": caption,
            "subfigure_focus": "",
        },
        "semantic": {
            "description": caption,
            "tags": tags,
            "nearby_text": nearby_text,
            "visual_card": {},
            "visual_profile": {},
            "quality": {},
            "body_callout_texts": [],
            "linked_text_chunk_ids": [],
        },
        "argumentative_roles": {
            "primary": vtype,
            "confidence": confidence,
            "review_utility": utility,
            "claim": caption,
        },
        "provenance": {
            "source_file_ref": {"relative": f"src/{unit_id}.pdf"},
            "checksum": "",
        },
        "permission_state": {
            "use_permission": use_permission,
            "allowed_claim_kinds": [],
            "license": license_text,
            "reuse_permission": reuse_permission,
            "permission_source": "fixture",
            "notes": [],
        },
        "hashes": {},
        "vector_refs": [],
        "lineage": {},
        "use_history": [],
        "review": {},
        "approval": {
            "state": approval_state,
            "source_marker": approval_marker,
        },
        "crop_hygiene": {},
        "paths": {
            "image_ref": {
                "root": "snapshot",
                "relative": f"assets/{unit_id}.png",
            }
        },
        "created_at": "",
        "source_map": {},
    }


def _publish_snapshot(work_dir: Path, units: list[dict]) -> Path:
    snapshot = work_dir / "snapshot"
    assets = snapshot / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    palette = ("navy", "darkred", "teal", "purple")
    for index, unit in enumerate(units):
        relative = unit["paths"]["image_ref"]["relative"]
        _image(snapshot / relative, color=palette[index % len(palette)])
    (snapshot / "units.json").write_text(
        json.dumps(units, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot


def _canonical_manuscript() -> str:
    parts = [
        "## Abstract",
        "",
        "The article evaluates numerical electromagnetic methods for "
        "nanophotonic design, contrasting mesh-free learning surrogates "
        "with grid-based classical simulation, and the path from modeling "
        "to measurement. "
        "[REF:doi:10.1/abstract]",
        "",
        "## Introduction",
        "",
        "This review compares physics-informed neural networks with "
        "differentiable electromagnetic solvers across forward modeling, "
        "inverse design, simulation credibility, and the path from "
        "simulation to experiment. [REF:doi:10.1/introduction]",
        "",
    ]
    for section_id in ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
        title = _SECTION_TITLES[section_id]
        parts.extend(
            [
                f"## {title}",
                "",
                f"# {title}",
                "",
                f"{_SECTION_BODIES[section_id]} "
                f"[REF:doi:10.1/{section_id.lower()}]",
                "",
            ]
        )
    parts.extend(
        [
            "## Conclusion",
            "",
            "The review concludes that robust design and uncertainty "
            "quantification are needed to close the simulation to experiment "
            "gap. [REF:doi:10.1/conclusion]",
            "",
        ]
    )
    return "\n".join(parts)


def _commander_payload() -> dict:
    return {
        "schema_version": (
            "optomind.global_manuscript_commander.work_order.v2"
        ),
        "fingerprint": "commander-fixture",
        "manuscript_diagnosis": (
            "The manuscript follows a coherent mechanism-to-application "
            "narrative."
        ),
        "section_decisions": [
            {
                "section_id": "S01",
                "decision": "retain",
                "rationale": (
                    "Establishes necessary theoretical baseline; no "
                    "structural changes needed."
                ),
            },
            {
                "section_id": "S05",
                "decision": "expand",
                "rationale": (
                    "Needs expansion on Bayesian and ensemble uncertainty "
                    "quantification."
                ),
            },
        ],
        "proposed_section_order": [
            {
                "section_id": "S01",
                "reason": "Foundational definitions come first.",
            }
        ],
        "structure_gaps": [
            {
                "section_id": "S05",
                "gap_type": "blueprint_structure_gap",
                "proposal": "Narrow S05 to robust design and UQ.",
            }
        ],
        "cross_section_conflicts": [
            {
                "conflict_type": "job_overlap",
                "sections": ["S04", "S05"],
                "description": "Both discuss error sources.",
                "recommendation": "Keep validation and UQ distinct.",
                "finding_id": "GMC-CS-001",
            }
        ],
        "visual_work_orders": [
            {
                "section_ids": ["S06"],
                "order": "Provide experimental validation figures.",
                "approval_required": False,
            }
        ],
    }


def _global_inputs_payload() -> dict:
    return {
        "schema_version": "optomind.staged_global_inputs.v1",
        "user_question": (
            "Compare PINNs with differentiable solvers including "
            "simulation credibility."
        ),
        "problem_understanding": (
            "Explain when PINNs and differentiable solvers agree or diverge."
        ),
        "global_review_thesis": (
            "The divergence is a fundamental architectural trade-off."
        ),
        "global_narrative_strategy": (
            "Mechanism-to-Application narrative across eight sections."
        ),
        "sections": [
            {
                "section_id": "S01",
                "chapter_thesis": (
                    "PINNs use soft constraints and automatic "
                    "differentiation."
                ),
                "central_thesis": (
                    "PINNs trade rigid accuracy for mesh-free flexibility."
                ),
                "reader_takeaway": (
                    "PINNs offer mesh-free scalability but face gradient "
                    "pathologies."
                ),
                "narrative_strategy": {
                    "statement": (
                        "Define the core mechanism of PINNs as soft-constraint "
                        "enforcement."
                    )
                },
                "terminology": [
                    {"term": "PINNs", "definition_claim_handle": "C01"}
                ],
                "review_summary": {
                    "available": True,
                    "status": "advisory",
                    "advisory_count": 1,
                    "blocking_count": 0,
                    "comments": [],
                },
                "core_trust_boundary": "core_evidence",
                "explanatory_trust_boundary": "background_explanation_only",
                "argument_sequence": [
                    {
                        "step_index": 1,
                        "purpose": "Define the soft-constraint mechanism.",
                        "claim_handles": [
                            "C01_REVIEWED_STUDY_REPORTS_PHYSICS",
                            "C05_REVIEWED_STUDY_REPORTS_PHYSICS",
                        ],
                        "evidence_handles": ["E01"],
                    }
                ],
            }
        ],
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prepare_inputs(
    work_dir: Path,
    *,
    units: list[dict],
    manuscript: str | None = None,
    commander: dict | None = None,
    global_inputs: dict | None = None,
) -> dict:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    manuscript_path = work_dir / "STAGED_COMPLETE_REVIEW_EN.md"
    manuscript_path.write_text(
        manuscript if manuscript is not None else _canonical_manuscript(),
        encoding="utf-8",
    )
    snapshot = _publish_snapshot(work_dir, units)
    commander_path = None
    global_inputs_path = None
    if commander is not None:
        commander_path = work_dir / "commander.json"
        _write_json(commander_path, commander)
    if global_inputs is not None:
        global_inputs_path = work_dir / "global_inputs.json"
        _write_json(global_inputs_path, global_inputs)
    return {
        "manuscript_path": manuscript_path,
        "visual_cache_path": snapshot,
        "commander_work_order_path": commander_path,
        "staged_global_inputs_path": global_inputs_path,
    }


def _run(
    work_dir: Path,
    *,
    units: list[dict],
    manuscript: str | None = None,
    commander: dict | None = None,
    global_inputs: dict | None = None,
    run_id: str = "fixture-run",
    max_placements_per_section: int = 2,
) -> dict:
    inputs = _prepare_inputs(
        work_dir,
        units=units,
        manuscript=manuscript,
        commander=commander,
        global_inputs=global_inputs,
    )
    output_dir = work_dir / "out"
    return run_staged_visual_remount(
        manuscript_path=inputs["manuscript_path"],
        visual_cache_path=inputs["visual_cache_path"],
        output_dir=output_dir,
        commander_work_order_path=inputs["commander_work_order_path"],
        staged_global_inputs_path=inputs["staged_global_inputs_path"],
        run_id=run_id,
        max_placements_per_section=max_placements_per_section,
    )


def _matching_s01_units() -> list[dict]:
    return [
        _unit(
            unit_id="vis-s01-a",
            caption=(
                "Optical resonance mechanism with field confinement inside "
                "the spectral emitter cavity."
            ),
            tags=[
                "optical-resonance",
                "mechanism",
                "field-confinement",
                "spectral-emitter",
            ],
            nearby_text=(
                "The optical resonance mechanism confines the field in the "
                "resonant cavity."
            ),
        ),
        _unit(
            unit_id="vis-s01-b",
            caption=(
                "Optical resonance mechanism with soft constraint "
                "enforcement inside the spectral emitter cavity."
            ),
            tags=["optical-resonance", "soft-constraint", "mechanism"],
        ),
        _unit(
            unit_id="vis-s01-c",
            caption=(
                "Optical resonance mechanism workflow for spectral emitter "
                "cavity analysis."
            ),
            tags=["optical-resonance", "workflow", "spectral-emitter"],
            vtype="method_or_workflow",
        ),
    ]


def _structural_policy_units() -> list[dict]:
    return [
        *_matching_s01_units(),
        _unit(
            unit_id="vis-abstract",
            caption=(
                "Numerical electromagnetic simulation methods for "
                "nanophotonic design with mesh-free learning surrogates "
                "and grid-based classical solvers."
            ),
            tags=[
                "numerical-electromagnetic",
                "nanophotonic-design",
                "mesh-free-learning",
            ],
            vtype="synthesis_overview",
        ),
        _unit(
            unit_id="vis-intro-1",
            caption=(
                "Simulation credibility path from simulation to experiment "
                "across forward modeling and inverse design."
            ),
            tags=["simulation-credibility", "forward-modeling"],
            vtype="taxonomy_or_roadmap",
        ),
        _unit(
            unit_id="vis-intro-2",
            caption=(
                "Physics-informed neural networks compared with "
                "differentiable electromagnetic solvers in forward modeling."
            ),
            tags=["physics-informed", "differentiable-solvers"],
            vtype="quantitative_comparison",
        ),
    ]


def test_parse_staged_manuscript_maps_all_canonical_sections():
    parsed = parse_staged_manuscript(_canonical_manuscript())
    assert parsed["section_ids"] == list(STAGED_SECTION_IDS)
    sections = {s["section_id"]: s for s in parsed["sections"]}
    assert len(sections) == 11
    assert sections["Abstract"]["title"] == "Abstract"
    assert (
        sections["S01"]["title"]
        == "Foundations of Physics-Constrained Inverse Design"
    )
    assert sections["S01"]["toc_duplicate"] is True
    assert sections["S01"]["toc_duplicate_line"] == sections["S01"][
        "heading_line"
    ] + 2
    assert sections["S01"]["word_count"] > 20
    assert "doi:10.1/s01" in sections["S01"]["citation_ids"]
    assert sections["Conclusion"]["citation_ids"] == ["doi:10.1/conclusion"]
    # The repeated '# same title' visible heading is presentation cleanup,
    # never section prose: no heading line survives in the parsed text.
    assert not any(
        line.lstrip().startswith("#")
        for line in sections["S01"]["text"].splitlines()
    )


def test_parse_rejects_duplicate_section_heading():
    manuscript = _canonical_manuscript().replace(
        "## Introduction",
        "## Abstract\n\nDuplicate abstract block.\n\n## Introduction",
        1,
    )
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(manuscript)
    assert "duplicate section id 'Abstract'" in str(exc.value)


def test_parse_rejects_duplicate_explicit_section_id():
    manuscript = _canonical_manuscript().replace(
        (
            "## Inverse Design: Gradient Fidelity and Optimization "
            "Dynamics\n\n# Inverse Design: Gradient Fidelity and "
            "Optimization Dynamics"
        ),
        (
            "## S03 Inverse Design: Gradient Fidelity\n\n"
            "# S03 Inverse Design: Optimization"
        ),
        1,
    )
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(manuscript)
    assert "duplicate section id 'S03'" in str(exc.value)

def test_dynamic_parser_accepts_noncontiguous_runtime_section_ids() -> None:
    manuscript = (
        "## Abstract\n\nAbstract.\n\n"
        "## Introduction\n\nIntroduction.\n\n"
        "## S01 Foundations\n\nS01 body.\n\n"
        "## S02 Methods\n\nS02 body.\n\n"
        "## S09 Outlook\n\nS09 body.\n\n"
        "## Conclusion\n\nConclusion.\n"
    )

    parsed = parse_staged_manuscript(
        manuscript,
        allow_dynamic_sections=True,
    )

    assert parsed["section_ids"] == [
        "Abstract",
        "Introduction",
        "S01",
        "S02",
        "S09",
        "Conclusion",
    ]


def test_parse_rejects_missing_section_id():
    manuscript = _canonical_manuscript().rsplit("## Conclusion", 1)[0]
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(manuscript)
    assert "missing section id(s)" in str(exc.value)
    assert "Conclusion" in str(exc.value)

    marker = f"{_SECTION_BODIES['S02']} [REF:doi:10.1/s02]"
    missing_s03 = (
        _canonical_manuscript().split(marker, 1)[0] + marker + "\n"
    )
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(missing_s03)
    assert str(exc.value) == (
        "missing section id(s) in staged manuscript: S03, S04, S05, S06, "
        "S07, S08, Conclusion"
    )

    missing_introduction = _canonical_manuscript().split(
        "## Foundations of Physics-Constrained Inverse Design",
        1,
    )[0]
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(missing_introduction)
    assert "S01" in str(exc.value)
    assert "missing section id(s)" in str(exc.value)


def test_parse_rejects_out_of_order_section_id():
    manuscript = _canonical_manuscript().replace(
        "## Foundations of Physics-Constrained Inverse Design",
        "## S03 Out Of Order Heading",
        1,
    )
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(manuscript)
    assert "out-of-order section id 'S03'" in str(exc.value)
    assert "expected 'S01'" in str(exc.value)


def test_parse_rejects_unexpected_heading_after_conclusion():
    manuscript = _canonical_manuscript().rstrip() + "\n\n## References\n\nMore text.\n"
    with pytest.raises(StagedVisualRemountError) as exc:
        parse_staged_manuscript(manuscript)
    assert "unexpected heading" in str(exc.value)
    assert "References" in str(exc.value)


def test_planner_section_inputs_include_responsibility_thesis_claims_citations():
    parsed = parse_staged_manuscript(_canonical_manuscript())
    enrichment = build_commander_enrichment(
        parsed=parsed,
        commander_work_order=_commander_payload(),
        staged_global_inputs=_global_inputs_payload(),
    )
    inputs = build_planner_section_inputs(
        parsed=parsed,
        enrichment=enrichment,
    )
    by_id = {item["section_id"]: item for item in inputs}
    s01 = by_id["S01"]
    assert "commander:retain" in s01["argument_role"]
    assert "theoretical baseline" in s01["argument_role"]
    assert "soft constraints" in s01["argument_role"]
    assert s01["text"]
    assert {c["claim_id"] for c in s01["claims"]} == {
        "C01_REVIEWED_STUDY_REPORTS_PHYSICS",
        "C05_REVIEWED_STUDY_REPORTS_PHYSICS",
    }
    assert s01["claims"][0]["status"] == "approved"
    assert {c["citation_id"] for c in s01["citations"]} == {
        "doi:10.1/s01"
    }
    assert (
        "The divergence is a fundamental architectural trade-off."
        in by_id["Abstract"]["argument_role"]
    )
    s05 = by_id["S05"]
    assert s05["argument_role"].startswith("commander:expand")
    assert "Bayesian and ensemble" in s05["argument_role"]


def test_remount_allows_zero_placements_and_records_empty_coverage(
    work_dir: Path,
):
    unrelated = _unit(
        unit_id="vis-unrelated",
        caption=(
            "Quantum chemistry hydration shell molecular orbital geometry "
            "solvent cluster."
        ),
        tags=["quantum", "hydration", "molecular-orbital", "solvent"],
        vtype="representative_example",
    )
    summary = _run(work_dir, units=[unrelated])
    assert summary["status"] == "completed"
    for artifact in summary["artifacts"].values():
        assert Path(artifact).is_file()
    plan_path = Path(summary["artifacts"]["plan"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["placements"] == []
    assert plan["conceptual_figure_requests"] == []
    assert plan["validation"]["fail_open"] is True

    audit = json.loads(
        Path(summary["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    assert audit["coverage"]["total_placements"] == 0
    assert audit["coverage"]["empty_coverage_recorded"] is True
    assert audit["coverage"]["sections_without_placements"] == list(
        STAGED_SECTION_IDS
    )
    assert audit["coverage"]["generation_requests_emitted"] is False
    assert audit["coverage"]["images_generated_by_bridge"] is False
    assert all(
        section["coverage_status"] == "empty_recorded"
        for section in audit["sections"]
    )
    reasons = {
        item["reason"]
        for section in audit["sections"]
        for item in section["unfilled_needs"]
    }
    assert "zero_visual_selected_acceptable" in reasons


def test_remount_no_generation_requests_by_default_even_for_conceptual_sections(
    work_dir: Path,
):
    summary = _run(work_dir, units=_matching_s01_units())
    plan = json.loads(
        Path(summary["artifacts"]["plan"]).read_text(encoding="utf-8")
    )
    assert len(plan["placements"]) >= 1
    assert plan["conceptual_figure_requests"] == []
    audit = json.loads(
        Path(summary["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    assert audit["coverage"]["generation_requests_emitted"] is False
    assert audit["coverage"]["images_generated_by_bridge"] is False


def test_remount_preserves_permission_and_publication_eligibility(
    work_dir: Path,
):
    allowed = _unit(
        unit_id="vis-allowed",
        caption=(
            "Optical resonance mechanism with field confinement inside the "
            "spectral emitter cavity."
        ),
        tags=["optical-resonance", "mechanism", "field-confinement"],
        license_text="cc-by-4.0",
        use_permission="factual_support",
        reuse_permission="allowed",
        approval_state="approved",
        approval_marker="human_approved",
    )
    restricted = _unit(
        unit_id="vis-restricted",
        caption=(
            "Optical resonance mechanism with field confinement inside the "
            "spectral emitter cavity variant."
        ),
        tags=["optical-resonance", "mechanism", "field-confinement"],
        license_text="",
        use_permission="discovery_only",
        reuse_permission="restricted",
        approval_state="pending",
        approval_marker="",
    )
    summary = _run(work_dir, units=[allowed, restricted])
    audit = json.loads(
        Path(summary["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    s01 = next(
        section
        for section in audit["sections"]
        if section["section_id"] == "S01"
    )
    assert s01["placement_count"] == 1
    placement = s01["placements"][0]
    assert placement["visual_chunk_id"] == "vis-allowed"
    assert placement["permission"]["status"] == "allowed"
    assert placement["permission"]["license"] == "cc-by-4.0"
    assert placement["permission"]["use_permission"] == "factual_support"
    assert placement["publication_eligible"] is True
    assert audit["permission_publication"][
        "publication_eligible_placements"
    ] == 1
    restricted_reasons = {
        item["reason"] for item in s01["unfilled_needs"]
    }
    assert "permission_restricted_candidate_excluded" in restricted_reasons


def test_remount_respects_max_placements_per_section(work_dir: Path):
    units = _matching_s01_units()
    two = _run(
        work_dir / "two",
        units=units,
        run_id="two",
        max_placements_per_section=2,
    )
    plan_two = json.loads(
        Path(two["artifacts"]["plan"]).read_text(encoding="utf-8")
    )
    s01_two = next(
        s for s in plan_two["sections"] if s["section_id"] == "S01"
    )
    # max_placements_per_section is an upper bound, never a quota: the
    # fixture has three independently qualifying S01 candidates, so S01 is
    # allowed up to 2 but exactly 2 is not required.
    assert 1 <= s01_two["placement_count"] <= 2
    assert all(
        s["placement_count"] <= 2 for s in plan_two["sections"]
    )
    audit_two = json.loads(
        Path(two["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    assert audit_two["coverage"]["weak_fallback_placements"] == 0

    one = _run(
        work_dir / "one",
        units=units,
        run_id="one",
        max_placements_per_section=1,
    )
    plan_one = json.loads(
        Path(one["artifacts"]["plan"]).read_text(encoding="utf-8")
    )
    s01_one = next(
        s for s in plan_one["sections"] if s["section_id"] == "S01"
    )
    # One allowed slot with qualifying candidates: the best candidate is
    # selected, never a forced filler.
    assert s01_one["placement_count"] == 1
    assert all(
        s["placement_count"] <= 1 for s in plan_one["sections"]
    )
    audit_one = json.loads(
        Path(one["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    assert audit_one["coverage"]["weak_fallback_placements"] == 0


def test_remount_is_deterministic(work_dir: Path):
    units = _matching_s01_units()
    commander = _commander_payload()
    global_inputs = _global_inputs_payload()
    inputs = _prepare_inputs(
        work_dir / "inputs",
        units=units,
        commander=commander,
        global_inputs=global_inputs,
    )
    cache_b = work_dir / "cache-b"
    shutil.copytree(inputs["visual_cache_path"], cache_b)

    def run_once(cache: Path, output_dir: Path) -> dict:
        return run_staged_visual_remount(
            manuscript_path=inputs["manuscript_path"],
            visual_cache_path=cache,
            output_dir=output_dir,
            commander_work_order_path=inputs["commander_work_order_path"],
            staged_global_inputs_path=inputs["staged_global_inputs_path"],
            run_id="same-run",
        )

    summary_a = run_once(inputs["visual_cache_path"], work_dir / "out-a")
    summary_b = run_once(cache_b, work_dir / "out-b")
    # Content fingerprints are relocation-safe: identical content at
    # different cache/output locations must produce the same hashes.
    assert summary_a["fingerprints"] == summary_b["fingerprints"]
    # Plan, review queue, factory reference, and blueprint embed only
    # cache-relative image paths and content, so they are byte-identical
    # across locations.
    for name in ("plan", "review_queue", "factory_plan", "blueprint"):
        assert (
            Path(summary_a["artifacts"][name]).read_bytes()
            == Path(summary_b["artifacts"][name]).read_bytes()
        )
    audit_a = json.loads(
        Path(summary_a["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    audit_b = json.loads(
        Path(summary_b["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    # Provenance paths may differ (they name the source location), but all
    # content-derived audit sections are identical.
    assert audit_a["fingerprints"] == audit_b["fingerprints"]
    assert audit_a["sections"] == audit_b["sections"]
    assert audit_a["coverage"] == audit_b["coverage"]
    assert (
        audit_a["permission_publication"]
        == audit_b["permission_publication"]
    )
    inputs_a = json.loads(
        Path(summary_a["artifacts"]["inputs"]).read_text(encoding="utf-8")
    )
    inputs_b = json.loads(
        Path(summary_b["artifacts"]["inputs"]).read_text(encoding="utf-8")
    )
    assert inputs_a["fingerprints"] == inputs_b["fingerprints"]
    assert inputs_a["section_inputs"] == inputs_b["section_inputs"]
    assert inputs_a["factory_blueprint"] == inputs_b["factory_blueprint"]


def test_remount_writes_factory_ready_plan_reference(work_dir: Path):
    summary = _run(work_dir, units=_matching_s01_units())
    plan_dir = Path(summary["artifacts"]["plan"]).parent
    raw_plan = json.loads(
        (plan_dir / "planner/VISUAL_CONSTRUCTION_PLAN.json").read_text(
            encoding="utf-8"
        )
    )
    editorial_path = plan_dir / (
        "planner/VISUAL_EDITORIAL_PLAN.json"
    )
    validation = validate_visual_editorial_plan_file(
        editorial_path,
        raw_plan["input_fingerprint"],
        set(STAGED_SECTION_IDS),
    )
    assert validation.startswith("VALIDATION_PASSED")
    plan = json.loads(
        Path(summary["artifacts"]["plan"]).read_text(encoding="utf-8")
    )
    factory = json.loads(
        Path(summary["artifacts"]["factory_plan"]).read_text(
            encoding="utf-8"
        )
    )
    assert factory["editorial_plan"]["placements"]
    assert factory["generation_policy"]["images_generated_by_bridge"] is False
    assert factory["factory_targets"]["visual_editorial_plan_path"].endswith(
        "VISUAL_EDITORIAL_PLAN.json"
    )
    assert (
        Path(summary["artifacts"]["plan"]).parent
        / "STAGED_VISUAL_REMOUNT_REVIEW_QUEUE.json"
    ).is_file()


def test_remount_rejects_invalid_max_placements(work_dir: Path):
    manuscript_path = work_dir / "manuscript.md"
    manuscript_path.write_text(_canonical_manuscript(), encoding="utf-8")
    snapshot = _publish_snapshot(work_dir, [])
    for invalid in (-1, 3):
        with pytest.raises(StagedVisualRemountError) as exc:
            run_staged_visual_remount(
                manuscript_path=manuscript_path,
                visual_cache_path=snapshot,
                output_dir=work_dir / "out",
                max_placements_per_section=invalid,
            )
        assert "max_placements_per_section must be 0, 1, or 2" in str(
            exc.value
        )


def test_remount_rejects_missing_inputs(work_dir: Path):
    manuscript_path = work_dir / "missing.md"
    snapshot = _publish_snapshot(work_dir, [])
    with pytest.raises(StagedVisualRemountError) as exc:
        run_staged_visual_remount(
            manuscript_path=manuscript_path,
            visual_cache_path=snapshot,
            output_dir=work_dir / "out",
        )
    assert "staged manuscript not found" in str(exc.value)

    manuscript_path.write_text(_canonical_manuscript(), encoding="utf-8")
    with pytest.raises(StagedVisualRemountError) as exc:
        run_staged_visual_remount(
            manuscript_path=manuscript_path,
            visual_cache_path=work_dir / "missing-cache",
            output_dir=work_dir / "out",
        )
    assert "visual cache snapshot not found" in str(exc.value)


def test_remount_emits_factory_blueprint_with_section_context(
    work_dir: Path,
):
    summary = _run(
        work_dir,
        units=_matching_s01_units(),
        commander=_commander_payload(),
        global_inputs=_global_inputs_payload(),
    )
    blueprint_path = Path(summary["artifacts"]["blueprint"])
    assert blueprint_path.is_file()
    blueprint = json.loads(
        blueprint_path.read_text(encoding="utf-8")
    )
    assert blueprint["schema_version"].endswith(".blueprint.v1")
    assert blueprint["review_thesis"]
    assert len(blueprint["sections"]) == 11
    section_ids = [s["section_id"] for s in blueprint["sections"]]
    assert section_ids == list(STAGED_SECTION_IDS)
    required_keys = {
        "section_id",
        "title",
        "full_text",
        "argument_role",
        "citation_ids",
    }
    for section in blueprint["sections"]:
        assert required_keys.issubset(section.keys())
    s01 = next(
        section
        for section in blueprint["sections"]
        if section["section_id"] == "S01"
    )
    assert s01["full_text"].startswith("The optical resonance mechanism")
    assert s01["citation_ids"] == ["doi:10.1/s01"]
    assert "commander:retain" in s01["argument_role"]

    audit = json.loads(
        Path(summary["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    assert audit["artifacts"]["blueprint"] == (
        "STAGED_VISUAL_REMOUNT_BLUEPRINT.json"
    )
    assert audit["fingerprints"]["factory_blueprint_sha256"]
    factory = json.loads(
        Path(summary["artifacts"]["factory_plan"]).read_text(
            encoding="utf-8"
        )
    )
    assert factory["factory_targets"]["blueprint_path"] == (
        "STAGED_VISUAL_REMOUNT_BLUEPRINT.json"
    )
    assert factory["structural_visual_policy"][
        "abstract_conclusion_zero"
    ] is True


def test_remount_structural_policy_excludes_front_back_matter_and_caps_intro(
    work_dir: Path,
):
    summary = _run(
        work_dir,
        units=_structural_policy_units(),
        commander=_commander_payload(),
        global_inputs=_global_inputs_payload(),
    )
    plan = json.loads(
        Path(summary["artifacts"]["plan"]).read_text(encoding="utf-8")
    )
    sections = {s["section_id"]: s for s in plan["sections"]}
    assert sections["Abstract"]["placement_count"] == 0
    assert sections["Conclusion"]["placement_count"] == 0
    assert sections["Introduction"]["placement_count"] == 1
    assert all(
        s["placement_count"] <= 2 for s in plan["sections"]
    )
    assert plan["conceptual_figure_requests"] == []
    assert not any(
        placement.get("section_id") in {"Abstract", "Conclusion"}
        for placement in plan["placements"]
    )
    reasons = {
        item["reason"]
        for section in plan["sections"]
        for item in section["unfilled_needs"]
    }
    assert "excluded_structural_policy_zero_visuals" in reasons
    assert "excluded_structural_policy_introduction_cap" in reasons

    audit = json.loads(
        Path(summary["artifacts"]["audit"]).read_text(encoding="utf-8")
    )
    policy = audit["coverage"]["structural_visual_policy"]
    assert policy["abstract_conclusion_zero"] is True
    assert policy["introduction_cap_one"] is True
    assert policy["excluded_by_policy"] >= 3
