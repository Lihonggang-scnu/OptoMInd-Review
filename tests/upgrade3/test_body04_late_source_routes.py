"""Offline actual-boundary regressions for late BODY selection opportunities."""
import copy
import json

from optomind_research.runtime.upgrade3 import progressive_review_plan as p


class OfflineModel:
    def __init__(self):
        self.calls = []
        self.chapters = [{"chapter_id": name, "title": name, "purpose": "Compare conditions"}
                         for name in ("CH01", "CH02")]

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "provisional_scope":
            return {"provisional_outline": self.chapters}
        if stage == "level1_outline":
            return {"shared_outline": self.chapters}
        if stage == "source_routing":
            return {"source_routes": [{"source_handle": row["source_handle"], "chapter_ids": ["CH02"],
                    "specific_usable_material": "Reported comparison of treatment conditions"}
                    for row in payload["candidate_batch"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": self.chapters}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": self.chapters, "chapters": self.chapters}
        if stage == "chapter_details":
            return {"chapter_plan": {"units": [{"unit_id": payload["chapter"]["chapter_id"] + "_U01",
                    "substantive_point": "Compare observed conditions", "paragraph_briefs": []}]}}
        if stage in {"whole_plan_improvement", "case_groups"}:
            return {}
        raise AssertionError(stage)


def make_planner(tmp_path, monkeypatch, *, late=True, known=True, content=True):
    plan = tmp_path / "PLAN.json"
    plan.write_text(json.dumps({"question": "How do conditions affect results?", "facets": [{"facet_id": "F1", "question": "conditions"}]}))
    pool = tmp_path / "POOL.jsonl"
    pool.write_text(json.dumps({"paper_id": "early", "planning_view": {
        "paper_identity": {"canonical_paper_id": "early", "title": "Early source"},
        "planning_summary": "Original comparison"}}) + "\n")
    model = OfflineModel()
    planner = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id="late",
        plan_path=plan, pool_path=pool, output_dir=tmp_path / "run", chapter_workers=1,
        planning_revision_enabled=True), planner=model)
    def tool_cycle(**kwargs):
        if kwargs["phase"] != "level2" or not late:
            return {}
        row = {"paper_id": "late", "title": "Late original study"}
        if content:
            row["supplement_gap_material"] = {"summary": "A review reports a controlled comparison with outcome Q under condition Z."}
        return {"supplement_results": [{"chapter_ids": ["CH01"] if known else [], "candidate_rows": [row]}]}
    monkeypatch.setattr(planner, "_tool_cycle", tool_cycle)
    monkeypatch.setattr(planner, "_post_case_review", lambda **kw: (kw["detail_records"], {}, {}))
    return planner, model


def test_late_usable_source_reaches_actual_case_input_and_persisted_routes(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch)
    planner.run()
    calls = {payload["chapter_id"]: payload for stage, payload in model.calls if stage == "case_groups"}
    late = next(row for row in calls["CH01"]["source_materials"] if row["source_handle"] == "P0002")
    assert "outcome Q" in json.dumps(late)
    assert all(row["source_handle"] != "P0002" for row in calls["CH02"]["source_routing"])
    saved = json.loads((planner.config.output_dir / "stages/source_routing_summary.json").read_text())
    assert saved["source_routes"][0]["source_handle"] == "P0001"
    assert saved["source_routes"][0]["chapter_ids"] == ["CH02"]
    assert saved["source_routes"][1]["chapter_ids"] == ["CH01"]
    assert len([1 for stage, _ in model.calls if stage == "source_routing"]) == 1
    model.calls.clear()
    planner.run(resume=True)
    assert not model.calls
    resumed = json.loads((planner.config.output_dir / "stages/source_routing_summary.json").read_text())
    assert saved == resumed


def test_unknown_context_routes_only_increment_and_resumes(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, known=False)
    planner.run()
    routing_calls = [payload for stage, payload in model.calls if stage == "source_routing"]
    assert [[row["source_handle"] for row in payload["candidate_batch"]] for payload in routing_calls] == [["P0001"], ["P0002"]]
    assert (planner.config.output_dir / "stages/source_routing/batch_001.json").exists()
    assert (planner.config.output_dir / "stages/late_source_routing/batch_001.json").exists()
    model.calls.clear()
    planner.run(resume=True)
    assert not model.calls


def test_identity_only_remains_visible_without_case_material_or_router_call(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, content=False)
    result = planner.run()
    saved = json.loads((planner.config.output_dir / "stages/source_routing_summary.json").read_text())
    late = next(row for row in saved["source_routes"] if row["source_handle"] == "P0002")
    assert late["route_status"] == "late_material_unavailable"
    assert late["chapter_ids"] == []
    assert len([1 for stage, _ in model.calls if stage == "source_routing"]) == 1
    for stage, payload in model.calls:
        if stage == "case_groups":
            assert all(row["source_handle"] != "P0002" for row in payload["source_routing"])
    assert "supporting_studies" not in json.dumps(result)


def test_normal_path_adds_no_routes_or_model_calls(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    planner.run()
    saved = json.loads((planner.config.output_dir / "stages/source_routing_summary.json").read_text())
    assert len(saved["source_routes"]) == 1
    assert "late_source_routes" not in saved
    assert len([1 for stage, _ in model.calls if stage == "source_routing"]) == 1
    model.calls.clear()
    planner.run(resume=True)
    assert not model.calls


def test_packet_only_resolved_review_material_gets_route_without_pool_card(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    packet_source = {"source_handle": "P0099", "paper_id": "original-study",
                     "doi": "10.1234/original", "title": "Original study",
                     "tool_supplement_materials": [{"summary": "Review reports outcome R in setting S."}]}
    records = [{"chapter": {"chapter_id": "CH01"}, "chapter_plan": {"units": []},
                "source_materials": [packet_source],
                "source_identity_map": {"P0099": {key: packet_source[key] for key in ("paper_id", "doi", "title")}}},
               {"chapter": {"chapter_id": "CH02"}, "chapter_plan": {},
                "candidate_materials": [packet_source]}]
    routing = {"source_routes": []}
    result = planner._route_late_sources(routing, [], detail_records=records, tool_results=[],
        shared_outline=model.chapters, resume=False, state={})
    assert result["source_routes"][0]["chapter_ids"] == ["CH01"]
    assert "outcome R" in result["source_routes"][0]["specific_usable_material"]
    assert not model.calls
    # Calling on an already merged ledger is idempotent.
    assert planner._route_late_sources(result, [], detail_records=records, tool_results=[],
        shared_outline=model.chapters, resume=True, state={}) == result
    records[0]["source_identity_map"]["P0099"]["doi"] = "10.1234/wrong"
    conflict = planner._route_late_sources(routing, [], detail_records=records, tool_results=[],
        shared_outline=model.chapters, resume=True, state={})
    assert conflict["source_routes"][0]["route_status"] == "late_identity_conflict"
    assert conflict["source_routes"][0]["chapter_ids"] == []
    assert not model.calls


def test_unknown_increment_cache_binds_actual_material(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    candidate = {"_source_handle": "P0002", "_paper_id": "late", "_b_summary": {"title": "Late study"},
                 "supplement_gap_material": {"summary": "Review reports condition A produces outcome B."}}
    records = [{"chapter": {"chapter_id": name}, "chapter_plan": {},
                "candidate_materials": [{"source_handle": "P0002"}]} for name in ("CH01", "CH02")]
    args = dict(detail_records=records, tool_results=[], shared_outline=model.chapters, state={})
    planner._route_late_sources({"source_routes": []}, [candidate], resume=False, **args)
    first = model.calls[-1][1]
    assert first["candidate_batch"][0]["supplement_material"]["supplement_gap_material"]["summary"].endswith("outcome B.")
    model.calls.clear()
    planner._route_late_sources({"source_routes": []}, [candidate], resume=True, **args)
    assert not model.calls
    candidate["supplement_gap_material"]["summary"] = "Review instead reports outcome C under condition D."
    planner._route_late_sources({"source_routes": []}, [candidate], resume=True, **args)
    assert len(model.calls) == 1
    assert "outcome C" in json.dumps(model.calls[0][1])


def test_late_pool_and_packet_handle_collision_cannot_route_foreign_content(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    pool = [{"_source_handle": "P0002", "_paper_id": "current-paper", "_b_summary": {"title": "Current"},
             "supplement_gap_material": {"summary": "Current paper reports actual comparison A."}}]
    stale = {"source_handle": "P0002", "paper_id": "stale-paper", "title": "Stale",
             "tool_supplement_materials": [{"summary": "Foreign findings must not be used for current paper."}]}
    records = [{"chapter": {"chapter_id": "CH01"}, "source_materials": [stale],
                "source_identity_map": {"P0002": {"paper_id": "stale-paper"}}}]
    result = planner._route_late_sources({"source_routes": []}, pool,
        detail_records=records, tool_results=[], shared_outline=model.chapters, resume=False, state={})
    assert result["source_routes"][0]["route_status"] == "late_identity_conflict"
    assert result["source_routes"][0]["chapter_ids"] == []
    assert result["source_routes"][0]["specific_usable_material"] == ""
    assert not model.calls


def test_tool_narrative_and_cited_identity_reach_case_selection_material(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    source = {"source_handle": "P0099", "paper_id": "original",
              "tool_materials": [{"summary": "Review reports outcome X in the original comparison.",
                                  "sources": [{"source_handle": "P0099", "paper_id": "original"}],
                                  "reporting_source": "review-paper"}]}
    records = [{"chapter": {"chapter_id": "CH01"}, "source_materials": [source],
                "source_identity_map": {"P0099": {"paper_id": "original"}}}]
    routed = planner._route_late_sources({"source_routes": []}, [], detail_records=records,
        tool_results=[], shared_outline=model.chapters, resume=False, state={})
    assert routed["source_routes"][0]["chapter_ids"] == ["CH01"]
    assert "outcome X" in routed["source_routes"][0]["specific_usable_material"]
    actual_material = p._case_selection_material_rows(["P0099"], records, [], {})[0]
    assert actual_material["tool_materials"] == source["tool_materials"]
    assert actual_material["material_available"] is True
    assert not model.calls


def test_identity_only_packet_cannot_hide_same_identity_pool_substance(tmp_path, monkeypatch):
    planner, model = make_planner(tmp_path, monkeypatch, late=False)
    candidate = {"_source_handle": "P0002", "_paper_id": "late", "title": "Late study", "_b_summary": {},
                 "supplement_gap_material": {"summary": "Review reports measured outcome M in comparison N."}}
    records = [{"chapter": {"chapter_id": "CH01"},
                "source_materials": [{"source_handle": "P0002", "paper_id": "late", "title": "Late study"}],
                "source_identity_map": {"P0002": {"paper_id": "late", "title": "Late study"}}}]
    routed = planner._route_late_sources({"source_routes": []}, [candidate], detail_records=records,
        tool_results=[], shared_outline=model.chapters, resume=False, state={})
    assert routed["source_routes"][0]["chapter_ids"] == ["CH01"]
    assert "outcome M" in routed["source_routes"][0]["specific_usable_material"]
    material = p._case_selection_material_rows(["P0002"], records, [candidate], {})[0]
    assert material["material_available"] is True
    assert "outcome M" in json.dumps(material)
    assert not model.calls
