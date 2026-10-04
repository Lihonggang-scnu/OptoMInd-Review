"""Independent second-stop regressions: real store/adapters and actual messages.

Only the model boundary is replaced. Set BODY_PREFLIGHT_REVIEW_EVIDENCE_ROOT to
retain synthetic, source-bound before/after consumer evidence.
"""
from __future__ import annotations
import copy
import json
import os
import socket
from pathlib import Path
import pytest
from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3 import progressive_review_plan as p


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Independent review forbids network")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


class Boundary:
    def __init__(self):
        self.calls = []
    def complete(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return {"content": {"question_material": [{"question_id": "Q01", "explanation": f"CONTROLLED_ANSWER_{len(self.calls)}", "remaining_points": []}]}}


def evidence(name, value):
    root = os.environ.get("BODY_PREFLIGHT_REVIEW_EVIDENCE_ROOT")
    if root:
        path = Path(root) / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def test_current_title_used_consistently_in_actual_lower_messages_and_artifact(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "READING_VIEW.md").write_text("A controlled source body under condition alpha.")
    paper = {"canonical_paper_id": "source-one", "title": "Original title", "material_scope": "fulltext", "approve_core": True,
        "nomination_reason": "Answer the synthetic question", "expected_information_gain": "Explain the fictional mechanism",
        "core_justification": "Required for this synthetic test", "knowledge_gap": "Explain synthetic mechanism details", "required_outputs": ["O01"]}
    request = dr.build_directed_request(review_id="review", topic="Synthetic topic", chapter={"chapter_id": "CH01", "title": "One"},
        questions=[{"question_id": "Q01", "question": "What changed?", "purpose": "Report finding", "required_output_ids": ["O01"], "gap_key": "finding"}],
        required_outputs=[{"output_id": "O01", "output_type": "explanation", "description": "Report change"}])
    store = dr.DirectedReadingStore(tmp_path / "directed.sqlite", core_cap=1)
    store.admit_candidate(request["review_id"], request["topic_binding"], paper)
    model = Boundary()
    args = dict(request=request, snapshot_dir=snapshot, output_dir=tmp_path / "result", store=store, client=model)
    first = dr.run_directed_reading(**args, paper=paper)
    old_path = Path(first["output_dir"]) / "DIRECTED_READING.json"
    old_bytes = old_path.read_bytes()
    current = {**paper, "title": "Corrected current title", "doi": "10.1234/current"}
    second = dr.run_directed_reading(**args, paper=current)
    messages = [json.loads(call[1]["content"]) for call in model.calls]
    evidence("CURRENT_TITLE_CONSUMER", {"reader_user_messages": messages, "before": first["output"], "after": second["output"],
        "calls": len(model.calls), "prior_artifact_unchanged": old_path.read_bytes() == old_bytes})
    assert len(model.calls) == 2 and not second["reused"]
    assert messages[-1]["paper_title"] == current["title"]
    assert second["output"]["paper_title"] == current["title"]
    assert second["output"]["paper_identity"]["title"] == current["title"]
    assert old_path.read_bytes() == old_bytes


@pytest.mark.parametrize("identity_change", [False, True], ids=["source_correction", "explicit_doi_conflict"])
def test_adaptive_corrected_source_does_not_reactivate_prior_content_in_chapter(tmp_path, monkeypatch, identity_change):
    source = tmp_path / "source"
    snapshot = source / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "READING_VIEW.md").write_text("Prior source body under condition alpha.")
    card = source / "PAPER_READING_CARD.json"
    card_body = {"paper_identity": {"canonical_paper_id": "source-one", "doi": "10.1234/prior", "title": "Example study"}, "material": {"material_scope": "fulltext"}}
    card.write_text(json.dumps(card_body))
    (source / "SOURCE_UNIT.json").write_text(json.dumps({"snapshot_path": str(snapshot)}))
    boundary = Boundary()
    monkeypatch.setattr(dr, "QwenDirectClient", lambda **kwargs: boundary)
    key = tmp_path / "dummy-key"
    key.write_text("fixture-only-not-a-credential")
    config = p.ProgressivePlannerConfig("review", tmp_path / "POOL.jsonl", tmp_path / "PLAN.json", tmp_path / "run", reader_workers=1, chapter_workers=1, shared_deep_read_budget=1, planning_revision_enabled=True)
    task = {"paper_id": "source-one", "questions": [{"question_id": "Q01", "question": "What changed?", "purpose": "Report finding", "required_output_ids": ["O01"]}], "required_outputs": [{"output_id": "O01", "description": "Report change"}], "chapter_ids": ["CH01"]}
    pool = [{"_paper_id": "source-one", "_source_handle": "P0001", "doi": "10.1234/prior", "title": "Example study", "card_path": str(card)}]
    def invoke():
        reader = p.make_directed_reading_runner(config, key_file=key, budget_ledger_path=tmp_path / "ledger.sqlite", budget_limit_cny=30)
        runner = p.make_retrieval_loop_runner(config, allow_external=False, directed_reader=reader)
        return runner(phase="same", directed_requests=[task], pool_rows=pool, source_handle_map={"P0001": "source-one"}, resume=True)
    first = invoke()
    old_files = {path: path.read_bytes() for path in config.output_dir.rglob("DIRECTED_READING.json")}
    assert len(boundary.calls) == 1 and old_files
    if identity_change:
        card_body["paper_identity"]["doi"] = "10.1234/current"
        card.write_text(json.dumps(card_body))
        pool[0]["doi"] = "10.1234/current"
    (snapshot / "READING_VIEW.md").write_text("Corrected source body under condition beta.")
    second = invoke()
    cache = json.loads((config.output_dir / "DIRECTED_MATERIAL_CACHE.json").read_text())
    actual_messages = []
    def chapter_model(stage, payload):
        actual_messages.extend(p._messages_for(stage, payload))
        return {"chapter_plan": {"thesis": "Explain the corrected finding", "units": [{"unit_id": "CH01_U01", "substantive_point": "Explain conditions", "paragraph_briefs": [{"point": "Report finding", "development": "Preserve conditions", "source_handles": ["P0001"]}]}]}}
    planner = p.ProgressiveReviewPlanner(config, planner=chapter_model)
    packets = planner._chapter_details(chapters=[{"chapter_id": "CH01", "title": "Finding", "source_ids": ["source-one"]}],
        shared_outline={}, topic="What changed?", candidates={"source-one": pool[0]}, level1_tools={}, level2_tools=second, resume=False, state={}, candidate_pool=pool)
    name = "DOI_CONFLICT_CONSUMER" if identity_change else "SOURCE_CORRECTION_CONSUMER"
    evidence(name, {"calls": len(boundary.calls), "reader_user_messages": [json.loads(call[1]["content"]) for call in boundary.calls],
        "before_material": first["directed_results"], "current_material": cache["materials"]["source-one"], "chapter_messages": actual_messages,
        "packet_source_materials": packets[0]["source_materials"], "old_artifacts_unchanged": all(path.read_bytes() == data for path, data in old_files.items())})
    assert len(boundary.calls) == 2
    assert "CONTROLLED_ANSWER_1" not in json.dumps(cache["materials"]["source-one"])
    assert "CONTROLLED_ANSWER_1" not in json.dumps(actual_messages)
    assert "CONTROLLED_ANSWER_1" not in json.dumps(packets[0]["source_materials"])
    assert "CONTROLLED_ANSWER_2" in json.dumps(actual_messages)
    assert all(path.read_bytes() == data for path, data in old_files.items())
