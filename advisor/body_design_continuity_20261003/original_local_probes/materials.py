import inspect
import json
import sys
from pathlib import Path

WT = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
OUT = Path(r"F:\OptoMind-Review-2\outputs\body_design_continuity_verification_20261003\materials")
sys.path.insert(0, str(WT))

from optomind_research.runtime.upgrade3.planning_material_triage import (
    LocalGap, LocalReadingBundle, LocalReadingPassage, _triage_payload,
)
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressiveReviewPlanner,
)
from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view, build_source_catalog, chapter_tool_materials_from_packet,
)


def triage_old_handle():
    gap = LocalGap(gap_id="G1", question="question")
    passage = LocalReadingPassage(
        source_handle="P0007", paper_id="paper-current-7", title="Current paper",
        year="2024", doi="10.current", reading_role="direct_evidence",
        text="substantive local passage", best_sentence="sentence",
        section_path=("Results",), material_depth="body", reading_path="local",
        card_path="card.json", from_existing_field=False,
    )
    bundle = LocalReadingBundle(
        gap=gap, passages=[passage], searched_papers=1, search_found=True,
    )
    return {
        "triage_payload_material_found": _triage_payload(gap, bundle)["material_found"],
        "prepare_signature": str(inspect.signature(
            __import__(
                "optomind_research.runtime.upgrade3.planning_material_triage",
                fromlist=["prepare_local_reading"],
            ).prepare_local_reading
        )),
    }


def bind_fallback():
    planner = object.__new__(ProgressiveReviewPlanner)
    planner.tool_materials_by_chapter = {
        "CH01": [{"sources": [{"source_handle": "P0007", "title": "stale-only"}]}]
    }
    planner._bind_tool_material_source_handles([{
        "_paper_id": "paper-current-7", "_source_handle": "P0001",
    }])
    return planner.tool_materials_by_chapter


def tool_catalog_gap():
    packet = {
        "chapter": {"chapter_id": "CH01", "title": "Chapter"},
        "chapter_plan": {"units": [{
            "substantive_point": "point",
            "paragraph_briefs": [{"point": "brief", "source_handles": ["P0001"]}],
        }]},
        "source_materials": [{
            "source_handle": "P0001", "paper_id": "paper-1", "title": "Used source",
            "study_summary_A": {"finding": "A"},
        }],
        "tool_materials": [{
            "need_id": "N-tool", "usable_content": "tool-only scientific synthesis",
            "sources": [{
                "source_handle": "P0999", "paper_id": "paper-999",
                "title": "Tool-only source", "doi": "10.9999",
            }],
        }],
    }
    packet_path = OUT / "tool_only_packet.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    per_source, chapter_level = chapter_tool_materials_from_packet(packet)
    view = build_chapter_view(packet_path)
    catalog = build_source_catalog(view)
    return {
        "packet_tool_split": {"per_source": per_source, "chapter_level": chapter_level},
        "view_sources": [source.source_handle for source in view.sources],
        "view_chapter_tool_materials": view.chapter_tool_materials,
        "source_catalog_keys": sorted(catalog),
    }


def late_route_gap():
    routes = [{"source_handle": "P0001", "chapter_ids": ["CH01"]}]
    proposals = {"chapter_proposals": [{"chapter_id": "CH01", "source_handles": []}]}
    return ProgressiveReviewPlanner._attach_routed_sources(
        proposals, routes, [{"chapter_id": "CH01"}]
    )


def artifact_counts():
    root = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\case_chain_restore_20261002")
    load = lambda rel: json.loads((root / rel).read_text(encoding="utf-8-sig"))
    routing = load("stages/source_routing_summary.json")
    plan = load("DETAILED_REVIEW_PLAN.json")
    cases = load("stages/case_groups.json")
    routed = {row.get("source_handle") for row in routing.get("source_routes", []) if isinstance(row, dict)}
    identities = set(plan.get("source_identity_map", {}))
    late = sorted(identities - routed)
    case_handles = {
        study.get("source_handle")
        for addition in cases.get("additions", []) if isinstance(addition, dict)
        for study in addition.get("studies", []) if isinstance(study, dict)
    }
    return {
        "pool_sources": routing.get("pool_sources"),
        "routed_sources": routing.get("routed_sources"),
        "route_count": len(routed),
        "identity_count": len(identities),
        "late_handles": late,
        "late_in_case_additions": sorted(set(late) & case_handles),
    }


if __name__ == "__main__":
    result = {
        "triage_old_handle": triage_old_handle(),
        "bind_fallback": bind_fallback(),
        "tool_catalog_gap": tool_catalog_gap(),
        "late_route_gap": late_route_gap(),
        "artifacts": artifact_counts(),
    }
    (OUT / "verify_findings_3_4_5.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
