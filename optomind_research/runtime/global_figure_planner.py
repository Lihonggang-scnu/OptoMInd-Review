"""Plan a small set of article-level figures from validated review synthesis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .article_completion_schemas import (
    ArticleRhetoricalContract,
    ArticleSynthesisMap,
    GlobalFigureItem,
    GlobalFigurePlan,
)
from .artifact_store import atomic_write_json


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def build_global_figure_plan(
    *,
    blueprint_path: Path,
    synthesis_map_path: Path,
    output_path: Path,
) -> GlobalFigurePlan:
    """Select useful global figures; never force all common templates."""

    blueprint = _read_json(blueprint_path, {})
    contract = ArticleRhetoricalContract.model_validate(
        blueprint.get("article_rhetorical_contract", {})
    )
    synthesis = ArticleSynthesisMap.model_validate(
        _read_json(synthesis_map_path, {})
    )
    candidates = set(
        contract.global_figure_contract.candidate_templates
    )
    figures: List[GlobalFigureItem] = []
    unfilled: List[Dict[str, Any]] = []
    section_count = len(synthesis.section_contributions)

    if "field_map" in candidates:
        eligible = section_count >= 4 and bool(
            contract.body_contract.primary_taxonomy.strip()
        )
        figures.append(
            GlobalFigureItem(
                figure_id="GF01",
                template_kind="field_map",
                argumentative_purpose=(
                    "Orient readers by showing the primary taxonomy and the "
                    "cross-cutting dimensions used throughout the review."
                ),
                placement="after_introduction",
                eligibility_status=(
                    "eligible" if eligible else "ineligible"
                ),
                eligibility_reasons=[
                    (
                        "The article has a stable primary taxonomy and enough "
                        "body sections to justify a field map."
                        if eligible
                        else "The body does not yet support a stable field map."
                    )
                ],
                source_route="generated" if eligible else "unfilled",
                data_provenance_level="schematic",
                generation_brief=(
                    "Create a clean scientific field map organized by the "
                    "primary taxonomy: "
                    f"{contract.body_contract.primary_taxonomy.rstrip('.')}. "
                    "Show cross-cutting dimensions without quantitative claims."
                )
                if eligible
                else "",
            )
        )

    if "timeline" in candidates:
        chronology_count = len(
            set(synthesis.reference_inventory.landmark_paper_ids)
            | set(synthesis.reference_inventory.frontier_paper_ids)
        )
        eligible = chronology_count >= 5
        figures.append(
            GlobalFigureItem(
                figure_id="GF02",
                template_kind="timeline",
                argumentative_purpose=(
                    "Show genuine conceptual or technical turning points, not "
                    "a decorative list of publication years."
                ),
                placement="after_introduction",
                eligibility_status=(
                    "needs_data" if eligible else "ineligible"
                ),
                eligibility_reasons=[
                    (
                        "Candidate landmark and frontier papers exist, but "
                        "publication years and turning-point labels must be "
                        "verified before deterministic plotting."
                        if eligible
                        else "Too few verified turning points justify a timeline."
                    )
                ],
                source_route="unfilled",
                data_provenance_level="exact",
            )
        )

    if "benchmark_landscape" in candidates:
        figures.append(
            GlobalFigureItem(
                figure_id="GF03",
                template_kind="benchmark_landscape",
                argumentative_purpose=(
                    "Compare approaches only when compatible quantitative "
                    "metrics and operating conditions can be traced."
                ),
                placement="within_body",
                eligibility_status="needs_data",
                eligibility_reasons=[
                    "The synthesis map alone does not contain an audited comparable dataset."
                ],
                source_route="unfilled",
                data_provenance_level="exact",
            )
        )

    if "challenge_roadmap" in candidates:
        eligible = bool(synthesis.challenge_candidates) and bool(
            synthesis.outlook_candidates
        )
        figures.append(
            GlobalFigureItem(
                figure_id="GF04",
                template_kind="challenge_roadmap",
                argumentative_purpose=(
                    "Connect evidence-backed challenges to root causes, current "
                    "responses, next steps, and success indicators."
                ),
                placement="before_challenges_and_outlook",
                eligibility_status=(
                    "eligible" if eligible else "ineligible"
                ),
                eligibility_reasons=[
                    (
                        "Validated challenge and opportunity chains are available."
                        if eligible
                        else "No validated challenge-to-opportunity chain exists."
                    )
                ],
                source_route="generated" if eligible else "unfilled",
                data_provenance_level="schematic",
                generation_brief=(
                    "Create a scientific challenge roadmap. Use only the "
                    "validated challenge and opportunity statements supplied "
                    "with the request. Distinguish established, conditional, "
                    "and open elements and do not invent measurements."
                )
                if eligible
                else "",
            )
        )

    for item in figures:
        if item.eligibility_status != "eligible":
            unfilled.append(
                {
                    "figure_id": item.figure_id,
                    "template_kind": item.template_kind,
                    "reason": item.eligibility_reasons,
                    "status": item.eligibility_status,
                }
            )
    plan = GlobalFigurePlan(
        article_level_figures=figures,
        intentionally_unfilled=unfilled,
    )
    atomic_write_json(output_path, plan.model_dump(mode="json"))
    return plan


def merge_global_figures_into_visual_plan(
    *,
    visual_plan_path: Path,
    global_figure_plan_path: Path,
    blueprint_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Route eligible global schematics into the existing visual factory."""

    visual_plan = _read_json(visual_plan_path, {})
    global_plan = GlobalFigurePlan.model_validate(
        _read_json(global_figure_plan_path, {})
    )
    blueprint = _read_json(blueprint_path, {})
    sections = [
        item
        for item in blueprint.get("sections", [])
        if isinstance(item, dict) and item.get("section_id")
    ]
    if not sections:
        raise ValueError("blueprint has no body sections")
    first_id = str(sections[0]["section_id"])
    last_id = str(sections[-1]["section_id"])
    requests = list(visual_plan.get("conceptual_figure_requests", []))
    existing_ids = {
        str(item.get("global_figure_id") or "")
        for item in requests
        if isinstance(item, dict)
    }
    kind_map = {
        "field_map": "concept_map",
        "challenge_roadmap": "workflow_schematic",
        "mechanism_synthesis": "mechanism_schematic",
        "taxonomy_map": "taxonomy_diagram",
    }
    for item in global_plan.article_level_figures:
        if (
            item.eligibility_status != "eligible"
            or item.source_route != "generated"
            or item.figure_id in existing_ids
        ):
            continue
        section_id = (
            last_id
            if item.placement == "before_challenges_and_outlook"
            else first_id
        )
        requests.append(
            {
                "section_id": section_id,
                "global_figure_id": item.figure_id,
                "figure_kind": kind_map.get(
                    item.template_kind,
                    "concept_map",
                ),
                "argumentative_purpose": item.argumentative_purpose,
                "generation_brief": item.generation_brief,
                "placement_guidance": item.placement,
                "data_provenance_level": item.data_provenance_level,
                "input_data": {},
                "approximate_data_allowed": (
                    item.data_provenance_level != "exact"
                ),
                "priority": "high",
                "required_disclosure": (
                    "AI-generated explanatory visual"
                ),
                "status": "pending_generation_and_review",
            }
        )
    visual_plan["conceptual_figure_requests"] = requests
    visual_plan["global_figure_plan_path"] = str(global_figure_plan_path)
    target = output_path or visual_plan_path
    atomic_write_json(target, visual_plan)
    return visual_plan
