"""WO-05 bounded coordinator/control-flow fixtures; model and tool-cycle substitutions."""
import copy
import json

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p
from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view, build_source_catalog, write_view
from optomind_research.runtime.upgrade3.review_unit_writer import build_unit_view, unit_messages, unit_payload


def read(path):
    return json.loads(path.read_text())


class Boundary:
    def __init__(self, initial="INITIAL_ARGUMENT", calibrated="CALIBRATED_ARGUMENT", known=True):
        self.initial, self.calibrated, self.known = initial, calibrated, known
        self.calls = []
        self.chapters = [{"chapter_id": cid, "title": cid, "purpose": "Compare actual conditions", "scope": "conditions"}
                         for cid in ("CH01", "CH02")]

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "provisional_scope":
            return {"provisional_scope": {"statement": "SCOPE_ONLY"}, "provisional_outline": self.chapters,
                    "material_theme_inventory": ["initial material theme"]}
        if stage == "level1_outline":
            return {"shared_scope": {"statement": "SCOPE_ONLY"}, "review_argument": self.initial,
                    "shared_outline": self.chapters}
        if stage == "source_routing":
            return {"source_routes": [{"source_handle": row["source_handle"],
                    "chapter_ids": ["CH01" if row["source_handle"] != "P0001" else "CH02"],
                    "specific_usable_material": "A concrete comparison under stated conditions"}
                    for row in payload["candidate_batch"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [payload["chapter"]]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": self.chapters, "chapters": self.chapters}
        if stage == "chapter_details":
            cid = payload["chapter"]["chapter_id"]
            return {"chapter_plan": {"thesis": "Conditions govern the comparison", "units": [{
                "unit_id": cid + "_U01", "substantive_point": cid + " comparison",
                "paragraph_briefs": [{"paragraph_id": cid + "_U01_P01", "point": "Compare conditions", "development": "Explain the evidence", "source_handles": ["P0001"] if cid == "CH02" else []}]}]}}
        if stage == "whole_plan_improvement":
            return {"finalized_review_argument": self.calibrated, "finalized_shared_scope": {"statement": "FINAL_SCOPE_ONLY"}}
        if stage == "affected_chapter_revision":
            return {"status": "no_change"}
        if stage == "case_groups":
            return {}
        raise AssertionError(stage)


def make_planner(tmp_path, monkeypatch, *, revision=True, late_phase=None, known=True, argument="CALIBRATED_ARGUMENT"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = tmp_path / "PLAN.json"
    plan.write_text(json.dumps({"question": "How do conditions alter results?", "scope": {"exclude": "unrelated applications"},
        "research_object": "Observed systems", "facets": [{"id": "F1", "ask": "Which conditions alter the comparison?",
        "must_exclude": ["unrelated evidence"], "keyword_queries": ["SEARCH_EXECUTION_ONLY"]}]}))
    pool = tmp_path / "POOL.jsonl"
    pool.write_text(json.dumps({"paper_id": "early", "planning_view": {"paper_identity": {
        "canonical_paper_id": "early", "title": "Early study"}, "planning_summary": "Initial comparison under condition A"}}) + "\n")
    boundary = Boundary(calibrated=argument, known=known)
    planner = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id="body05", plan_path=plan, pool_path=pool,
        output_dir=tmp_path / "run", chapter_workers=1, planning_revision_enabled=revision), planner=boundary)
    def tools(**kw):
        if kw["phase"] != late_phase:
            return {}
        return {"supplement_results": [{"chapter_ids": ["CH01"] if known else [], "candidate_rows": [{
            "paper_id": "late", "title": "Late controlled comparison", "supplement_gap_material": {
                "summary": "Observed outcome Q only under condition Z; the relation reverses elsewhere."}}]}]}
    monkeypatch.setattr(planner, "_tool_cycle", tools)
    return planner, boundary


def test_first_level_sees_compact_user_intent_and_refreshed_observed_themes(tmp_path, monkeypatch):
    planner, boundary = make_planner(tmp_path, monkeypatch, late_phase="level1")
    result = planner.run(stop_after="level1")
    actual = next(payload for stage, payload in boundary.calls if stage == "level1_outline")
    assert actual["original_plan"]["question"] == "How do conditions alter results?"
    assert actual["original_plan"]["scope"] == {"exclude": "unrelated applications"}
    assert actual["original_plan"]["facets"][0]["ask"].startswith("Which conditions")
    assert "SEARCH_EXECUTION_ONLY" not in json.dumps(actual)
    assert "candidate_pool" not in actual
    themes = actual["material_theme_inventory"]
    assert themes[0] == "initial material theme"
    assert themes[1]["source_handle"] == "P0002"
    assert themes[1]["chapter_ids"] == ["CH01"]
    assert "outcome Q" in themes[1]["material_excerpt"]
    assert themes == read(planner.config.output_dir / "stages/material_theme_inventory.json")["material_theme_inventory"]
    assert result["review_argument"] == "INITIAL_ARGUMENT"
    assert result["review_argument_status"] == "carried_forward_uncalibrated"


@pytest.mark.parametrize("revision", [False, True])
def test_calibrated_argument_scope_persist_separately_in_both_paths(tmp_path, monkeypatch, revision):
    planner, boundary = make_planner(tmp_path, monkeypatch, revision=revision)
    result = planner.run()
    coordinator = next(payload for stage, payload in boundary.calls if stage == "whole_plan_improvement")
    assert coordinator["review_argument"] == "INITIAL_ARGUMENT"
    assert coordinator["shared_scope"] == {"statement": "SCOPE_ONLY"}
    saved = read(planner.config.output_dir / "DETAILED_REVIEW_PLAN.json")
    packet = read(planner.config.output_dir / "writer_packets/CH01.json")
    for actual in (result, saved, packet):
        assert actual["review_argument"] == "CALIBRATED_ARGUMENT"
        assert actual["shared_scope"] == {"statement": "FINAL_SCOPE_ONLY"}
        assert actual["review_argument_status"] == "calibrated"
        assert actual["review_argument_source"] == "whole_plan_improvement"
    assert "Calibrate the supplied review_argument" in p._messages_for("whole_plan_improvement", coordinator)[0]["content"]
    boundary.calls.clear()
    planner.run(resume=True)
    assert boundary.calls == []


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("revision", [False, True])
def test_late_material_reaches_only_relevant_coordinator_owner_before_formal_cases(tmp_path, monkeypatch, known, revision):
    planner, boundary = make_planner(tmp_path, monkeypatch, late_phase="level2", known=known, revision=revision)
    planner.run()
    coordinator = next(payload for stage, payload in boundary.calls if stage == "whole_plan_improvement")
    assert set(coordinator["late_material_changes"]) == {"CH01"}
    change = coordinator["late_material_changes"]["CH01"][0]
    assert change["source_handle"] == "P0002"
    assert "outcome Q" in json.dumps(change["source_materials"])
    owners = [payload for stage, payload in boundary.calls if stage == "affected_chapter_revision"]
    assert [item["chapter_id"] for item in owners] == ["CH01"]
    assert owners[0]["review_argument"] == "CALIBRATED_ARGUMENT"
    assert owners[0]["late_material_changes"][0]["source_handle"] == "P0002"
    assert "outcome Q" in json.dumps(owners[0]["late_material_changes"])
    stages = [stage for stage, _ in boundary.calls]
    assert stages.index("whole_plan_improvement") < stages.index("affected_chapter_revision") < stages.index("case_groups")
    assert stages.count("whole_plan_improvement") == 1
    assert stages.count("source_routing") == (1 if known else 2)
    final = read(planner.config.output_dir / "DETAILED_REVIEW_PLAN.json")
    assert set(final["whole_plan_improvement"]["late_material_changes"]) == {"CH01"}
    state = read(planner.config.output_dir / "RUN_STATE.json")
    assert set(state["stage_inputs"]["whole_plan_improvement"]["late_material_changes"]) == {"CH01"}
    theme = final["material_theme_inventory"][1]
    assert theme["chapter_ids"] == ["CH01"] and "outcome Q" in theme["material_excerpt"]
    assert not (planner.config.output_dir / "stages/affected_chapter_revision/CH02.json").exists()
    boundary.calls.clear()
    planner.run(resume=True)
    assert boundary.calls == []


def test_same_scope_distinct_arguments_reach_actual_writer_payload(tmp_path, monkeypatch):
    arguments = []
    for index, argument in enumerate(("Conditions explain the divergence", "Measurement choices explain the divergence")):
        planner, _ = make_planner(tmp_path / str(index), monkeypatch, argument=argument)
        planner.run()
        root = planner.config.output_dir
        packet_path = root / "writer_packets/CH02.json"
        view = build_chapter_view(packet_path, id_map_path=root / "ID_MAP.json")
        view_path = write_view(view, root / "ARRANGEMENT_INPUT.json")
        arrangement = {"chapter_id": "CH02", "units": [{"unit_id": "CH02_U01", "focus": "Compare conditions",
            "paragraph_tasks": [{"paragraph_id": "CH02_U01_P01", "point": "Compare conditions", "source_briefs": ["CH02_U01_P01"]}]}]}
        arrangement["source_catalog"] = build_source_catalog(view)
        p._atomic_json(root / "ARRANGEMENT.json", arrangement)
        writing_view = build_unit_view(root / "ARRANGEMENT.json", "CH02_U01", view_path=view_path)
        payload = unit_payload(writing_view)
        messages = unit_messages(writing_view)
        p._atomic_json(root / "WRITER_MESSAGES.json", messages)
        actual_user = json.loads(messages[-1]["content"])
        assert actual_user["chapter_frame"]["review_argument"] == argument
        assert actual_user["chapter_frame"]["shared_scope"] == {"statement": "FINAL_SCOPE_ONLY"}
        arguments.append(payload["chapter_frame"]["review_argument"])
        p._atomic_json(root / "WRITER_PAYLOAD.json", payload)
        assert read(root / "WRITER_PAYLOAD.json")["chapter_frame"]["review_argument"] == argument
    assert arguments[0] != arguments[1]


def test_missing_argument_is_transparent_and_never_derived_from_scope():
    missing = p._review_guidance({"shared_scope": {"statement": "SCOPE_ONLY"}}, {})
    assert missing["review_argument"] == "" and missing["review_argument_status"] == "missing"
    retained = p._review_guidance({"review_argument": "INITIAL_ARGUMENT"}, {}, {"finalized_shared_scope": "NEW_SCOPE"})
    assert retained["review_argument"] == "INITIAL_ARGUMENT"
    assert retained["review_argument_status"] == "carried_forward_uncalibrated"
    blank = p._review_guidance({"review_argument": "REAL_ARGUMENT"}, {}, {"finalized_review_argument": "  ", "finalized_shared_scope": "  "})
    assert blank["review_argument"] == "REAL_ARGUMENT" and blank["review_argument_status"] == "carried_forward_uncalibrated"
    structured = p._review_guidance({}, {}, {"finalized_review_argument": {"claim": "Conditional relation", "limit": "Observed settings"}})
    assert json.loads(structured["review_argument"])["claim"] == "Conditional relation"


def test_owner_identity_seed_uses_explicit_paragraph_relationship_before_position():
    old = {"units": [{"paragraph_briefs": [{"paragraph_id": "paragraph-A"}]},
                     {"paragraph_briefs": [{"paragraph_id": "paragraph-B"}]}]}
    arranged = {"units": [{"unit_id": "unit-B", "paragraph_tasks": [{"paragraph_id": "derived-B", "source_briefs": ["paragraph-B"]}]},
                           {"unit_id": "unit-A", "paragraph_tasks": [{"paragraph_id": "derived-A", "source_briefs": ["paragraph-A"]}]}]}
    plan, ids = p._seed_owner_unit_ids(old, arranged)
    assert ids == ["unit-A", "unit-B"]
    assert [unit["unit_id"] for unit in plan["units"]] == ids
    # A merged or split derived identity must not overwrite old units by position.
    for units in ([{"unit_id": "merged", "paragraph_tasks": [{"source_briefs": ["paragraph-A", "paragraph-B"]}]}],
                  [{"unit_id": "split-1", "paragraph_tasks": [{"source_briefs": ["paragraph-A"]}]},
                   {"unit_id": "split-2", "paragraph_tasks": [{"source_briefs": ["paragraph-A"]}]}]):
        seeded, ids = p._seed_owner_unit_ids(old, {"units": units})
        assert ids == []
        assert all("unit_id" not in unit for unit in seeded["units"])
    assert p._seed_owner_unit_ids({"units": [{"point": "legacy"}]}, {"units": [{"unit_id": "legacy-unit"}]})[1] == ["legacy-unit"]


def test_owner_reorder_needs_no_new_identity_but_partial_split_map_is_rejected():
    old = {"units": [{"unit_id": "A"}, {"unit_id": "B"}]}
    assert p._validate_owner_plan_update(old, {"units": list(reversed(old["units"]))}, [])[1] == []
    new = {"units": [{"unit_id": "A1"}, {"unit_id": "A2"}, {"unit_id": "B"}]}
    assert "unit_id_remap_missing_new_unit" in p._validate_owner_plan_update(old, new, [], owner_response={"unit_id_remap": {"A1": ["A"]}})[1]
    assert p._validate_owner_plan_update(old, new, [], owner_response={"unit_id_remap": {"A1": ["A"], "A2": ["A"]}})[1] == []
    assert "updated_plan_duplicate_unit_ids" in p._validate_owner_plan_update(old, {"units": [{"unit_id": "A"}, {"unit_id": "A"}]}, [])[1]


def test_real_tool_cycle_theme_inventory_stable_same_and_fresh_instance_resume(tmp_path, monkeypatch):
    planner, boundary = make_planner(tmp_path, monkeypatch)
    monkeypatch.setattr(planner, "_tool_cycle", p.ProgressiveReviewPlanner._tool_cycle.__get__(planner))
    tool_calls = []
    def runner(**kwargs):
        tool_calls.append(kwargs["phase"])
        return {"directed_results": [{"materials": [{"paper_id": "early", "content": "Fresh reading establishes a conditional difference."}]}]}
    planner.retrieval_loop_runner = runner
    first = planner.run(stop_after="level1")
    assert "Fresh reading" in json.dumps(first["material_theme_inventory"])
    boundary.calls.clear()
    second = planner.run(resume=True, stop_after="level1")
    assert second["material_theme_inventory"] == first["material_theme_inventory"]
    assert boundary.calls == [] and tool_calls == ["level1"]
    fresh = p.ProgressiveReviewPlanner(planner.config, planner=boundary, retrieval_loop_runner=runner)
    third = fresh.run(resume=True, stop_after="level1")
    assert third["material_theme_inventory"] == first["material_theme_inventory"]
    assert boundary.calls == [] and tool_calls == ["level1"]
