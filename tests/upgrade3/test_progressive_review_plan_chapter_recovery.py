"""Offline tests for explicit successful chapter recovery."""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    ProgressiveReviewPlanner,
    recover_compatible_chapter_details,
)


def _write_source(root: Path, chapter_id: str, *, updated_plan: dict, source_materials: list[dict], chapter: dict | None = None, original_plan: dict | None = None, shared_outline: dict | None = None):
    revision_dir = root / "stages" / "affected_chapter_revision"
    chapter_dir = root / "stages" / "chapters"
    revision_dir.mkdir(parents=True)
    chapter_dir.mkdir(parents=True)
    original = original_plan or {"reader_objective": "read", "thesis": "thesis", "units": []}
    packet_chapter = chapter or {"chapter_id": chapter_id, "scope": "scope", "title": "Title"}
    (revision_dir / f"{chapter_id}.json").write_text(json.dumps({
        "chapter_id": chapter_id,
        "status": "complete",
        "owner_status": "updated",
        "updated_plan": updated_plan,
        "cache_inputs": {
            "topic_id": "topic",
            "research_question": "question",
            "chapter": packet_chapter,
            "chapter_plan": original,
            "candidate_materials": [],
            "candidate_navigation": {},
            "source_materials": source_materials,
        },
    }), encoding="utf-8")
    (chapter_dir / f"{chapter_id}.json").write_text(json.dumps({"shared_outline": shared_outline or {"scope": "shared"}}), encoding="utf-8")


def test_recovery_seeds_plan_and_removes_pending_case_suggestions(tmp_path):
    source = tmp_path / "source"
    original = {"reader_objective": "read", "thesis": "thesis", "units": [{"unit_id": "CH03_U01", "paragraph_briefs": []}]}
    updated = {"reader_objective": "read", "thesis": "updated", "units": [{
        "unit_id": "CH03_U01", "paragraph_briefs": [{"point": "brief"}],
        "case_suggestions": [{"source_handle": "P9999"}],
    }]}
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"a": 1}, "review_planning_B": {"b": 1}}
    chapter = {"chapter_id": "CH03", "scope": "scope", "title": "Title"}
    _write_source(source, "CH03", updated_plan=updated, source_materials=[material], chapter=chapter, original_plan=original)
    current = [{
        "topic_id": "topic", "research_question": "question", "chapter": chapter,
        "chapter_plan": original, "candidate_materials": [], "candidate_navigation": {},
        "shared_outline": {"scope": "shared"}, "source_materials": [material],
    }]
    output = tmp_path / "output"
    (output / "stages" / "chapters").mkdir(parents=True)
    original_path = output / "stages" / "chapters" / "CH03.json"
    original_path.write_text(json.dumps(current[0]), encoding="utf-8")
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, shared_outline={"scope": "shared"}, output_root=output, chapter_ids=["CH03"])
    assert report["chapters"]["CH03"]["status"] == "recovered"
    assert len(recovered[0]["chapter_plan"]["units"][0]["paragraph_briefs"]) == 1
    assert "case_suggestions" not in recovered[0]["chapter_plan"]["units"][0]
    assert json.loads(original_path.read_text(encoding="utf-8"))["chapter_plan"] == original
    saved = json.loads((output / "stages" / "recovered_chapters" / "CH03.json").read_text(encoding="utf-8"))
    assert saved["chapter_plan"] == recovered[0]["chapter_plan"]


def test_recovery_checks_existing_material_despite_unproven_current_success(tmp_path):
    source = tmp_path / "source"
    plan = {"reader_objective": "read", "thesis": "thesis", "units": [{"unit_id": "CH03_U01", "paragraph_briefs": [{"point": "brief", "source_handles": ["P0001"]}]}]}
    chapter = {"chapter_id": "CH03", "scope": "scope", "title": "Title"}
    source_material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"a": 1}, "review_planning_B": {"b": 1}}
    _write_source(source, "CH03", updated_plan=plan, source_materials=[source_material], chapter=chapter, original_plan=plan)
    changed = {**source_material, "study_summary_A": {"a": 999}}
    current = [{"topic_id": "topic", "research_question": "question", "chapter": chapter,
                "chapter_plan": plan, "candidate_materials": [], "candidate_navigation": {},
                "shared_outline": {"scope": "shared"}, "source_materials": [changed]}]
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, shared_outline={"scope": "shared"}, chapter_ids=["CH03"])
    assert report["chapters"]["CH03"]["reason"] == "required_material_identity_or_content_mismatch"
    assert recovered[0]["chapter_plan"] == plan

    output = tmp_path / "output"
    saved = output / "stages" / "affected_chapter_revision" / "CH03.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"status": "complete", "owner_status": "updated"}), encoding="utf-8")
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, shared_outline={"scope": "shared"}, output_root=output, chapter_ids=["CH03"])
    assert report["chapters"]["CH03"]["reason"] == "required_material_identity_or_content_mismatch"
    assert recovered[0]["chapter_plan"] == plan


def test_recovery_rejects_missing_shared_outline_and_unresolved_paper(tmp_path):
    source = tmp_path / "source"
    plan = {"reader_objective": "read", "thesis": "thesis", "units": [{"unit_id": "CH03_U01", "paragraph_briefs": [{"source_ids": ["unknown-paper"]}]}]}
    chapter = {"chapter_id": "CH03", "scope": "scope", "title": "Title"}
    _write_source(source, "CH03", updated_plan=plan, source_materials=[], chapter=chapter, original_plan=plan)
    current = [{"topic_id": "topic", "research_question": "question", "chapter": chapter,
                "chapter_plan": plan, "candidate_materials": [], "candidate_navigation": {},
                "source_materials": []}]
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, shared_outline={"scope": "shared"}, chapter_ids=["CH03"])
    assert report["chapters"]["CH03"]["reason"] == "compatibility_mismatch"
    assert "shared_outline_unavailable" in report["chapters"]["CH03"]["fields"]

    current[0]["shared_outline"] = {"scope": "shared"}
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, shared_outline={"scope": "shared"}, chapter_ids=["CH03"])
    assert report["chapters"]["CH03"]["reason"] == "required_material_unavailable"
    assert report["chapters"]["CH03"]["paper_ids"] == ["unknown-paper"]


def test_run_consumes_recovered_baseline_before_global_pass_and_keeps_it_on_owner_failure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    original = {"reader_objective": "read", "thesis": "old", "units": [{
        "unit_id": "CH03_U01", "paragraph_briefs": [{"point": "old"}],
    }]}
    updated = {"reader_objective": "read", "thesis": "recovered", "units": [{
        "unit_id": "CH03_U01", "paragraph_briefs": [{"point": "recovered"}],
    }]}
    chapter = {"chapter_id": "CH03", "scope": "scope", "title": "Title", "source_ids": []}
    shared = {"scope": "shared", "chapters": [chapter]}
    material = {"source_handle": "P0001", "paper_id": "paper-1", "study_summary_A": {"a": 1}, "review_planning_B": {"b": 1}}
    _write_source(source, "CH03", updated_plan=updated, source_materials=[material], chapter=chapter, original_plan=original, shared_outline=shared)
    pool = tmp_path / "pool.jsonl"
    pool.write_text(json.dumps({"planning_view": {"paper_identity": {"canonical_paper_id": "paper-1"}}}) + "\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"question_en": "question", "facets": ["facet"]}), encoding="utf-8")
    detail = {"topic_id": "topic", "research_question": "question", "chapter": chapter,
              "chapter_plan": original, "candidate_materials": [], "candidate_navigation": {},
              "shared_outline": shared, "source_materials": [material]}
    events = []
    responses = {
        "provisional_scope": {"supplement_requests": [], "directed_reads": []},
        "level1_outline": {"shared_outline": shared},
        "chapter_proposals": {"chapter_proposals": [chapter]},
        "harmonized_scope": {"shared_outline": shared, "chapters": [chapter]},
        "finalize_chapter_scope": {"shared_outline": shared, "chapters": [chapter]},
        "whole_plan_improvement": {"chapter_updates": [{"chapter_id": "CH03", "feedback": "retry"}]},
        "case_groups": {},
    }

    def fake_stage(self, name, fn, **kwargs):
        events.append(name)
        return {"response": responses.get(name, {})}

    captured = {}
    monkeypatch.setattr(ProgressiveReviewPlanner, "_stage", fake_stage)
    monkeypatch.setattr(ProgressiveReviewPlanner, "_tool_cycle", lambda self, **kwargs: {"phase": kwargs.get("phase"), "supplement_results": [], "directed_results": []})
    monkeypatch.setattr(ProgressiveReviewPlanner, "_route_sources", lambda self, *args, **kwargs: {"source_routes": []})
    monkeypatch.setattr(ProgressiveReviewPlanner, "_chapter_details", lambda self, **kwargs: [json.loads(json.dumps(detail))])
    monkeypatch.setattr(ProgressiveReviewPlanner, "_write_final_outputs", lambda self, value: None)

    def fake_assemble(self, **kwargs):
        captured["records"] = kwargs["chapter_records"]
        return {"status": "ok", "chapters": kwargs["chapter_records"]}

    monkeypatch.setattr(ProgressiveReviewPlanner, "_assemble_final", fake_assemble)
    cfg = ProgressivePlannerConfig(topic_id="topic", pool_path=pool, plan_path=plan,
                                   output_dir=tmp_path / "run", recovery_from=source,
                                   planning_revision_enabled=True)

    owner_calls = []

    def failing_owner(stage, payload):
        owner_calls.append(stage)
        if stage == "affected_chapter_revision":
            raise RuntimeError("offline owner failure")
        return {"response": {}}

    planner = ProgressiveReviewPlanner(cfg, planner=failing_owner)
    result = planner.run()
    assert result["status"] == "ok"
    assert events.index("whole_plan_improvement") > events.index("finalize_chapter_scope")
    assert captured["records"][0]["chapter_plan"] == updated
    assert "affected_chapter_revision" in owner_calls

    # A later failed owner stage cannot erase the in-memory recovered baseline.
    assert captured["records"][0]["chapter_recovery"]["source_root"] == str(source.resolve())


def test_recovery_rejects_scope_mismatch(tmp_path):
    source = tmp_path / "source"
    plan = {"reader_objective": "read", "thesis": "thesis", "units": []}
    chapter = {"chapter_id": "CH04", "scope": "source scope", "title": "Title"}
    _write_source(source, "CH04", updated_plan=plan, source_materials=[], chapter=chapter, original_plan=plan)
    current = [{
        "topic_id": "topic", "research_question": "question",
        "chapter": {"chapter_id": "CH04", "scope": "different scope", "title": "Title"},
        "chapter_plan": plan, "candidate_materials": [], "candidate_navigation": {},
        "shared_outline": {"scope": "shared"}, "source_materials": [],
    }]
    recovered, report = recover_compatible_chapter_details(current, recovery_root=source, chapter_ids=["CH04"])
    assert report["chapters"]["CH04"]["reason"] == "compatibility_mismatch"
    assert recovered[0]["chapter"]["scope"] == "different scope"
