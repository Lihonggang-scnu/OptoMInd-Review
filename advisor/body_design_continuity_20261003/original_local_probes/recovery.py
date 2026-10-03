"""Offline verification of the R1, R4 and N1 recovery seams.

Only the planner callable is fake.  Recovery JSON, owner cache, stage cache,
SQLite/store code (none needed by these checks), and final assembly remain
production implementations from the locked checkout.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

CHECKOUT = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
sys.path.insert(0, str(CHECKOUT))

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    ProgressiveReviewPlanner,
    build_candidate_navigation,
    load_planning_pool,
    recover_compatible_chapter_details,
)


ROOT = Path(__file__).resolve().parent
RUN_ROOT = ROOT / "r1_production_run"
R4_ROOT = ROOT / "r4_material_version"
N1_ROOT = ROOT / "n1_list_outline"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def base_plan(thesis: str) -> dict:
    return {
        "reader_objective": "read",
        "thesis": thesis,
        "units": [{
            "unit_id": "CH03_U01",
            "point": "one unit",
            "paragraph_briefs": [{"point": thesis, "source_handles": ["P0001"]}],
        }],
    }


def chapter() -> dict:
    return {
        "chapter_id": "CH03", "title": "Title", "scope": "scope", "source_ids": ["paper-1"],
        "source_handles": ["P0001"], "excluded_source_handles": [],
    }


def material(*, supplement: str = "") -> dict:
    row = {
        "source_handle": "P0001",
        "paper_id": "paper-1",
        "title": "Paper",
        "study_summary_A": {"finding": "same A"},
        "review_planning_B": {"use": "same B"},
    }
    if supplement:
        row["supplement_gap_material"] = {"gap_id": "G1", "text": supplement}
    return row


def make_source(root: Path, *, shared_outline: object, source_materials: list[dict], updated_plan: dict, original_plan: dict, candidate_navigation: dict | None = None) -> None:
    write_json(root / "stages" / "affected_chapter_revision" / "CH03.json", {
        "chapter_id": "CH03",
        "status": "complete",
        "owner_status": "updated",
        "updated_plan": updated_plan,
        "cache_inputs": {
            "topic_id": "topic",
            "research_question": "question",
            "chapter": chapter(),
            "chapter_plan": original_plan,
            "candidate_materials": [],
            "candidate_navigation": candidate_navigation or {},
            "source_materials": source_materials,
        },
    })
    write_json(root / "stages" / "chapters" / "CH03.json", {"shared_outline": shared_outline})


def make_pool(path: Path, card_path: Path, *, supplement: str = "") -> None:
    write_json(card_path, {
        "paper_identity": {"canonical_paper_id": "paper-1", "title": "Paper"},
        "general_understanding": {"finding": "same A"},
        "review_planning": {"use": "same B"},
    })
    row = {
        "paper_id": "paper-1",
        "title": "Paper",
        "card_path": str(card_path),
        "planning_view": {"paper_identity": {"canonical_paper_id": "paper-1", "title": "Paper"}},
    }
    if supplement:
        row["supplement_gap_material"] = {"gap_id": "G1", "text": supplement}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def run_r1() -> dict:
    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    source = RUN_ROOT / "source"
    run = RUN_ROOT / "run"
    pool = RUN_ROOT / "pool.jsonl"
    plan_file = RUN_ROOT / "plan.json"
    original = base_plan("original")
    recovered = base_plan("recovered")
    owner_plan = base_plan("owner-success")
    shared = {"scope": "shared", "chapters": [chapter()]}
    source_material = material()
    make_pool(pool, RUN_ROOT / "card.json")
    loaded_pool = load_planning_pool(pool)
    candidate_navigation = build_candidate_navigation(
        chapter=chapter(), candidates=loaded_pool,
        source_routes=[{
            "source_handle": "P0001", "chapter_ids": ["CH03"], "specific_usable_material": "use",
            "route_status": "assigned", "reason": "", "interpretation_limits": [],
        }],
        research_question="question",
    )
    make_source(
        source, shared_outline=shared, source_materials=[source_material], updated_plan=recovered,
        original_plan=original, candidate_navigation=candidate_navigation,
    )
    write_json(plan_file, {"question_en": "question", "facets": ["facet"]})

    owner_calls: list[str] = []
    owner_should_fail = {"value": False}

    def fake_planner(stage: str, payload: dict):
        if stage == "affected_chapter_revision":
            owner_calls.append(stage)
            if owner_should_fail["value"]:
                raise RuntimeError("simulated owner failure")
            return {"updated_plan": owner_plan}
        if stage == "provisional_scope":
            return {"review_title": "Review", "material_theme_inventory": [], "supplement_requests": [], "directed_reads": []}
        if stage == "level1_outline":
            return {"shared_outline": shared}
        if stage == "source_routing":
            return {"source_routes": [{"source_handle": "P0001", "chapter_ids": ["CH03"], "specific_usable_material": "use"}]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [chapter()]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": shared, "chapters": [chapter()]}
        if stage == "chapter_details":
            return {"chapter_plan": original}
        if stage == "whole_plan_improvement":
            return {"chapter_updates": [{"chapter_id": "CH03", "feedback": "retry owner"}]}
        if stage == "case_groups":
            return {"additions": [], "status": "complete"}
        return {}

    cfg = ProgressivePlannerConfig(
        topic_id="topic", pool_path=pool, plan_path=plan_file, output_dir=run,
        planning_revision_enabled=True, recovery_from=source, recovery_chapters=("CH03",),
        chapter_workers=1,
    )
    first = ProgressiveReviewPlanner(cfg, planner=fake_planner).run()
    first_plan = first["chapters"][0]["chapter_plan"]["thesis"]
    first_recovery = json.loads((run / "stages" / "chapter_recovery.json").read_text(encoding="utf-8"))
    first_recovered_path = run / "stages" / "recovered_chapters" / "CH03.json"
    first_recovered_plan = None
    if first_recovered_path.is_file():
        first_recovered_plan = json.loads(first_recovered_path.read_text(encoding="utf-8")).get("chapter_plan", {}).get("thesis")
    owner_should_fail["value"] = True
    second = ProgressiveReviewPlanner(cfg, planner=fake_planner).run(resume=True)
    second_plan = second["chapters"][0]["chapter_plan"]["thesis"]
    owner_cache = json.loads((run / "stages" / "affected_chapter_revision" / "CH03.json").read_text(encoding="utf-8"))
    recovery_report = json.loads((run / "stages" / "chapter_recovery.json").read_text(encoding="utf-8"))
    return {
        "first_plan_thesis": first_plan,
        "first_recovery_reason": first_recovery.get("chapters", {}).get("CH03", {}).get("reason"),
        "first_recovery_status": first_recovery.get("chapters", {}).get("CH03", {}).get("status"),
        "first_recovery_fields": first_recovery.get("chapters", {}).get("CH03", {}).get("fields"),
        "first_recovered_plan_thesis": first_recovered_plan,
        "second_plan_thesis": second_plan,
        "owner_calls": len(owner_calls),
        "owner_cache_status_after_second": owner_cache.get("status"),
        "owner_cache_owner_status_after_second": owner_cache.get("owner_status"),
        "second_recovery_reason": recovery_report.get("chapters", {}).get("CH03", {}).get("reason"),
        "failure_observed": first_plan == "owner-success" and second_plan == "original" and len(owner_calls) == 2 and owner_cache.get("status") == "failed",
    }


def run_r4() -> dict:
    if R4_ROOT.exists():
        shutil.rmtree(R4_ROOT)
    source = R4_ROOT / "source"
    current_pool = R4_ROOT / "pool.jsonl"
    card = R4_ROOT / "card.json"
    old = material(supplement="80 C")
    current = material(supplement="25 C")
    plan = base_plan("recovery")
    shared = {"scope": "shared"}
    make_source(source, shared_outline=shared, source_materials=[old], updated_plan=plan, original_plan=plan)
    make_pool(current_pool, card, supplement="25 C")
    pool_row = json.loads(current_pool.read_text(encoding="utf-8").strip())
    pool_row["_source_handle"] = "P0001"
    pool_row["_paper_id"] = "paper-1"
    pool_row["_b_summary"] = {"declared_content_depth": "card"}
    current_packet = {
        "topic_id": "topic", "research_question": "question", "chapter": chapter(),
        "chapter_plan": plan, "candidate_materials": [], "candidate_navigation": {},
        "shared_outline": shared, "source_materials": [],
    }
    recovered, report = recover_compatible_chapter_details(
        [current_packet], recovery_root=source, current_pool=[pool_row],
        shared_outline=shared, chapter_ids=["CH03"], output_root=R4_ROOT / "output",
    )
    recovered_rows = recovered[0].get("source_materials") or []
    recovered_row = recovered_rows[0] if recovered_rows else {}
    return {
        "report": report["chapters"]["CH03"],
        "recovered_supplement": recovered_row.get("supplement_gap_material", {}).get("text"),
        "current_pool_supplement": "25 C",
        "old_material_reused": recovered_row.get("supplement_gap_material", {}).get("text") == "80 C",
    }


def run_n1() -> dict:
    if N1_ROOT.exists():
        shutil.rmtree(N1_ROOT)
    source = N1_ROOT / "source"
    plan = base_plan("same")
    outline = [{"chapter_id": "CH03", "title": "Title", "scope": "scope"}]
    make_source(source, shared_outline=outline, source_materials=[], updated_plan=plan, original_plan=plan)
    packet = {
        "topic_id": "topic", "research_question": "question", "chapter": chapter(),
        "chapter_plan": plan, "candidate_materials": [], "candidate_navigation": {},
        "shared_outline": outline, "source_materials": [],
    }
    recovered, report = recover_compatible_chapter_details(
        [packet], recovery_root=source, shared_outline=outline, chapter_ids=["CH03"], output_root=N1_ROOT / "output",
    )
    return {
        "reason": report["chapters"]["CH03"].get("reason"),
        "fields": report["chapters"]["CH03"].get("fields"),
        "recovered_count": report.get("recovered_chapters"),
        "list_outline_rejected": report["chapters"]["CH03"].get("reason") == "compatibility_mismatch" and "shared_outline_unavailable" in (report["chapters"]["CH03"].get("fields") or []),
    }


def main() -> None:
    result = {"R1": run_r1(), "R4": run_r4(), "N1": run_n1()}
    write_json(ROOT / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
