"""Full scientific records survive the formal planner's model boundary, offline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import progressive_review_plan as cli


def live_adapter(tmp_path, monkeypatch):
    """Use the production message/capacity adapter with a fake transport only."""
    model = object.__new__(p.QwenProgressivePlanner)
    model.model = model.chapter_model = "offline-model"
    model.output_tokens = 16000
    model.thinking_budget = 2048
    model.counter = lambda _encoding, messages: sum(len(m["content"]) for m in messages) // 4
    model.output_dir = tmp_path
    model.key_file = tmp_path / "unused-key"
    model.timeout_seconds = 10
    model.ledger = None
    calls = []
    monkeypatch.setattr(runtime, "QwenDirectClient", lambda **kwargs: object())

    def invoke(_client, messages, **kwargs):
        calls.append(messages)
        return {"chapter_plan": {"thesis": "Measured effect", "units": [{
            "unit_id": "CH01_U01", "substantive_point": "Explain the effect",
            "paragraph_briefs": [{"point": "Finding", "development": "Preserve its conditions"}],
        }]}}

    monkeypatch.setattr(runtime, "invoke_client", invoke)
    return model, calls


def decoded(messages):
    content = messages[-1]["content"]
    return json.JSONDecoder().raw_decode(content[content.index("{"):])[0]


def test_formal_defaults_and_explicit_legacy_mode(tmp_path):
    argv = ["--pool", "pool", "--plan", "plan", "--output-dir", "out"]
    assert cli.parser().parse_args(argv).planning_revision is True
    assert cli.parser().parse_args(argv + ["--planning-revision"]).planning_revision is True
    assert cli.parser().parse_args(argv + ["--no-planning-revision"]).planning_revision is False
    assert p.ProgressivePlannerConfig(topic_id="test", pool_path=tmp_path, plan_path=tmp_path,
                                      output_dir=tmp_path).planning_revision_enabled is True


@pytest.mark.parametrize("revision", [True, False])
@pytest.mark.parametrize("domain", ["battery cycling", "clinical cohort", "historical archive"])
def test_chapter_actual_messages_preserve_long_ab_in_both_modes(tmp_path, monkeypatch, revision, domain):
    finding = (domain + " detailed research finding. ") * 110 + "FINDING_TAIL"
    conditions = (domain + " population, setting, method and controls. ") * 100 + "CONDITIONS_TAIL"
    a = {"finding": finding, "research_conditions": conditions}
    b = {"planning_summary": finding, "paragraph_material": {"conditions": conditions}}
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"general_understanding": a, "review_planning": b}))
    source = {"_paper_id": "paper-1", "_source_handle": "P0001", "card_path": str(card),
              "title": domain, "planning_view": {"paper_identity": {"title": domain}}}
    model, calls = live_adapter(tmp_path, monkeypatch)
    cfg = p.ProgressivePlannerConfig(topic_id="test", pool_path=tmp_path / "pool",
        plan_path=tmp_path / "plan", output_dir=tmp_path / "run", chapter_workers=1,
        planning_revision_enabled=revision)
    flow = p.ProgressiveReviewPlanner(cfg, planner=model)
    stages = cfg.output_dir / "stages"
    stages.mkdir(parents=True)
    (stages / "source_routing_summary.json").write_text(json.dumps({"source_routes": [{
        "source_handle": "P0001", "chapter_ids": ["CH01"], "specific_usable_material": "THIN_ROUTE_ONLY"}]}))
    flow._chapter_details(chapters=[{"chapter_id": "CH01", "title": domain, "source_ids": ["paper-1"]}],
        shared_outline={}, topic=domain, candidates={"paper-1": source},
        level1_tools={}, level2_tools={}, resume=False, state={})
    assert len(calls) == 1
    material = decoded(calls[0])["source_materials"][0]
    assert material["study_summary_A"] == a
    assert material["planning_material"] == b
    assert material["routing_note"]["specific_usable_material"] == "THIN_ROUTE_ONLY"
    assert ("candidate_navigation" in decoded(calls[0])) is revision


def test_case_actual_messages_preserve_full_atomic_strings(tmp_path, monkeypatch):
    model, calls = live_adapter(tmp_path, monkeypatch)
    finding = "complete finding " * 600 + "FINDING_TAIL"
    conditions = "population and methods " * 600 + "CONDITIONS_TAIL"
    material = {"source_handle": "P0001", "paper_id": "paper-1",
        "study_summary_A": {"finding": finding, "conditions": conditions},
        "review_planning_B": {"paragraphs": [{"point": finding, "conditions": conditions}]},
        "deep_read_material": {"paper_id": "paper-1", "question_material": [
            {"explanation": finding, "conditions": conditions}]} }
    projected = p._case_selection_material_row(material)
    model("case_groups", {"source_materials": [projected]})
    actual = decoded(calls[0])["source_materials"][0]
    for field in ("study_summary_A", "review_planning_B"):
        assert actual[field] == material[field]
    assert actual["deep_read_material"]["question_material"] == material["deep_read_material"]["question_material"]
    assert "full_material_records" in p.CASE_GROUPS_PROMPT_CONTRACT


def test_oversized_case_material_fails_before_transport_without_clipping(tmp_path, monkeypatch):
    model, calls = live_adapter(tmp_path, monkeypatch)
    model.counter = lambda _encoding, messages: 1_000_000
    material = p._case_selection_material_row({"study_summary_A": {"finding": "x" * 5000 + "TAIL"}})
    with pytest.raises(p.ProgressivePlanError, match="planner_context_preflight_exceeded:case_groups"):
        model("case_groups", {"source_materials": [material]})
    assert material["study_summary_A"]["finding"].endswith("TAIL")
    assert calls == []
