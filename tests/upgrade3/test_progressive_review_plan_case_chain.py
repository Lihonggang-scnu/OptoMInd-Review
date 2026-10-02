"""Offline regressions for the restored BODY case chain."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view,
    build_source_catalog,
    write_view,
)
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    ProgressiveReviewPlanner,
    CASE_GROUPS_PROMPT_CONTRACT,
    _messages_for,
)
from optomind_research.runtime.upgrade3.review_unit_writer import (
    build_unit_view,
    unit_messages,
    unit_payload,
)


def _candidate(tmp_path: Path, handle: str = "P0002") -> dict:
    card = tmp_path / (handle + "_CARD.json")
    card.write_text(json.dumps({
        "general_understanding": {"approach": "A card-backed comparison"},
        "review_planning": {"planning_summary": "B gives the concrete writing use"},
    }), encoding="utf-8")
    return {
        "_source_handle": handle,
        "_paper_id": "paper-2",
        "title": "Candidate case",
        "planning_view": {
            "paper_identity": {"canonical_paper_id": "paper-2", "title": "Candidate case"},
            "planning_summary": "B gives the concrete writing use",
        },
        "card_path": str(card),
    }


def test_body_case_append_materializes_real_card_and_reaches_writer_message(tmp_path):
    records = [{
        "chapter": {"chapter_id": "CH01", "title": "Mechanism"},
        "chapter_plan": {
            "thesis": "Explain the mechanism",
            "units": [
                {"unit_id": "CH01_U01", "substantive_point": "Context", "paragraph_briefs": []},
                {
                    "unit_id": "CH01_U02",
                    "substantive_point": "Concrete comparison",
                    "paragraph_briefs": [{
                        "point": "Compare the conditions",
                        "development": "Use the card-backed comparison",
                        "source_handles": [],
                    }],
                    "supporting_studies": [],
                },
            ],
        },
        "source_materials": [],
    }]
    contribution = "Use this study as a concrete comparison of the stated conditions."
    attached = planning._attach_case_groups(
        records,
        {"additions": [{"unit_key": "CH01:2", "studies": [{
            "source_handle": "P0002", "contribution": contribution,
        }]}]},
        planning_revision=True,
        body_case_additions=True,
        candidate_rows=[_candidate(tmp_path)],
    )
    unit = attached[0]["chapter_plan"]["units"][1]
    assert unit["supporting_studies"] == [{
        "source_handle": "P0002", "contribution": contribution,
    }]
    assert "case_suggestions" not in unit
    source = attached[0]["source_materials"][0]
    assert source["source_handle"] == "P0002"
    assert source["study_summary_A"]["approach"] == "A card-backed comparison"
    assert source["review_planning_B"]["planning_summary"] == "B gives the concrete writing use"

    packet_path = tmp_path / "writer_packet.json"
    packet = {
        **attached[0],
        "research_question": "How does the mechanism work?",
        "review_argument": "Use material-backed comparisons.",
    }
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    unit_view = next(item for item in view.units if item.unit_id == "CH01_U02")
    assert unit_view.case_uses == [{
        "field": "supporting_studies",
        "source_handle": "P0002",
        "paper_id": "",
        "text": contribution,
        "conditions": "",
        "unit_id": "CH01_U02",
    }]
    assert view.source_uses["P0002"][0]["kind"] in {"paragraph", "case"}

    arrangement = {
        "chapter_id": "CH01",
        "chapter_argument": "Use material-backed comparisons.",
        "units": [
            {"unit_id": "CH01_U01", "focus": "Context", "paragraph_tasks": []},
            {"unit_id": "CH01_U02", "focus": "Concrete comparison", "paragraph_tasks": [{
                "paragraph_id": "CH01_U02_P01",
                "point": "Compare the conditions",
                "development": "Use the card-backed comparison",
                "source_uses": [{"source_handle": "P0002", "role": "case", "use": contribution}],
            }]},
        ],
    }
    arrangement["source_catalog"] = build_source_catalog(view, arrangement)
    arrangement_path = tmp_path / "ARRANGEMENT.json"
    arrangement_path.write_text(json.dumps(arrangement, ensure_ascii=False), encoding="utf-8")
    view_path = write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    writing_view = build_unit_view(arrangement_path, "CH01_U02", view_path=view_path)
    payload = unit_payload(writing_view)
    material = next(item for item in payload["sources"] if item["source_handle"] == "P0002")
    assert material["study_summary_A"]["approach"] == "A card-backed comparison"
    assert material["review_planning_B"]["planning_summary"] == "B gives the concrete writing use"
    writer_user = json.loads(unit_messages(writing_view, planning_revision=True)[1]["content"])
    writer_material = next(item for item in writer_user["sources"] if item["source_handle"] == "P0002")
    assert writer_material["source_handle"] == "P0002"
    assert contribution in json.dumps(writer_user, ensure_ascii=False)


def test_revision_chain_runs_owner_before_direct_case_append(tmp_path):
    class OfflinePlanner:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []
            self.chapter = {"chapter_id": "CH01", "title": "Mechanism", "purpose": "Explain the mechanism", "scope": "Material-backed mechanism"}

        def __call__(self, stage, payload):
            self.calls.append((stage, copy.deepcopy(dict(payload))))
            if stage == "provisional_scope":
                return {"provisional_outline": [self.chapter], "material_theme_inventory": ["mechanism"], "supplement_requests": [], "directed_reads": []}
            if stage == "level1_outline":
                return {"shared_outline": [self.chapter], "supplement_requests": [], "directed_reads": []}
            if stage == "source_routing":
                return {"source_routes": [{"source_handle": "P0001", "chapter_ids": ["CH01"], "specific_usable_material": "A concrete mechanism source."}]}
            if stage == "chapter_proposals":
                return {"chapter_proposals": [self.chapter]}
            if stage in {"harmonize_scope", "finalize_chapter_scope"}:
                return {"shared_outline": [self.chapter], "chapters": [self.chapter]}
            if stage == "chapter_details":
                return {"chapter_plan": {"thesis": "Explain the mechanism", "reader_objective": "Understand the mechanism", "units": [{
                    "unit_id": "CH01_U01", "substantive_point": "The mechanism depends on conditions", "source_handles": ["P0001"],
                    "paragraph_briefs": [{"point": "State the mechanism", "development": "Relate it to the conditions", "source_handles": ["P0001"]}],
                }]}}
            if stage == "whole_plan_improvement":
                return {"affected_chapters": [{"chapter_id": "CH01", "feedback": "Keep the mechanism conditions explicit."}]}
            if stage == "affected_chapter_revision":
                return {"status": "no_change"}
            if stage == "case_groups":
                return {"additions": [{"unit_key": "CH01:1", "studies": [{
                    "source_handle": "P0001", "contribution": "Use this source as the concrete mechanism case.",
                }]}]}
            raise AssertionError(stage)

    plan_path = tmp_path / "PLAN.json"
    plan_path.write_text(json.dumps({"question": "How does the mechanism work?", "facets": [{"facet_id": "F1", "question": "mechanism"}]}), encoding="utf-8")
    pool_path = tmp_path / "POOL.jsonl"
    pool_path.write_text(json.dumps({
        "paper_id": "paper-1",
        "planning_view": {"paper_identity": {"canonical_paper_id": "paper-1", "title": "Mechanism source"}, "planning_summary": "Mechanism material"},
        "summary_view": {"finding": "A mechanism result"},
    }) + "\n", encoding="utf-8")
    model = OfflinePlanner()
    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="case-chain",
            plan_path=plan_path,
            pool_path=pool_path,
            output_dir=tmp_path / "run",
            chapter_workers=1,
            planning_revision_enabled=True,
        ),
        planner=model,
    )
    result = planner.run()
    stages = [stage for stage, _payload in model.calls]
    assert "affected_chapter_revision" in stages, stages
    assert stages.index("whole_plan_improvement") < stages.index("affected_chapter_revision") < stages.index("case_groups")
    unit = result["chapters"][0]["chapter_plan"]["units"][0]
    assert unit["supporting_studies"][0]["contribution"].startswith("Use this source")
    assert "case_suggestions" not in unit


def test_case_prompt_uses_formal_contribution_and_direct_append_contract():
    messages = _messages_for("case_groups", {"planning_revision_mode": True, "unit_catalog": []})
    system, user = messages
    assert '"contribution"' in user["content"]
    assert "本阶段直接把已选案例追加到正文计划" in system["content"]
    assert "最终案例由章节负责人对照材料确认" not in system["content"]
    assert "case_suggestions" not in system["content"]
    assert CASE_GROUPS_PROMPT_CONTRACT == "case_groups.review_v2_04_body_append_contribution"
