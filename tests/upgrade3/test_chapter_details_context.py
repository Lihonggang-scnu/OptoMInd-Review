"""Focused offline checks for revision chapter-owner material projection."""

import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    ProgressiveReviewPlanner,
)


def test_revision_projection_keeps_findings_contributions_and_unknown_material():
    a = {
        "paper_kind": "primary research",
        "research_scope": "human solid tumour cohort",
        "approach": "prospective measurement",
        "key_findings": [{"conditions": "only under ICI exposure", "finding": "response differed"}],
        "contribution_and_limits": [{"contribution": "quantified the association", "limits": "observational"}],
        "work_summary": "duplicate broad summary",
        "problem_or_question": "duplicate question",
        "methodological_note": {"substantive": "retain this unknown field"},
    }
    b = {
        "planning_summary": "use the study for the mechanism comparison",
        "facet_contributions": [{
            "facet_id": "F1",
            "contribution": "complete contribution",
            "boundaries": "complete boundary",
            "possible_uses": ["generic use suggestion"],
            "evidence_detail": "retain this unknown field",
        }],
        "scope_interpretation_cautions": ["association is not causation"],
        "broader_review_uses": [{
            "material": "complete material",
            "connection_to_review": "direct connection",
            "purpose": "generic purpose suggestion",
            "review_specific_detail": "retain this unknown field",
        }],
        "topic_handles": ["identity metadata"],
        "snapshot_sha256": "hash metadata",
        "synthesis_note": "retain substantive unknown field",
    }
    a_before, b_before = copy.deepcopy(a), copy.deepcopy(b)
    projected_a = planning._project_chapter_a_material(a)
    projected_b = planning._project_chapter_b_material(b)

    assert a == a_before and b == b_before
    assert projected_a["key_findings"] == a["key_findings"]
    assert projected_a["contribution_and_limits"] == a["contribution_and_limits"]
    assert projected_a["methodological_note"] == a["methodological_note"]
    assert "work_summary" not in projected_a and "problem_or_question" not in projected_a
    assert projected_b["facet_contributions"][0]["contribution"] == "complete contribution"
    assert projected_b["facet_contributions"][0]["boundaries"] == "complete boundary"
    assert projected_b["facet_contributions"][0]["evidence_detail"] == "retain this unknown field"
    assert "possible_uses" not in projected_b["facet_contributions"][0]
    assert projected_b["broader_review_uses"][0]["material"] == "complete material"
    assert projected_b["broader_review_uses"][0]["connection_to_review"] == "direct connection"
    assert "purpose" not in projected_b["broader_review_uses"][0]
    assert "topic_handles" not in projected_b and "snapshot_sha256" not in projected_b
    assert projected_b["synthesis_note"] == "retain substantive unknown field"

    # Summary-only legacy cards keep their original shape rather than becoming
    # an empty A object when the normal core fields are absent.
    summary_only = {"work_summary": "only usable account", "problem_or_question": "scope"}
    assert planning._project_chapter_a_material(summary_only) == summary_only
    scope_only = {**summary_only, "paper_kind": "review", "research_scope": "scope", "key_findings": []}
    assert planning._project_chapter_a_material(scope_only) == scope_only


def _chapter_fixture(tmp_path: Path):
    card = {
        "general_understanding": {
            "paper_kind": "primary research",
            "research_scope": "scope",
            "approach": "approach",
            "key_findings": [{"conditions": "condition", "finding": "finding"}],
            "contribution_and_limits": [{"contribution": "contribution", "limits": "limits"}],
            "work_summary": "duplicate",
            "problem_or_question": "duplicate",
        },
        "review_planning": {
            "planning_summary": "summary",
            "facet_contributions": [{"facet_id": "F1", "contribution": "contribution", "boundaries": "boundary", "possible_uses": ["generic"]}],
            "scope_interpretation_cautions": ["caution"],
            "broader_review_uses": [{"material": "material", "connection_to_review": "connection", "purpose": "generic"}],
            "topic_handles": ["metadata"],
        },
    }
    card_path = tmp_path / "PAPER_READING_CARD.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    candidate = {
        "_paper_id": "paper1",
        "_source_handle": "P0001",
        "card_path": str(card_path),
        "planning_view": {"paper_identity": {"title": "Paper", "doi": "10.1/test", "year": "2026"}},
        "_b_summary": {"declared_content_depth": "abstract"},
    }
    chapter = {"chapter_id": "CH01", "title": "Chapter", "purpose": "purpose", "scope": "scope", "source_ids": ["paper1"]}
    return candidate, chapter, card


def test_chapter_details_projection_does_not_change_full_packet_or_legacy(tmp_path):
    candidate, chapter, card = _chapter_fixture(tmp_path)
    captured = []

    def model(stage, payload):
        captured.append((stage, payload))
        return {"chapter_plan": {
            "thesis": "thesis", "reader_objective": "objective",
            "units": [{"unit_id": "U01", "substantive_point": "point", "source_handles": ["P0001"],
                       "paragraph_briefs": [{"point": "point", "development": "development", "source_handles": ["P0001"]}]}],
        }}

    revised = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(topic_id="fixture", plan_path=tmp_path / "PLAN.json", pool_path=tmp_path / "POOL.jsonl",
                                 output_dir=tmp_path / "revised", chapter_workers=1, planning_revision_enabled=True),
        planner=model,
    )
    revised._parts_plan = {
        "context": "body context",
        **{
            name: {"purpose": f"{name} purpose", "focus": [], "boundary": [],
                   "placement": {"mode": "standalone", "anchor": name}, "finalize_from": []}
            for name in ("abstract", "introduction", "conclusion")
        },
    }
    packets = revised._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=False, state={},
    )
    revision_source = captured[-1][1]["source_materials"][0]
    packet_source = packets[0]["source_materials"][0]
    assert "work_summary" not in revision_source["study_summary_A"]
    assert "possible_uses" not in revision_source["planning_material"]["facet_contributions"][0]
    assert packet_source["study_summary_A"] == card["general_understanding"]
    assert packet_source["review_planning_B"] == card["review_planning"]

    captured.clear()
    legacy = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(topic_id="fixture", plan_path=tmp_path / "PLAN.json", pool_path=tmp_path / "POOL.jsonl",
                                 output_dir=tmp_path / "legacy", chapter_workers=1, planning_revision_enabled=False),
        planner=model,
    )
    legacy._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=False, state={},
    )
    legacy_source = captured[-1][1]["source_materials"][0]
    assert legacy_source["planning_material"] == card["review_planning"]


def test_revision_resume_ignores_appended_navigation_sources_but_tracks_real_changes(tmp_path, monkeypatch):
    candidate, chapter, card = _chapter_fixture(tmp_path)
    calls = []

    def model(stage, payload):
        calls.append(payload)
        return {"chapter_plan": {
            "thesis": "thesis", "reader_objective": "objective",
            "units": [{"unit_id": "U01", "substantive_point": "point", "source_handles": ["P0001"],
                       "paragraph_briefs": [{"point": "point", "development": "development", "source_handles": ["P0001"]}]}],
        }}

    nav_version = ["v1"]

    def fake_navigation(**_kwargs):
        return {
            "candidate_materials": [{"source_handle": "P0002", "title": "Unassigned candidate", "version": nav_version[0]}],
            "candidate_count": 1,
        }

    monkeypatch.setattr(planning, "build_candidate_navigation", fake_navigation)
    parts_plan = {
        "context": "body context",
        **{
            name: {"purpose": f"{name} purpose", "focus": [], "boundary": [],
                   "placement": {"mode": "standalone", "anchor": name}, "finalize_from": []}
            for name in ("abstract", "introduction", "conclusion")
        },
    }

    def make_planner(output_dir):
        planner = ProgressiveReviewPlanner(
            ProgressivePlannerConfig(topic_id="fixture", plan_path=tmp_path / "PLAN.json", pool_path=tmp_path / "POOL.jsonl",
                                     output_dir=output_dir, chapter_workers=1, planning_revision_enabled=True),
            planner=model,
        )
        planner._parts_plan = parts_plan
        return planner

    output = tmp_path / "resume-assigned"
    make_planner(output)._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=False, state={}, candidate_pool=[candidate],
    )
    assert len(calls) == 1
    saved = json.loads((output / "stages" / "chapters" / "CH01.json").read_text(encoding="utf-8"))
    assert any(item.get("source_role") == "candidate_navigation" for item in saved["source_materials"])

    make_planner(output)._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=True, state={}, candidate_pool=[candidate],
    )
    assert len(calls) == 1

    card["general_understanding"]["approach"] = "assigned A changed"
    Path(candidate["card_path"]).write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    make_planner(output)._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=True, state={}, candidate_pool=[candidate],
    )
    assert len(calls) == 2

    navigation_output = tmp_path / "resume-navigation"
    make_planner(navigation_output)._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=False, state={}, candidate_pool=[candidate],
    )
    nav_version[0] = "v2"
    make_planner(navigation_output)._chapter_details(
        chapters=[chapter], shared_outline={}, topic="topic", candidates={"paper1": candidate},
        level1_tools={}, level2_tools={}, resume=True, state={}, candidate_pool=[candidate],
    )
    assert len(calls) == 4


def test_actual_ch03_revision_payload_is_within_local_token_budget():
    """Measure the current 418-source CH03 shape without contacting a model."""
    run_root = Path(__file__).resolve().parents[2].parent / "new_plan"
    stage_path = run_root / "stages" / "chapters_tools.json"
    route_path = run_root / "stages" / "source_routing_summary.json"
    scope_path = run_root / "stages" / "harmonized_scope.json"
    state_path = run_root / "RUN_STATE.json"
    if not all(path.is_file() for path in (stage_path, route_path, scope_path, state_path)):
        pytest.skip("acceptance runroot is unavailable")

    stage = json.loads(stage_path.read_text(encoding="utf-8"))
    routes = json.loads(route_path.read_text(encoding="utf-8"))["source_routes"]
    scope = json.loads(scope_path.read_text(encoding="utf-8"))["response"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    pool_by_handle = {row.get("_source_handle"): row for row in stage.get("pool_rows") or []}
    chapter_routes = []
    seen = set()
    for route in routes:
        handle = route.get("source_handle")
        if handle and "CH03" in (route.get("chapter_ids") or []) and handle not in seen:
            chapter_routes.append(route)
            seen.add(handle)
    assert len(chapter_routes) == 418

    source_materials = []
    for route in chapter_routes:
        candidate = pool_by_handle[route["source_handle"]]
        card = planning._card_for_candidate(candidate)
        a = card.get("general_understanding") if isinstance(card.get("general_understanding"), dict) else {}
        b = card.get("review_planning") if isinstance(card.get("review_planning"), dict) else {}
        planning_view = candidate.get("planning_view") if isinstance(candidate.get("planning_view"), dict) else {}
        identity = planning_view.get("paper_identity") if isinstance(planning_view.get("paper_identity"), dict) else {}
        b_summary = candidate.get("_b_summary") if isinstance(candidate.get("_b_summary"), dict) else {}
        source_materials.append({
            "source_handle": candidate.get("_source_handle"),
            "title": identity.get("title") or candidate.get("title"),
            "material_depth": b_summary.get("declared_content_depth", ""),
            "study_summary_A": planning._project_chapter_a_material(a),
            "planning_material": planning._project_chapter_b_material(b),
            "supplement_material": candidate.get("supplement_gap_material") or {},
            "supplement_materials": [dict(item) for item in candidate.get("supplement_gap_materials") or [] if isinstance(item, dict)],
            "deep_read_material": {},
            "routing_note": {key: route[key] for key in ("chapter_ids", "specific_usable_material", "interpretation_limits", "reason") if key in route},
        })
    chapter = next(row for row in scope["chapters"] if row.get("chapter_id") == "CH03")
    payload = {
        "topic_id": state["topic_id"],
        "research_question": state["topic"],
        "shared_outline": scope.get("shared_outline") or {},
        "chapter": chapter,
        "position": {"index": 3, "count": 5},
        "next_chapter_title": "",
        "previous_chapter_title": "",
        "source_materials": source_materials,
        "relevant_tool_feedback": [],
        "citation_rules": dict(planning.CURRENT_CITATION_RULES),
        "candidate_navigation": {},
        "candidate_materials": [],
        "planning_revision_mode": True,
        "tool_feedback_scope": {"unanswered_need_means": "bounded"},
        "required_behavior": {"inspect_relevant_unassigned_candidates": True},
    }
    counter = planning.qwen_local_token_counter(planning.DEFAULT_TOKENIZER_PATH)
    raw_tokens = counter(b"", planning._messages_for("chapter_details", payload))
    estimated = int(raw_tokens * planning.TOKEN_MARGIN_MULTIPLIER + 0.999999) + planning.TOKEN_FRAMING_MARGIN
    assert estimated < 900_000


def test_case_pool_fallback_keeps_card_deep_and_local_material(tmp_path: Path):
    """The case fallback uses the single-paper deep-material helper contract."""
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps({
        "general_understanding": {"paper_kind": "primary", "key_findings": ["finding"]},
        "review_planning": {"planning_summary": "useful planning context"},
    }), encoding="utf-8")
    candidate = {
        "_paper_id": "paper1",
        "_source_handle": "P0591",
        "card_path": str(card_path),
        "planning_view": {"paper_identity": {"title": "Example study", "doi": "10/example"}},
        "_b_summary": {"declared_content_depth": "abstract"},
        "supplement_gap_material": {"gap_id": "G1", "useful_material": "supplement"},
        "supplement_gap_materials": [{"gap_id": "G1", "useful_material": "supplement"}],
    }
    deep = {"paper_id": "paper1", "content": {"finding": "deep evidence"}}
    payload = planning.build_local_material_payload(
        candidate,
        deep_material=deep,
        question="local question",
        hits=[{"paper_id": "paper1", "text": "local passage"}],
    )
    assert payload["study_summary_A"]["key_findings"] == ["finding"]
    assert payload["review_planning_B"]["planning_summary"] == "useful planning context"
    assert payload["deep_read_material"]["content"]["finding"] == "deep evidence"
    assert payload["local_passages"]["passages"][0]["text"] == "local passage"
    assert payload["supplement_gap_material"]["gap_id"] == "G1"

    rows = planning._case_selection_material_rows(
        ["P0591"], [], [candidate], {"paper1": deep},
    )
    assert rows[0]["study_summary_A"]["key_findings"] == ["finding"]
    assert rows[0]["review_planning_B"]["planning_summary"] == "useful planning context"
    assert rows[0]["deep_read_material"]["content"]["finding"] == "deep evidence"
    assert rows[0]["supplement_gap_material"]["gap_id"] == "G1"
