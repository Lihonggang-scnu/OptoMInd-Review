"""Focused deterministic tests for the article visual asset planner adapter."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

from optomind_research.runtime.article_visual_asset_planner import (
    ArticleVisualAssetPlannerConfig,
    plan_article_visual_assets,
    validate_visual_construction_plan,
)
from optomind_research.runtime.visual_asset_planner_adapter import (
    load_visual_cache_records,
    run_article_visual_asset_planner,
)
from optomind_research.runtime.visual_cache_ingest import (
    ingest_visual_candidates,
)
from optomind_research.runtime.visual_cache_store import (
    VisualCacheStore,
)
from optomind_research.runtime.visual_editor_tool_provider import (
    validate_visual_editorial_plan_file,
)
from optomind_research.runtime.visual_evidence_factory import (
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
)

_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-article-visual-tests"


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


@pytest.fixture()
def table_tmp() -> Path:
    """Workspace-local scratch dir that avoids stale ACL-blocked roots."""

    scratch = Path(__file__).resolve().parents[1] / ".codex-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"planner-table-test-{uuid.uuid4().hex[:10]}"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)
    try:
        scratch.rmdir()
    except OSError:
        pass


def _image(path: Path, color: str = "navy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 200), color=color).save(path)
    return path


def _record(
    image_path: Path,
    *,
    chunk_id: str = "vis-1",
    paper_id: str = "paper-1",
    doi: str = "10.1/visual",
    caption: str = "Optical resonance mechanism showing field confinement.",
    search_text: str = "optical resonance field confinement mechanism",
    kind: str = "single_figure",
    vtype: str = "mechanism_anchor",
    status: str = "ok",
    confidence: str = "high",
    utility: str = "high",
    permission: str = "allowed",
    needs_human_review: bool = False,
    caption_missing: bool = False,
    crop_contamination: bool = False,
    page_prose_contamination: bool = False,
    parent_chunk_id: str = "",
    subfigure_label: str = "",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "doi": doi,
        "title": f"Paper {paper_id}",
        "caption": caption,
        "caption_missing": caption_missing,
        "crop_contamination": crop_contamination,
        "page_prose_contamination": page_prose_contamination,
        "chunk_kind": kind,
        "search_text": search_text,
        "visual_argument_type": vtype,
        "visual_argument_status": status,
        "visual_argument_confidence": confidence,
        "visual_argument_claim": caption,
        "visual_argument_needs_human_review": needs_human_review,
        "review_utility": utility,
        "parent_chunk_id": parent_chunk_id,
        "subfigure_label": subfigure_label,
        "local_image_path": str(image_path),
        "permission": {
            "status": permission,
            "license": "CC-BY" if permission == "allowed" else "",
        },
    }


def _section(
    section_id: str,
    *,
    text: str = "",
    role: str = "",
    title: str = "",
    claims: tuple = (),
    expected: tuple = (),
) -> dict:
    return {
        "section_id": section_id,
        "title": title,
        "text": text,
        "argument_role": role,
        "claims": [dict(claim) for claim in claims],
        "expected_visual_arguments": list(expected),
    }


def _snapshot_candidate(
    *,
    chunk_id: str,
    kind: str,
    subfigure_label: str,
    image_path: Path,
    parent_image_path: Path,
    caption: str,
    visual_argument_type: str = "trend_or_parameter_map",
    review_decision: str = "",
    tags: tuple[str, ...] = ("spectrum",),
    license_text: str = "",
    extra: dict | None = None,
) -> dict:
    record = {
        "schema_version": "visual_chunk.v1",
        "chunk_id": chunk_id,
        "chunk_kind": kind,
        "paper_id": "snap-paper",
        "doi": "10.1/snapshot",
        "paper_title": "Snapshot Paper",
        "parent_asset_id": "snap-fig1",
        "parent_label": "Figure 1",
        "subfigure_label": subfigure_label,
        "local_image_path": str(image_path),
        "parent_image_path": str(parent_image_path),
        "caption": caption,
        "nearby_text": "The emitter is selective in the atmospheric window.",
        "visual_argument_type": visual_argument_type,
        "visual_argument_status": "pending_multimodal_review",
        "visual_argument_needs_human_review": 1,
        "review_decision": review_decision,
        "use_permission": "factual_support",
        "allowed_claim_kinds": ["trend"],
        "tags": list(tags),
        "license": license_text,
    }
    if extra:
        record.update(extra)
    return record


def _publish_snapshot(work_dir: Path) -> Path:
    source = work_dir / "source"
    source.mkdir(parents=True, exist_ok=True)
    parent_image = _image(source / "fig1.png", "red")
    child_image = _image(source / "fig1-a.png", "blue")
    records = [
        _snapshot_candidate(
            chunk_id="snap-fig1-parent",
            kind="parent_figure",
            subfigure_label="",
            image_path=parent_image,
            parent_image_path=parent_image,
            caption=(
                "Figure 1. a) spectrum of emitter b) device photograph of "
                "the packaged filter."
            ),
            visual_argument_type="mechanism_anchor",
            tags=("spectrum", "device-photograph"),
        ),
        _snapshot_candidate(
            chunk_id="snap-fig1-subfig-a",
            kind="subfigure",
            subfigure_label="a",
            image_path=child_image,
            parent_image_path=parent_image,
            caption=(
                "a) spectrum of the selective emitter with narrow emission."
            ),
            visual_argument_type="trend_or_parameter_map",
            tags=(
                "spectrum",
                "selective-emitter",
                "narrow-emission",
                "atmospheric-window",
            ),
        ),
    ]
    assets = work_dir / "assets"
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=assets,
    )
    assert report["errors"] == []
    store = VisualCacheStore(work_dir / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    return store.root / "v1"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_plan_is_deterministic_and_enforces_per_section_limit(work_dir: Path):
    records = []
    for index in range(4):
        image_path = _image(work_dir / f"candidate-{index}.png")
        records.append(
            _record(
                image_path,
                chunk_id=f"vis-{index}",
                paper_id=f"paper-{index}",
                caption=(
                    "Optical resonance mechanism with field confinement "
                    f"variant {index}."
                ),
                search_text=(
                    "optical resonance field confinement mechanism cavity "
                    f"variant {index}"
                ),
            )
        )
    section = _section(
        "S01",
        text=(
            "The optical resonance mechanism confines the field in the "
            "resonant cavity and enables sensing."
        ),
        role="Explain the optical resonance mechanism.",
        title="Resonance mechanism",
    )
    first = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    second = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    assert first == second
    assert len(first["placements"]) <= 2
    assert len(first["sections"][0]["placements"]) <= 2
    assert first["validation"]["status"] == "passed"


def test_zero_placements_allowed_and_section_still_accounted(work_dir: Path):
    image_path = _image(work_dir / "unrelated.png")
    record = _record(
        image_path,
        chunk_id="vis-unrelated",
        caption="Silicon photodetector packaging robustness details.",
        search_text="silicon packaging robustness photodetector",
        vtype="representative_example",
        utility="medium",
    )
    section = _section(
        "S01",
        text=(
            "Colloidal quantum dots are synthesized by hot injection of "
            "precursors."
        ),
        role="Materials synthesis",
        title="Quantum dot synthesis",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
    )
    assert plan["placements"] == []
    assert plan["conceptual_figure_requests"] == []
    assert any(
        item["section_id"] == "S01"
        and item["reason"] == "zero_visual_selected_acceptable"
        for item in plan["unfilled_visual_needs"]
    )
    assert plan["validation"]["status"] == "passed"
    editorial_path = work_dir / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(editorial_path, plan["visual_editorial_plan"])
    validation = validate_visual_editorial_plan_file(
        editorial_path,
        plan["input_fingerprint"],
        {"S01"},
    )
    assert validation.startswith("VALIDATION_PASSED")


def test_same_image_is_never_reused_across_sections(work_dir: Path):
    shared = _image(work_dir / "shared.png")
    records = [
        _record(
            shared,
            chunk_id="vis-dup-a",
            paper_id="paper-a",
            caption="Optical resonance mechanism with field confinement.",
        ),
        _record(
            shared,
            chunk_id="vis-dup-b",
            paper_id="paper-b",
            caption="Optical resonance mechanism with field confinement.",
        ),
    ]
    section_text = (
        "The optical resonance mechanism confines the field in the "
        "resonant cavity and enables sensing."
    )
    sections = [
        _section(
            "S01",
            text=section_text,
            role="Explain the optical resonance mechanism.",
        ),
        _section(
            "S02",
            text=section_text,
            role="Explain the optical resonance mechanism.",
        ),
    ]
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=records,
    )
    assert len(plan["placements"]) == 1
    selected_paths = {
        placement["local_image_path"] for placement in plan["placements"]
    }
    assert selected_paths == {str(shared)}
    section_counts = {
        item["section_id"]: item["placement_count"]
        for item in plan["sections"]
    }
    assert section_counts["S01"] + section_counts["S02"] == 1
    assert section_counts["S02"] == 0
    assert len({item["visual_chunk_id"] for item in plan["placements"]}) == 1
    assert any(
        item["reason"] == "zero_visual_selected_acceptable"
        for item in plan["unfilled_visual_needs"]
    )


def test_whole_figure_preferred_for_broad_mechanism_section(work_dir: Path):
    parent = _image(work_dir / "parent.png")
    sub_a = _image(work_dir / "sub-a.png")
    sub_b = _image(work_dir / "sub-b.png")
    records = [
        _record(
            parent,
            chunk_id="parent-1",
            paper_id="paper-parent",
            caption=(
                "Optical resonance mechanism overview showing field "
                "confinement and sensing."
            ),
            search_text=(
                "optical resonance mechanism field confinement sensing "
                "overview"
            ),
            kind="composite_parent",
        ),
        _record(
            sub_a,
            chunk_id="sub-a",
            paper_id="paper-a",
            caption="Field confinement panel detail.",
            search_text="field confinement panel detail",
            kind="subfigure",
            parent_chunk_id="parent-1",
            subfigure_label="a",
            confidence="medium",
        ),
        _record(
            sub_b,
            chunk_id="sub-b",
            paper_id="paper-b",
            caption="Sensing response panel detail.",
            search_text="sensing response panel detail",
            kind="subfigure",
            parent_chunk_id="parent-1",
            subfigure_label="b",
            confidence="medium",
        ),
    ]
    section = _section(
        "S01",
        text=(
            "The resonant cavity confines the optical field and enables "
            "sensing."
        ),
        role="Explain the optical resonance mechanism and the sensing principle.",
        title="Optical resonance mechanism",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    assert len(plan["placements"]) == 1
    placement = plan["placements"][0]
    assert placement["visual_chunk_id"] == "parent-1"
    assert placement["figure_mode"] == "whole_figure"
    assert placement["transformation_need"]["kind"] == "none"
    assert placement["lineage"]["composite"] is False


def test_coherent_subfigures_become_one_composite_placement(work_dir: Path):
    parent = _image(work_dir / "parent.png")
    sub_a = _image(work_dir / "sub-a.png")
    sub_b = _image(work_dir / "sub-b.png")
    records = [
        _record(
            parent,
            chunk_id="parent-1",
            paper_id="paper-parent",
            caption="Optical resonance overview of a resonant cavity.",
            search_text="optical resonance overview resonant cavity",
            kind="composite_parent",
            confidence="medium",
        ),
        _record(
            sub_a,
            chunk_id="sub-a",
            paper_id="paper-a",
            caption="Field confinement panel inside the resonant cavity.",
            search_text="field confinement panel resonant cavity",
            kind="subfigure",
            parent_chunk_id="parent-1",
            subfigure_label="a",
        ),
        _record(
            sub_b,
            chunk_id="sub-b",
            paper_id="paper-b",
            caption="Sensing response panel of the resonant cavity.",
            search_text="sensing response panel resonant cavity",
            kind="subfigure",
            parent_chunk_id="parent-1",
            subfigure_label="b",
        ),
    ]
    section = _section(
        "S01",
        text=(
            "The field confinement panel and the sensing response panel "
            "together illustrate the resonant cavity mechanism."
        ),
        role=(
            "Compare the field confinement panel and the sensing response "
            "panel in the resonant cavity."
        ),
        title="Resonant cavity panels",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    assert len(plan["placements"]) == 1
    placement = plan["placements"][0]
    assert placement["figure_mode"] == "subfigure"
    assert placement["composite_group_id"].startswith("COMP-")
    assert len(placement["panel_manifest"]) == 2
    assert placement["transformation_need"]["kind"] == "compose_subfigures"
    editorial_placements = plan["visual_editorial_plan"]["placements"]
    assert len(editorial_placements) == 2
    assert {
        item["composite_group_id"] for item in editorial_placements
    } == {placement["composite_group_id"]}
    assert {item["visual_chunk_id"] for item in editorial_placements} == {
        "sub-a",
        "sub-b",
    }


def test_permission_handling_allowed_review_and_restricted(work_dir: Path):
    allowed_path = _image(work_dir / "allowed.png")
    review_path = _image(work_dir / "review.png")
    restricted_path = _image(work_dir / "restricted.png")
    records = [
        _record(
            allowed_path,
            chunk_id="vis-allowed",
            paper_id="paper-allowed",
            permission="allowed",
            status="ok",
        ),
        _record(
            review_path,
            chunk_id="vis-review",
            paper_id="paper-review",
            permission="requires_review",
            status="ok",
        ),
        _record(
            restricted_path,
            chunk_id="vis-restricted",
            paper_id="paper-restricted",
            permission="restricted",
            status="ok",
        ),
    ]
    section = _section(
        "S01",
        text=(
            "The optical resonance mechanism confines the field in the "
            "resonant cavity."
        ),
        role="Explain the optical resonance mechanism.",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    assert len(plan["placements"]) == 2
    by_chunk = {
        item["visual_chunk_id"]: item for item in plan["placements"]
    }
    assert by_chunk["vis-allowed"]["status"] == "verified_existing"
    assert by_chunk["vis-allowed"]["image_review_required"] is False
    assert by_chunk["vis-review"]["status"] == (
        "traceable_source_pending_review"
    )
    assert by_chunk["vis-review"]["image_review_required"] is True
    assert by_chunk["vis-review"]["permission"]["status"] == (
        "requires_review"
    )
    assert "vis-restricted" not in by_chunk
    assert any(
        item["reason"] == "permission_restricted_candidate_excluded"
        for item in plan["unfilled_visual_needs"]
    )


def test_coverage_gap_is_unfilled_and_never_blocks_prose(work_dir: Path):
    image_path = _image(work_dir / "mechanism.png")
    record = _record(
        image_path,
        chunk_id="vis-mechanism",
        caption="Optical resonance mechanism with field confinement.",
    )
    section = _section(
        "S01",
        text="Resonant cavity field confinement analysis.",
        role="Resonant cavity field confinement analysis",
        title="Resonance analysis",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
    )
    assert len(plan["placements"]) == 1
    assert plan["coverage_audit"]["satisfied"] == [
        "mechanism_or_conceptual_diagram"
    ]
    assert plan["coverage_audit"]["unsatisfied"] == [
        "workflow_or_process_diagram"
    ]
    assert plan["coverage_audit"]["status"] == "degraded"
    assert any(
        item["reason"] == "article_coverage_requirement_unmet"
        and item["need_kind"] == "workflow_or_process_diagram"
        for item in plan["unfilled_visual_needs"]
    )
    assert plan["validation"]["status"] == "passed"
    assert any(
        "coverage_requirement_unmet" in warning
        for warning in plan["validation"]["warnings"]
    )


def test_coverage_requirements_can_be_satisfied_by_conceptual_requests():
    sections = [
        _section(
            "S01",
            text="The optical resonance mechanism confines the field.",
            role="Explain the physical mechanism.",
            title="Mechanism",
        ),
        _section(
            "S02",
            text="Deposition and patterning form the multilayer workflow.",
            role="Describe the fabrication workflow and process.",
            title="Workflow",
        ),
    ]
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=[],
    )
    kinds = {
        request["figure_kind"]
        for request in plan["conceptual_figure_requests"]
    }
    assert "mechanism_schematic" in kinds
    assert "workflow_schematic" in kinds
    assert plan["coverage_audit"]["status"] == "satisfied"
    assert plan["coverage_audit"]["unsatisfied"] == []
    for request in plan["conceptual_figure_requests"]:
        assert request["required_disclosure"] == (
            "AI-generated explanatory visual"
        )
        assert request["empirical_content"] is False
        assert request["data_provenance_level"] == "schematic"
        assert request["input_data"] == {}
        assert request["generation_safety"]["eligible"] is True
        assert request["status"] == "pending_generation_and_review"


def test_generation_safety_blocks_empirical_only_sections():
    section = _section(
        "S01",
        text=(
            "We measured reflectance spectra and simulated transmission "
            "curves for the multilayer stack."
        ),
        role="Experimental results",
        title="Measured spectra",
        expected=("quantitative_comparison",),
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[],
    )
    assert plan["conceptual_figure_requests"] == []
    assert any(
        item["reason"] == "empirical_visual_generation_forbidden"
        and item["need_kind"] == "empirical_visual_need"
        for item in plan["unfilled_visual_needs"]
    )
    assert plan["generation_safety"]["approved_request_count"] == 0


def test_editorial_plan_passes_editor_and_factory_contracts(work_dir: Path):
    mechanism_image = _image(work_dir / "mechanism.png", "navy")
    workflow_image = _image(work_dir / "workflow.png", "darkred")
    records = [
        _record(
            mechanism_image,
            chunk_id="vis-mechanism",
            paper_id="paper-mechanism",
            caption="Optical resonance mechanism with field confinement.",
            vtype="mechanism_anchor",
        ),
        _record(
            workflow_image,
            chunk_id="vis-workflow",
            paper_id="paper-workflow",
            caption=(
                "Fabrication process flow for multilayer optical filters "
                "with deposition and patterning."
            ),
            search_text=(
                "fabrication process flow multilayer optical filter "
                "deposition patterning"
            ),
            vtype="method_or_workflow",
        ),
    ]
    sections = [
        _section(
            "S01",
            text="Resonant cavity field confinement analysis.",
            role="Resonant cavity field confinement analysis",
        ),
        _section(
            "S02",
            text="Multilayer optical filter fabrication.",
            role="Multilayer optical filter fabrication",
        ),
    ]
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=records,
    )
    assert plan["visual_editorial_plan"]["schema_version"] == (
        "research_harness.visual_editorial_plan.v1"
    )
    editorial_path = work_dir / "VISUAL_EDITORIAL_PLAN.json"
    _write_json(editorial_path, plan["visual_editorial_plan"])
    validation = validate_visual_editorial_plan_file(
        editorial_path,
        plan["input_fingerprint"],
        {"S01", "S02"},
    )
    assert validation.startswith("VALIDATION_PASSED")

    package = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=work_dir / "factory",
            test_mode=True,
            real_visual_audit=False,
            real_image_generation=False,
        )
    ).run(
        visual_plan_path=editorial_path,
        blueprint={"sections": [{"section_id": "S01"}, {"section_id": "S02"}]},
        review_work_dir=work_dir / "review",
    )
    assert package["validation"]["status"] == "passed"
    assert len(package["figures"]) == 2
    assert {figure["figure_type"] for figure in package["figures"]} == {
        "source_single"
    }


def test_fail_open_partial_output_on_malformed_inputs(work_dir: Path):
    good_image = _image(work_dir / "good.png")
    good_record = _record(
        good_image,
        chunk_id="vis-good",
        caption="Optical resonance mechanism with field confinement.",
    )
    good_section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
    )
    plan = plan_article_visual_assets(
        sections=[
            {"title": "missing section id"},
            good_section,
        ],
        visual_cache_records=[
            good_record,
            {"paper_id": "broken", "local_image_path": ""},
            "not-a-dict",
        ],
    )
    assert plan["validation"]["status"] == "degraded"
    assert plan["validation"]["errors"]
    assert plan["validation"]["fail_open"] is True
    assert len(plan["sections"]) == 1
    assert len(plan["placements"]) == 1
    assert plan["placements"][0]["visual_chunk_id"] == "vis-good"


def test_final_candidates_carry_explicit_image_review_state(work_dir: Path):
    ok_image = _image(work_dir / "ok.png", "navy")
    pending_image = _image(work_dir / "pending.png", "darkred")
    records = [
        _record(
            ok_image,
            chunk_id="vis-ok",
            caption="Optical resonance mechanism with field confinement.",
            status="ok",
            permission="allowed",
        ),
        _record(
            pending_image,
            chunk_id="vis-pending",
            caption="Optical resonance sensing response in the cavity.",
            search_text="optical resonance sensing response cavity",
            status="pending_multimodal_review",
            permission="allowed",
            vtype="trend_or_parameter_map",
        ),
    ]
    section = _section(
        "S01",
        text=(
            "The optical resonance mechanism confines the field and the "
            "sensing response in the cavity."
        ),
        role="Explain the optical resonance mechanism and sensing response.",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
    )
    by_chunk = {
        item["visual_chunk_id"]: item for item in plan["placements"]
    }
    assert by_chunk["vis-ok"]["image_review_required"] is False
    assert by_chunk["vis-ok"]["review_state"] == "verified_existing"
    assert by_chunk["vis-pending"]["image_review_required"] is True
    assert by_chunk["vis-pending"]["review_state"] == (
        "traceable_source_pending_review"
    )
    queue = plan["image_review_queue"]
    assert "Tag/vector shortlist first" in queue["policy"]
    pending_items = [
        item
        for item in queue["items"]
        if item["visual_chunk_id"] == "vis-pending"
    ]
    assert pending_items and pending_items[0]["review_required"] is True


def test_weak_filler_fail_open_only_option_but_not_when_stronger_exists(
    work_dir: Path,
):
    weak_image = _image(work_dir / "weak.png")
    weak_record = _record(
        weak_image,
        chunk_id="vis-weak",
        caption="Optical resonance mechanism field confinement low utility.",
        search_text="optical resonance mechanism field confinement",
        vtype="mechanism_anchor",
        utility="low",
        confidence="low",
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak_record],
    )
    # Fail-open: the only lexically relevant traceable candidate is kept as
    # an explicit pending/review-required placement.
    assert [p["visual_chunk_id"] for p in plan["placements"]] == ["vis-weak"]
    placement = plan["placements"][0]
    assert placement["status"] == "traceable_source_pending_review"
    assert placement["image_review_required"] is True
    assert "fail_open_weak_or_pending_only_relevant_candidate" in (
        placement["review_required_reason"]
    )

    # A stronger candidate still wins; the weak filler is not promoted.
    strong_image = _image(work_dir / "strong.png")
    strong_record = _record(
        strong_image,
        chunk_id="vis-strong",
        caption="Optical resonance mechanism showing field confinement.",
        search_text="optical resonance mechanism field confinement",
        vtype="mechanism_anchor",
        utility="high",
        confidence="high",
    )
    plan_strong = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak_record, strong_record],
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1
        ),
    )
    assert [p["visual_chunk_id"] for p in plan_strong["placements"]] == [
        "vis-strong"
    ]
    assert "vis-weak" not in {
        p["visual_chunk_id"] for p in plan_strong["placements"]
    }


def test_adapter_reads_manuscript_and_sqlite_cache_and_writes_artifacts(
    work_dir: Path,
):
    image_path = _image(work_dir / "source.png")
    kb_path = work_dir / "visual.sqlite"
    conn = sqlite3.connect(str(kb_path))
    try:
        conn.execute(
            """
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT,
              doi TEXT,
              title TEXT,
              caption TEXT,
              chunk_kind TEXT,
              search_text TEXT,
              visual_argument_type TEXT,
              visual_argument_status TEXT,
              visual_argument_confidence TEXT,
              visual_argument_claim TEXT,
              visual_argument_needs_human_review INTEGER,
              visual_argument_schema_version TEXT,
              local_image_path TEXT,
              raw_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO visual_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "vis-sqlite-1",
                "paper-sqlite",
                "10.1/sqlite",
                "Resonant optical sensing study",
                "Optical resonance mechanism with field confinement.",
                "single_figure",
                "optical resonance field confinement sensing mechanism",
                "mechanism_anchor",
                "ok",
                "high",
                "Optical resonance mechanism with field confinement.",
                0,
                "visual_argument_protocol.v1",
                str(image_path),
                json.dumps({"permission": {"status": "allowed"}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    draft_path = work_dir / "SECTION_DRAFT_EN.md"
    draft_path.write_text(
        "The optical resonance mechanism confines the field in the "
        "resonant cavity and enables sensing.",
        encoding="utf-8",
    )
    output_dir = work_dir / "out"
    plan = run_article_visual_asset_planner(
        sections=[
            {
                "section_id": "S01",
                "title": "Resonance mechanism",
                "argument_role": (
                    "Explain the optical resonance mechanism and field "
                    "confinement."
                ),
            }
        ],
        section_text_files={"S01": draft_path},
        visual_cache_paths=[kb_path],
        output_dir=output_dir,
    )
    assert (output_dir / "VISUAL_CONSTRUCTION_PLAN.json").is_file()
    assert (output_dir / "VISUAL_EDITORIAL_PLAN.json").is_file()
    assert (
        output_dir / "ARTICLE_VISUAL_IMAGE_REVIEW_QUEUE.json"
    ).is_file()
    assert plan["input_fingerprint"]
    assert len(plan["placements"]) == 1
    assert plan["placements"][0]["visual_chunk_id"] == "vis-sqlite-1"
    editorial_path = output_dir / "VISUAL_EDITORIAL_PLAN.json"
    validation = validate_visual_editorial_plan_file(
        editorial_path,
        plan["input_fingerprint"],
        {"S01"},
    )
    assert validation.startswith("VALIDATION_PASSED")


def test_validation_is_deterministic_and_checks_duplicate_paths(
    work_dir: Path,
):
    image_path = _image(work_dir / "source.png")
    plan = plan_article_visual_assets(
        sections=[_section("S01", role="Explain the mechanism.")],
        visual_cache_records=[
            _record(
                image_path,
                chunk_id="vis-one",
                caption="Optical resonance mechanism with field confinement.",
            )
        ],
    )
    report = validate_visual_construction_plan(plan)
    assert report == validate_visual_construction_plan(plan)
    assert report["status"] == "passed"

    tampered = dict(plan)
    tampered["placements"] = [
        dict(plan["placements"][0]),
        dict(plan["placements"][0]),
    ]
    duplicated = validate_visual_construction_plan(tampered)
    assert duplicated["status"] == "degraded"
    assert any("duplicate_image" in error for error in duplicated["errors"])


def test_config_can_relax_or_tighten_limits(work_dir: Path):
    images = [_image(work_dir / f"img-{index}.png") for index in range(4)]
    records = [
        _record(
            images[index],
            chunk_id=f"vis-{index}",
            paper_id=f"paper-{index}",
            caption=(
                "Optical resonance mechanism with field confinement "
                f"variant {index}."
            ),
            search_text=(
                "optical resonance field confinement mechanism cavity "
                f"variant {index}"
            ),
        )
        for index in range(4)
    ]
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
    )
    tight = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1
        ),
    )
    assert len(tight["placements"]) == 1
    loose = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=2
        ),
    )
    assert len(loose["placements"]) == 2


def test_adapter_loads_published_snapshot_directory_and_sqlite(
    work_dir: Path,
):
    snapshot = _publish_snapshot(work_dir)
    section = _section(
        "S01",
        text=(
            "The spectrum of the selective emitter shows narrow emission "
            "in the atmospheric window."
        ),
        role="Explain the emitter spectrum.",
        title="Emitter spectrum",
    )
    plan_from_dir = run_article_visual_asset_planner(
        sections=[section],
        visual_cache_paths=[snapshot],
        output_dir=work_dir / "out-dir",
    )
    plan_from_sqlite = run_article_visual_asset_planner(
        sections=[section],
        visual_cache_paths=[snapshot / "visual_cache.sqlite"],
        output_dir=work_dir / "out-sqlite",
    )
    assert plan_from_dir == plan_from_sqlite
    assert len(plan_from_dir["placements"]) == 1
    placement = plan_from_dir["placements"][0]
    assert placement["figure_mode"] == "subfigure"
    assert placement["lineage"]["parent_chunk_id"]
    assert placement["permission"]["status"] == "requires_review"
    assert placement["status"] == "traceable_source_pending_review"
    assert placement["image_review_required"] is True
    image_path = Path(placement["local_image_path"])
    assert image_path.is_file()
    assert str(image_path).startswith(str(snapshot / "assets"))
    assert placement["paper_id"] == "snap-paper"
    assert placement["doi"] == "10.1/snapshot"
    assert "spectrum" in placement["caption_preview"].lower()
    assert (work_dir / "out-dir" / "VISUAL_EDITORIAL_PLAN.json").is_file()
    assert (work_dir / "out-sqlite" / "VISUAL_EDITORIAL_PLAN.json").is_file()

    records = load_visual_cache_records(snapshot)
    assert len(records) == 2
    assert {record["chunk_kind"] for record in records} == {
        "parent_figure",
        "subfigure",
    }
    assert all(
        record["permission"]["use_permission"] == "factual_support"
        for record in records
    )
    parent_id = next(
        record["chunk_id"]
        for record in records
        if record["chunk_kind"] == "parent_figure"
    )
    assert placement["visual_chunk_id"] != parent_id


def test_adapter_loads_snapshot_units_json_payload(work_dir: Path):
    snapshot = _publish_snapshot(work_dir)
    section = _section(
        "S01",
        text=(
            "The spectrum of the selective emitter shows narrow emission "
            "in the atmospheric window."
        ),
        role="Explain the emitter spectrum.",
    )
    plan = run_article_visual_asset_planner(
        sections=[section],
        visual_cache_paths=[snapshot / "units.json"],
        output_dir=work_dir / "out-json",
    )
    assert len(plan["placements"]) == 1
    assert Path(plan["placements"][0]["local_image_path"]).is_file()
    assert (work_dir / "out-json" / "VISUAL_CONSTRUCTION_PLAN.json").is_file()


def test_snapshot_approval_and_permission_adaptation(work_dir: Path):
    source = work_dir / "source"
    source.mkdir(parents=True)
    approved_image = _image(source / "approved.png", "navy")
    pending_image = _image(source / "pending.png", "purple")
    rejected_image = _image(source / "rejected.png", "darkred")
    records = [
        _snapshot_candidate(
            chunk_id="snap-approved",
            kind="single_figure",
            subfigure_label="",
            image_path=approved_image,
            parent_image_path=approved_image,
            caption="Approved optical mechanism figure.",
            visual_argument_type="mechanism_anchor",
            review_decision="human_approved",
            license_text="cc-by-4.0",
            extra={"visual_argument_status": "ok"},
        ),
        _snapshot_candidate(
            chunk_id="snap-pending",
            kind="single_figure",
            subfigure_label="",
            image_path=pending_image,
            parent_image_path=pending_image,
            caption="Pending optical mechanism figure.",
            visual_argument_type="mechanism_anchor",
        ),
        _snapshot_candidate(
            chunk_id="snap-rejected",
            kind="single_figure",
            subfigure_label="",
            image_path=rejected_image,
            parent_image_path=rejected_image,
            caption="Rejected optical mechanism figure.",
            visual_argument_type="mechanism_anchor",
            review_decision="human_rejected",
        ),
    ]
    assets = work_dir / "assets"
    units, report = ingest_visual_candidates(
        records,
        source_root=source,
        copy_assets_to=assets,
    )
    assert report["errors"] == []
    store = VisualCacheStore(work_dir / "cache")
    store.publish_snapshot(
        version="v1",
        units=units,
        assets_dir=assets,
        path_roots={"source": source},
    )
    snapshot = store.root / "v1"
    records_from_dir = load_visual_cache_records(snapshot)
    records_from_sqlite = load_visual_cache_records(
        snapshot / "visual_cache.sqlite"
    )
    assert records_from_sqlite == records_from_dir
    by_legacy = {
        record["legacy_chunk_id"]: record
        for record in records_from_dir
    }
    approved = by_legacy["snap-approved"]
    pending = by_legacy["snap-pending"]
    rejected = by_legacy["snap-rejected"]
    assert approved["visual_argument_status"] == "ok"
    assert approved["visual_argument_needs_human_review"] is False
    assert approved["permission"]["status"] == "allowed"
    assert approved["permission"]["license"] == "cc-by-4.0"
    assert pending["visual_argument_status"] == "pending_multimodal_review"
    assert pending["visual_argument_needs_human_review"] is True
    assert pending["permission"]["status"] == "requires_review"
    assert rejected["visual_argument_status"] == "failed"


def test_snapshot_explicit_reuse_marker_and_approval_marker(work_dir: Path):
    custom = work_dir / "custom-snapshot"
    assets_dir = custom / "assets"
    assets_dir.mkdir(parents=True)
    image_path = _image(assets_dir / "explicit.png")
    unit = {
        "schema_version": "optomind.visual_unit.v1",
        "unit_id": "unit:visual:custom-explicit",
        "unit_kind": "single_figure",
        "unit_role": "review_asset",
        "source_identity": {
            "paper_id": "custom-paper",
            "doi": "10.1/custom",
        },
        "figure_identity": {
            "asset_id": "custom-fig",
            "figure_label": "Figure C",
        },
        "caption": {"clean": "Optical resonance mechanism diagram."},
        "semantic": {
            "description": (
                "Optical resonance mechanism with field confinement."
            ),
            "tags": ["mechanism", "resonance"],
        },
        "argumentative_roles": {
            "primary": "mechanism_anchor",
            "confidence": "high",
        },
        "provenance": {},
        "permission_state": {
            "use_permission": "discovery_only",
            "visual_reuse_permission": "allowed",
        },
        "hashes": {},
        "vector_refs": {},
        "lineage": {},
        "use_history": {},
        "review": {},
        "approval": {
            "state": "approved",
            "source_marker": "human_approved",
        },
        "paths": {
            "image_ref": {
                "root": "snapshot",
                "relative": "assets/explicit.png",
            }
        },
        "created_at": "",
    }
    (custom / "units.json").write_text(
        json.dumps({"units": [unit]}, ensure_ascii=False),
        encoding="utf-8",
    )
    records = load_visual_cache_records(custom)
    assert len(records) == 1
    assert records[0]["visual_argument_status"] == "ok"
    assert records[0]["visual_argument_needs_human_review"] is False
    assert records[0]["permission"]["status"] == "allowed"
    assert records[0]["permission"]["explicit_reuse_permission"] is True
    assert Path(records[0]["local_image_path"]).is_file()


def test_generated_snapshot_without_paper_identity_loads_and_plans(
    work_dir: Path,
):
    custom = work_dir / "generated-snapshot"
    assets_dir = custom / "assets"
    assets_dir.mkdir(parents=True)
    image_path = _image(assets_dir / "generated.png", "lightblue")
    disclosure = "AI-generated explanatory visual; not empirical evidence."
    unit = {
        "schema_version": "optomind.visual_unit.v1",
        "unit_id": "unit:visual:generated-001",
        "unit_kind": "generated_visual",
        "unit_role": "review_asset",
        "source_identity": {
            "paper_id": "",
            "doi": "",
            "source_kind": "ai_generated_explanatory_visual",
            "rights": {
                "disclosure": disclosure,
                "attribution_kind": "none",
                "doi_policy": "no_source_doi",
            },
            "title": "Article-owned generated visual",
        },
        "figure_identity": {
            "asset_id": "gen-fig",
            "figure_label": "FIG-GEN-001",
        },
        "caption": {
            "clean": (
                "Conceptual resonant mechanism schematic. "
                + disclosure
            ),
            "disclosure": disclosure,
        },
        "semantic": {
            "description": (
                "Resonant mechanism concept with field confinement."
            ),
            "tags": ["mechanism", "resonance"],
        },
        "argumentative_roles": {
            "primary": "mechanism_anchor",
            "confidence": "high",
            "review_utility": "high",
        },
        "provenance": {"generation_disclosure": disclosure},
        "permission_state": {
            "use_permission": "discovery_only",
            "evidence_ceiling": "explanatory_only",
            "empirical_evidence_allowed": False,
            "quantitative_evidence_allowed": False,
        },
        "hashes": {},
        "vector_refs": {"entries": []},
        "lineage": {
            "generation_status": "ai_generated",
            "generation": {
                "prompt": "Draw a mechanism schematic.",
                "model_version": "mock",
                "disclosure": disclosure,
                "created_at": "2026-08-13T00:00:00+00:00",
                "review_history": [
                    {"decision": "human_approved"}
                ],
                "attempt_history": [],
            },
            "enhancement_history": [],
        },
        "use_history": {"used_in_run_ids": []},
        "review": {
            "review_decision": "human_approved",
            "human_review_status": "approved",
            "needs_human_review": False,
        },
        "approval": {
            "state": "approved",
            "source_marker": "human_approved",
            "approved_at": "2026-08-13T00:05:00+00:00",
        },
        "crop_hygiene": {
            "schema_version": "optomind.visual_crop_hygiene.v1",
            "status": "clean",
            "source_kind": "ai_generated_explanatory_visual",
            "reason": "generated_visual_no_caption_band",
            "evidence": {},
            "derivative": None,
        },
        "paths": {
            "image_ref": {
                "root": "snapshot",
                "relative": "assets/generated.png",
            },
            "source_ref": {
                "root": "unbound",
                "relative": "generated.png",
            },
        },
        "created_at": "2026-08-13T00:05:00+00:00",
    }
    (custom / "units.json").write_text(
        json.dumps({"units": [unit]}, ensure_ascii=False),
        encoding="utf-8",
    )

    records = load_visual_cache_records(custom)
    assert len(records) == 1
    record = records[0]
    assert record["chunk_kind"] == "generated_visual"
    assert record["paper_id"].startswith("generated:")
    assert record["doi"] == ""
    assert record["generated_visual"] is True
    assert record["source_route"] == "generated_cache"
    assert record["required_disclosure"] == (
        "AI-generated explanatory visual"
    )
    assert record["visual_argument_status"] == "ok"
    assert Path(record["local_image_path"]).is_file()

    section = _section(
        "S01",
        text="Explain the resonant mechanism conceptually.",
        role="Explain the mechanism.",
        title="Resonant mechanism",
        expected=("mechanism_anchor",),
    )
    plan = run_article_visual_asset_planner(
        sections=[section],
        visual_cache_paths=[custom],
        output_dir=work_dir / "out-generated",
    )
    assert not any(
        "visual_cache_record" in error
        for error in plan["validation"]["errors"]
    )
    assert len(plan["placements"]) == 1
    placement = plan["placements"][0]
    assert placement["paper_id"].startswith("generated:")
    assert placement["doi"] == ""
    assert placement["figure_kind"] == "generated_explanatory_visual"
    assert placement["generated_visual"] is True
    assert placement["required_disclosure"] == (
        "AI-generated explanatory visual"
    )
    assert "AI-generated explanatory visual" in placement[
        "caption_proposal"
    ]
    assert "Source:" not in placement["caption_proposal"]
    validation = validate_visual_editorial_plan_file(
        work_dir / "out-generated" / "VISUAL_EDITORIAL_PLAN.json"
    )
    assert validation.startswith("VALIDATION_PASSED")


def test_source_record_without_paper_identity_is_rejected(work_dir: Path):
    image = _image(work_dir / "source-no-paper.png")
    record = _record(
        image,
        chunk_id="source-no-paper",
        paper_id="",
        doi="",
    )
    plan = plan_article_visual_assets(
        sections=[
            _section(
                "S01",
                text="Optical resonance mechanism.",
                role="mechanism",
            )
        ],
        visual_cache_records=[record],
    )
    assert any(
        "visual_cache_record[0]_invalid" in error
        or "missing paper_id" in error
        for error in plan["validation"]["errors"]
    )


def test_caption_missing_downranks_but_does_not_exclude(
    work_dir: Path,
) -> None:
    captioned_image = _image(work_dir / "captioned.png", "navy")
    missing_image = _image(work_dir / "missing.png", "maroon")
    captioned = _record(
        captioned_image,
        chunk_id="cap-1",
        caption="Optical resonance mechanism showing field confinement.",
        caption_missing=False,
    )
    missing = _record(
        missing_image,
        chunk_id="nocap-1",
        caption="Caption unavailable; inspect the source figure.",
        caption_missing=True,
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    config = ArticleVisualAssetPlannerConfig(max_placements_per_section=1)

    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[captioned, missing],
        config=config,
    )
    assert plan["placements"][0]["visual_chunk_id"] == "cap-1"

    # Fail-open: a missing-caption candidate is still selectable when it is
    # the only relevant traceable asset.
    plan_only_missing = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[missing],
        config=config,
    )
    assert plan_only_missing["placements"][0]["visual_chunk_id"] == "nocap-1"
    assert plan_only_missing["validation"]["status"] == "passed"


def test_fail_open_selects_best_weak_candidate_when_only_relevant_option(
    work_dir: Path,
) -> None:
    image_a = _image(work_dir / "weak-a.png", "navy")
    image_b = _image(work_dir / "weak-b.png", "maroon")
    image_low = _image(work_dir / "weak-low.png", "teal")
    image_unrelated = _image(work_dir / "unrelated.png", "olive")

    def weak_record(
        image_path: Path,
        *,
        chunk_id: str,
        search_text: str,
    ) -> dict:
        return _record(
            image_path,
            chunk_id=chunk_id,
            caption="Caption unavailable; inspect the source figure.",
            search_text=search_text,
            caption_missing=True,
            status="pending_multimodal_review",
            needs_human_review=True,
            utility="low",
        )

    weak_a = weak_record(
        image_a,
        chunk_id="weak-a",
        search_text="optical resonance mechanism",
    )
    weak_b = weak_record(
        image_b,
        chunk_id="weak-b",
        search_text="optical resonance",
    )
    weak_low = weak_record(
        image_low,
        chunk_id="weak-low",
        search_text="optical",
    )
    unrelated = _record(
        image_unrelated,
        chunk_id="unrelated",
        caption="Real unrelated caption.",
        search_text="stock market volatility clustering",
        status="ok",
        utility="high",
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    config = ArticleVisualAssetPlannerConfig(max_placements_per_section=2)

    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak_b, weak_a, unrelated],
        config=config,
    )
    assert [p["visual_chunk_id"] for p in plan["placements"]] == ["weak-a"]
    placement = plan["placements"][0]
    assert placement["status"] == "traceable_source_pending_review"
    assert placement["image_review_required"] is True
    assert "fail_open_weak_or_pending_only_relevant_candidate" in (
        placement["review_required_reason"]
    )

    # Low-overlap missing-caption/pending-only input still fails open, while
    # the zero-overlap candidate stays excluded.
    plan_low = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak_low, unrelated],
        config=config,
    )
    assert [p["visual_chunk_id"] for p in plan_low["placements"]] == [
        "weak-low"
    ]
    assert plan_low["placements"][0]["status"] == (
        "traceable_source_pending_review"
    )
    assert "unrelated" not in {
        p["visual_chunk_id"] for p in plan_low["placements"]
    }


def test_fail_open_does_not_promote_stronger_restricted_or_failed(
    work_dir: Path,
) -> None:
    strong_image = _image(work_dir / "strong.png", "lime")
    weak_image = _image(work_dir / "weak.png", "cyan")
    restricted_image = _image(work_dir / "restricted.png", "red")
    failed_image = _image(work_dir / "failed.png", "gray")
    strong = _record(
        strong_image,
        chunk_id="strong",
        caption="Optical resonance mechanism showing field confinement.",
        search_text="optical resonance mechanism",
        status="ok",
        utility="high",
    )
    weak = _record(
        weak_image,
        chunk_id="weak",
        caption="Caption unavailable; inspect the source figure.",
        search_text="optical resonance mechanism",
        caption_missing=True,
        status="pending_multimodal_review",
        needs_human_review=True,
        utility="low",
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    config = ArticleVisualAssetPlannerConfig(max_placements_per_section=1)

    # A stronger candidate exists: normal ranking wins and the weak
    # candidate is not promoted.
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak, strong],
        config=config,
    )
    assert [p["visual_chunk_id"] for p in plan["placements"]] == ["strong"]

    # Restricted candidates are never promoted via the fail-open fallback.
    restricted = _record(
        restricted_image,
        chunk_id="restricted",
        caption="Caption unavailable; inspect the source figure.",
        search_text="optical resonance mechanism",
        caption_missing=True,
        status="pending_multimodal_review",
        needs_human_review=True,
        utility="low",
        permission="restricted",
    )
    plan_restricted = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[restricted],
        config=config,
    )
    assert plan_restricted["placements"] == []
    assert any(
        item["reason"] == "permission_restricted_candidate_excluded"
        for item in plan_restricted["unfilled_visual_needs"]
    )

    # Failed/unusable candidates never reach the shortlist or the fallback.
    failed = _record(
        failed_image,
        chunk_id="failed",
        caption="Optical resonance mechanism.",
        search_text="optical resonance mechanism",
        status="failed",
    )
    plan_failed = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[failed],
        config=config,
    )
    assert plan_failed["placements"] == []


def test_shortlist_multi_paper_diversity(work_dir: Path) -> None:
    images = [
        _image(work_dir / f"c{i:02d}.png", "navy") for i in range(16)
    ]
    records = [
        _record(
            images[index],
            chunk_id=f"a-{index:02d}",
            paper_id="paper-a",
            caption="Optical resonance mechanism showing field confinement.",
            search_text="optical resonance mechanism field confinement",
            vtype="mechanism_anchor",
        )
        for index in range(12)
    ]
    records.extend(
        _record(
            images[12 + index],
            chunk_id=f"b-{index:02d}",
            paper_id="paper-b",
            caption="Optical resonance mechanism showing field confinement.",
            search_text="optical resonance mechanism field confinement",
            vtype="mechanism_anchor",
        )
        for index in range(4)
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    config = ArticleVisualAssetPlannerConfig(
        max_placements_per_section=6,
        shortlist_top_k=12,
        shortlist_max_per_paper=3,
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=records,
        config=config,
    )
    papers = {placement["paper_id"] for placement in plan["placements"]}
    assert "paper-a" in papers
    assert "paper-b" in papers
    assert plan["validation"]["status"] == "passed"


def test_crop_contamination_downranks_but_does_not_exclude(
    work_dir: Path,
) -> None:
    clean_image = _image(work_dir / "crop-clean.png", "navy")
    contam_image = _image(work_dir / "crop-contam.png", "maroon")
    clean = _record(
        clean_image,
        chunk_id="crop-clean",
        caption="Optical resonance mechanism showing field confinement.",
        crop_contamination=False,
    )
    contaminated = _record(
        contam_image,
        chunk_id="crop-contam",
        caption="Optical resonance mechanism showing field confinement.",
        crop_contamination=True,
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    config = ArticleVisualAssetPlannerConfig(max_placements_per_section=1)
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[contaminated, clean],
        config=config,
    )
    assert [p["visual_chunk_id"] for p in plan["placements"]] == [
        "crop-clean"
    ]
    plan_only = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[contaminated],
        config=config,
    )
    assert [p["visual_chunk_id"] for p in plan_only["placements"]] == [
        "crop-contam"
    ]


def test_permission_status_preserved_through_planner(
    work_dir: Path,
) -> None:
    allowed_image = _image(work_dir / "perm-allowed.png", "lime")
    pending_image = _image(work_dir / "perm-pending.png", "teal")
    allowed = _record(
        allowed_image,
        chunk_id="perm-allowed",
        caption="Optical resonance mechanism showing field confinement.",
        permission="allowed",
    )
    pending = _record(
        pending_image,
        chunk_id="perm-pending",
        caption="Optical resonance mechanism showing field confinement.",
        permission="requires_review",
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[allowed, pending],
        config=ArticleVisualAssetPlannerConfig(max_placements_per_section=2),
    )
    by_chunk = {
        placement["visual_chunk_id"]: placement
        for placement in plan["placements"]
    }
    assert by_chunk["perm-allowed"]["permission"]["status"] == "allowed"
    assert by_chunk["perm-allowed"]["permission"]["license"] == "CC-BY"
    assert by_chunk["perm-pending"]["permission"]["status"] == (
        "requires_review"
    )


def test_page_prose_contamination_gets_strong_penalty(
    work_dir: Path,
) -> None:
    clean_image = _image(work_dir / "prose-clean.png", "navy")
    crop_image = _image(work_dir / "prose-crop.png", "maroon")
    prose_image = _image(work_dir / "prose-page.png", "teal")
    clean = _record(
        clean_image,
        chunk_id="prose-clean",
        caption="Optical resonance mechanism showing field confinement.",
    )
    crop = _record(
        crop_image,
        chunk_id="prose-crop",
        caption="Optical resonance mechanism showing field confinement.",
        crop_contamination=True,
    )
    prose = _record(
        prose_image,
        chunk_id="prose-page",
        caption="Optical resonance mechanism showing field confinement.",
        page_prose_contamination=True,
    )
    section = _section(
        "S01",
        text="The optical resonance mechanism confines the field.",
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[prose, crop, clean],
        config=ArticleVisualAssetPlannerConfig(max_placements_per_section=3),
    )
    assert [p["visual_chunk_id"] for p in plan["placements"]] == [
        "prose-clean",
        "prose-crop",
        "prose-page",
    ]
    # Fail-open: page-prose candidate still selectable when nothing cleaner.
    plan_only = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[prose],
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1
        ),
    )
    assert [p["visual_chunk_id"] for p in plan_only["placements"]] == [
        "prose-page"
    ]


def test_article_level_source_diversity_soft_penalty(
    work_dir: Path,
) -> None:
    def section_records(index_start: int, images: list) -> list[dict]:
        records = []
        for index in range(3):
            records.append(
                _record(
                    images[index],
                    chunk_id=f"a-{index_start + index}",
                    paper_id="paper-a",
                    caption=(
                        "Optical resonance mechanism showing field "
                        "confinement."
                    ),
                    search_text=(
                        "optical resonance mechanism field confinement"
                    ),
                    vtype="mechanism_anchor",
                )
            )
        for index in range(2):
            records.append(
                _record(
                    images[3 + index],
                    chunk_id=f"b-{index_start + index}",
                    paper_id="paper-b",
                    caption=(
                        "Optical resonance mechanism showing field "
                        "confinement."
                    ),
                    search_text=(
                        "optical resonance mechanism field confinement"
                    ),
                    vtype="mechanism_anchor",
                )
            )
        return records

    images_one = [
        _image(work_dir / f"s1-{index}.png", "navy") for index in range(5)
    ]
    images_two = [
        _image(work_dir / f"s2-{index}.png", "maroon") for index in range(5)
    ]
    sections = [
        _section(
            "S01",
            text="The optical resonance mechanism confines the field.",
            role="Explain the optical resonance mechanism.",
            title="Optical resonance mechanism",
        ),
        _section(
            "S02",
            text="The optical resonance mechanism confines the field.",
            role="Explain the optical resonance mechanism.",
            title="Optical resonance mechanism",
        ),
    ]
    records = [
        *section_records(0, images_one),
        *section_records(5, images_two),
    ]
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=2,
            paper_diversity_penalty=0.05,
        ),
    )
    assert len(plan["placements"]) == 4
    paper_counts: dict[str, int] = {}
    for placement in plan["placements"]:
        paper_counts[placement["paper_id"]] = (
            paper_counts.get(placement["paper_id"], 0) + 1
        )
    # Soft article-level penalty spreads reuse across papers without dropping
    # any candidate or placement.
    assert paper_counts == {"paper-a": 2, "paper-b": 2}
    assert plan["validation"]["status"] == "passed"


def test_article_diversity_prevents_single_paper_monopoly_with_close_alternatives(
    work_dir: Path,
) -> None:
    """v7-like distribution: one paper scores higher, alternatives are close.

    The soft article-level objective (quadratic reuse penalty + unseen-paper
    coverage bonus) must spread placements across papers even without paid
    review, without hard quotas or candidate deletion.
    """

    section_text = (
        "The optical resonance mechanism confines the field in the "
        "resonant cavity and enables sensing."
    )
    section_role = "Explain the optical resonance mechanism."
    section_title = "Optical resonance mechanism"
    caption = (
        "Optical resonance mechanism showing field confinement and "
        "resonant cavity sensing."
    )
    searches = {
        "a": (
            "optical resonance mechanism confines field resonant cavity "
            "sensing enables"
        ),
        "b": "optical resonance mechanism field",
        "c": "optical resonance mechanism",
        "d": "optical resonance",
        "e": "mechanism field",
    }
    pending = dict(
        status="pending_multimodal_review",
        needs_human_review=True,
    )
    sections = []
    records = []
    for section_index in range(1, 6):
        section_id = f"S{section_index:02d}"
        sections.append(
            _section(
                section_id,
                text=section_text,
                role=section_role,
                title=section_title,
            )
        )
        for index in range(2):
            records.append(
                _record(
                    _image(work_dir / f"{section_id}-a-{index}.png", "navy"),
                    chunk_id=f"{section_id}-a-{index}",
                    paper_id="paper-a",
                    caption=caption,
                    search_text=searches["a"],
                    vtype="mechanism_anchor",
                )
            )
        for paper in ("b", "c", "d", "e"):
            for index in range(2):
                records.append(
                    _record(
                        _image(
                            work_dir / f"{section_id}-{paper}-{index}.png",
                            "maroon",
                        ),
                        chunk_id=f"{section_id}-{paper}-{index}",
                        paper_id=f"paper-{paper}",
                        caption=caption,
                        search_text=searches[paper],
                        vtype="mechanism_anchor",
                        **pending,
                    )
                )
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=2,
        ),
    )
    counts: dict[str, int] = {}
    for placement in plan["placements"]:
        counts[placement["paper_id"]] = (
            counts.get(placement["paper_id"], 0) + 1
        )
    assert sum(counts.values()) == 10
    # No single paper monopolizes the article when close alternatives exist.
    assert max(counts.values()) <= 6
    assert len(counts) >= 4
    assert plan["validation"]["status"] == "passed"


def test_clearly_superior_candidate_still_wins(work_dir: Path) -> None:
    strong_image = _image(work_dir / "strong.png", "navy")
    weak_a_image = _image(work_dir / "weak-a.png", "maroon")
    weak_b_image = _image(work_dir / "weak-b.png", "teal")
    section_text = (
        "The optical resonance mechanism confines the field in the "
        "resonant cavity and enables sensing."
    )
    strong = _record(
        strong_image,
        chunk_id="strong-superior",
        paper_id="paper-super",
        caption=(
            "Optical resonance mechanism confines the field in the "
            "resonant cavity and enables sensing."
        ),
        search_text=(
            "optical resonance mechanism confines field resonant cavity "
            "sensing enables"
        ),
        vtype="mechanism_anchor",
        utility="high",
        confidence="high",
    )
    weak_a = _record(
        weak_a_image,
        chunk_id="weak-alt-a",
        paper_id="paper-alt-a",
        caption="Optical resonance mechanism.",
        search_text="optical resonance",
        vtype="mechanism_anchor",
        utility="low",
        confidence="low",
    )
    weak_b = _record(
        weak_b_image,
        chunk_id="weak-alt-b",
        paper_id="paper-alt-b",
        caption="Optical resonance mechanism.",
        search_text="mechanism",
        vtype="mechanism_anchor",
        utility="low",
        confidence="low",
    )
    section = _section(
        "S01",
        text=section_text,
        role="Explain the optical resonance mechanism.",
        title="Optical resonance mechanism",
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[weak_a, strong, weak_b],
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1,
        ),
    )
    assert [p["visual_chunk_id"] for p in plan["placements"]] == [
        "strong-superior"
    ]


def test_fail_open_fallback_increments_used_papers_for_article_diversity(
    work_dir: Path,
) -> None:
    """Weak/pending-only fallback placements must count toward paper reuse.

    Without the increment, every later section would see the fallback paper
    as unseen and re-award the coverage bonus, collapsing the article onto
    one source.  The fix keeps the fallback fail-open but spreads reuse.
    """

    section_text = (
        "The optical resonance mechanism confines the field in the "
        "resonant cavity and enables sensing."
    )
    section_role = "Explain the optical resonance mechanism."
    section_title = "Optical resonance mechanism"
    sections = []
    records = []
    for section_index in range(1, 5):
        section_id = f"S{section_index:02d}"
        sections.append(
            _section(
                section_id,
                text=section_text,
                role=section_role,
                title=section_title,
            )
        )
        for paper in ("a", "b", "c", "d"):
            records.append(
                _record(
                    _image(
                        work_dir / f"{section_id}-{paper}.png",
                        "navy",
                    ),
                    chunk_id=f"{section_id}-{paper}-0",
                    paper_id=f"paper-{paper}",
                    caption="Caption unavailable; inspect the source figure.",
                    search_text="optical resonance mechanism field",
                    caption_missing=True,
                    status="pending_multimodal_review",
                    needs_human_review=True,
                    utility="low",
                )
            )
    plan = plan_article_visual_assets(
        sections=sections,
        visual_cache_records=records,
        config=ArticleVisualAssetPlannerConfig(
            max_placements_per_section=1,
        ),
    )
    counts: dict[str, int] = {}
    for placement in plan["placements"]:
        assert placement["status"] == "traceable_source_pending_review"
        assert "fail_open_weak_or_pending_only_relevant_candidate" in (
            placement["review_required_reason"]
        )
        counts[placement["paper_id"]] = (
            counts.get(placement["paper_id"], 0) + 1
        )
    assert sum(counts.values()) == 4
    # Every section falls back to a distinct paper: the unseen-paper bonus
    # and the reuse penalty are effective across fallback placements.
    assert counts == {
        "paper-a": 1,
        "paper-b": 1,
        "paper-c": 1,
        "paper-d": 1,
    }


def test_table_record_flows_through_placement_contract_and_editorial_plan(
    table_tmp: Path,
) -> None:
    image_path = _image(table_tmp / "table1.png", "gold")
    record = {
        **_record(
            image_path,
            chunk_id="vis-table-1",
            caption=(
                "Table 1. Radiative cooling performance comparison summary."
            ),
            search_text=(
                "radiative cooling performance comparison summary"
            ),
            vtype="quantitative_comparison",
            status="ok",
            confidence="high",
            utility="high",
            permission="allowed",
        ),
        "asset_kind": "table",
        "use_permission": "factual_support",
    }
    section = _section(
        "S01",
        text=(
            "Table 1 summarizes radiative cooling performance comparison."
        ),
        role="Compare measured cooling performance.",
        title="Cooling performance comparison",
        expected=("quantitative_comparison",),
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(),
    )
    assert plan["validation"]["status"] == "passed"
    assert len(plan["placements"]) == 1
    placement = plan["placements"][0]
    assert placement["asset_kind"] == "table"
    assert placement["figure_kind"] == "source_table"
    assert placement["is_table"] is True
    assert placement["publication_eligible"] is True
    assert placement["figure_contract"]["asset_kind"] == "table"
    assert placement["source_map"]["asset_kind"] == "table"
    assert placement["provenance"]["asset_kind"] == "table"
    assert plan["table_count"] == 1
    assert plan["asset_kind_counts"] == {"table": 1}
    editorial = plan["visual_editorial_plan"]
    assert editorial["placements"][0]["asset_kind"] == "table"
    assert editorial["placements"][0]["figure_kind"] == "source_table"
    assert editorial["asset_kind_counts"] == {"table": 1}


def test_pending_table_stays_pending_and_never_publication_eligible(
    table_tmp: Path,
) -> None:
    image_path = _image(table_tmp / "table-pending.png", "silver")
    record = {
        **_record(
            image_path,
            chunk_id="vis-table-pending",
            caption=(
                "Table 2. Radiative cooling performance comparison summary."
            ),
            search_text=(
                "radiative cooling performance comparison summary"
            ),
            vtype="quantitative_comparison",
            status="pending_multimodal_review",
            confidence="medium",
            utility="medium",
            permission="requires_review",
            needs_human_review=True,
        ),
        "asset_kind": "table",
        "use_permission": "discovery_only",
    }
    section = _section(
        "S01",
        text=(
            "Table 2 summarizes radiative cooling performance comparison."
        ),
        role="Compare measured cooling performance.",
        title="Cooling performance comparison",
        expected=("quantitative_comparison",),
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(),
    )
    placement = plan["placements"][0]
    assert placement["asset_kind"] == "table"
    assert placement["figure_kind"] == "source_table"
    assert placement["status"] == "traceable_source_pending_review"
    assert placement["image_review_required"] is True
    assert placement["publication_eligible"] is False
    assert placement["figure_contract"]["asset_kind"] == "table"
    editorial_placement = plan["visual_editorial_plan"]["placements"][0]
    assert editorial_placement["status"] == (
        "traceable_source_pending_review"
    )
    assert editorial_placement["publication_eligible"] is False


def test_legacy_table_caption_is_not_mislabeled_as_source_figure(
    table_tmp: Path,
) -> None:
    """Old records without asset_kind still resolve Table 1 to a table."""

    image_path = _image(table_tmp / "legacy-table.png", "tan")
    record = _record(
        image_path,
        chunk_id="vis-legacy-table",
        caption=(
            "Table 1. Radiative cooling performance comparison summary."
        ),
        search_text=(
            "radiative cooling performance comparison summary"
        ),
        vtype="quantitative_comparison",
        status="ok",
        confidence="high",
        utility="high",
        permission="allowed",
    )
    section = _section(
        "S01",
        text=(
            "Table 1 summarizes radiative cooling performance comparison."
        ),
        role="Compare measured cooling performance.",
        title="Cooling performance comparison",
        expected=("quantitative_comparison",),
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(),
    )
    placement = plan["placements"][0]
    assert placement["asset_kind"] == "table"
    assert placement["figure_kind"] == "source_table"
    assert placement["is_table"] is True


def test_local_table_label_overrides_cached_figure_asset_kind(
    table_tmp: Path,
) -> None:
    image_path = _image(table_tmp / "table-mislabel.png", "khaki")
    record = {
        **_record(
            image_path,
            chunk_id="vis-table-mislabel",
            caption=(
                "Table 1. Radiative cooling performance comparison summary."
            ),
            search_text=(
                "radiative cooling performance comparison summary"
            ),
            vtype="quantitative_comparison",
            status="ok",
            confidence="high",
            utility="high",
            permission="allowed",
        ),
        "asset_kind": "figure",
        "use_permission": "factual_support",
    }
    section = _section(
        "S01",
        text=(
            "Table 1 summarizes radiative cooling performance comparison."
        ),
        role="Compare measured cooling performance.",
        title="Cooling performance comparison",
        expected=("quantitative_comparison",),
    )
    plan = plan_article_visual_assets(
        sections=[section],
        visual_cache_records=[record],
        config=ArticleVisualAssetPlannerConfig(),
    )
    placement = plan["placements"][0]
    assert placement["asset_kind"] == "table"
    assert placement["figure_kind"] == "source_table"
