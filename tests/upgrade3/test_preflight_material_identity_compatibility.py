"""F4: actual local adapter, store, cache and chapter messages; model boundary only."""
from __future__ import annotations

import copy
import json
import shutil
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3 import progressive_review_plan as p


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network denied in F4 offline control")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def task():
    return {"paper_id": "paper-A", "questions": [{"question_id": "Q01", "question": "What changed?", "purpose": "Compare conditions", "required_output_ids": ["O01"]}], "required_outputs": [{"output_id": "O01", "description": "Report change"}]}


def fixture(tmp_path, monkeypatch):
    snapshot = tmp_path / "source" / "snapshot-original"
    snapshot.mkdir(parents=True)
    (snapshot / "READING_VIEW.md").write_text("## Abstract\nORIGINAL: sample increased from 2 to 4 at 10 K.\n")
    dump(snapshot / "manifest.json", {"snapshot_id": "snapshot-original"})
    card = tmp_path / "source" / "PAPER_READING_CARD.json"
    identity = {"paper_id": "paper-A", "title": "Synthetic paper A", "doi": "10.synthetic/a"}
    dump(card, {"paper_identity": identity, "general_understanding": {"finding": "A_FINDING"}, "review_planning": {"planning_summary": "A_PLANNING"}, "material": {"material_scope": "fulltext", "snapshot_id": "snapshot-original"}})
    dump(card.parent / "SOURCE_UNIT.json", {"snapshot_path": str(snapshot)})
    row = {"_paper_id": "paper-A", "_source_handle": "P0001", "title": identity["title"], "doi": identity["doi"], "card_path": str(card), "planning_view": {"paper_identity": identity}, "_b_summary": {"declared_content_depth": "fulltext"}}
    key = tmp_path / "synthetic-placeholder.txt"
    key.write_text("synthetic-not-a-real-key")
    calls = []
    class ModelBoundary:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            response = {"question_material": [{"question_id": "Q01", "explanation": "REVISED_ANSWER" if "REVISED:" in str(messages) or "Changed citation identity" in str(messages) else "ORIGINAL_ANSWER", "remaining_points": []}]}
            return {"content": json.dumps(response), "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(dr, "QwenDirectClient", ModelBoundary)
    config = p.ProgressivePlannerConfig(topic_id="f4-offline", pool_path=tmp_path / "POOL.jsonl", plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "run", reader_workers=1, chapter_workers=1, shared_deep_read_budget=1)
    context = {"pool_by_id": {"paper-A": row}, "output_dir": tmp_path / "phase"}
    def runner(prior=()):
        return p.make_directed_reading_runner(config, key_file=key, budget_ledger_path=tmp_path / "ledger.json", budget_limit_cny=1, prior_readings=prior)
    return config, row, snapshot, context, calls, runner


def chapters(tmp_path, candidate, monkeypatch, *, revision=True):
    from optomind_research.runtime.upgrade3.module4 import runtime as transport
    calls = []
    class OwnerBoundary:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, **kwargs):
            raw = messages[-1]["content"]
            payload, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
            call = {"payload": copy.deepcopy(payload), "messages": copy.deepcopy(messages)}
            calls.append(call)
            dump(tmp_path / "chapter_messages" / f"{len(calls):03d}.json", call)
            response = {"chapter_plan": {"thesis": "Explain the measured change", "units": [{"unit_id": "CH01_U01", "substantive_point": "Explain conditions", "paragraph_briefs": [{"point": "Explain the change", "development": "Compare conditions"}]}]}}
            return {"content": json.dumps(response), "complete": True, "finish_reason": "stop", "usage": {"input_tokens": 0, "output_tokens": 0}}
    monkeypatch.setattr(transport, "QwenDirectClient", OwnerBoundary)
    # Exercise the real planner adapter without loading a tokenizer/paid ledger.
    model = object.__new__(p.QwenProgressivePlanner)
    model.model = "offline-outline"
    model.chapter_model = "offline-owner"
    model.output_tokens = 18000
    model.thinking_budget = 8192
    model.output_dir = tmp_path / "chapter"
    model.key_file = tmp_path / "NO_KEY_READ"
    model.timeout_seconds = 1
    model.ledger = None
    model.counter = lambda *_args, **_kwargs: 32
    planner = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id="f4-owner", pool_path=tmp_path / "POOL.jsonl", plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "chapter", planning_revision_enabled=revision, chapter_workers=1), planner=model)
    args = dict(chapters=[{"chapter_id": "CH01", "title": "Change", "source_ids": ["paper-A"]}], shared_outline={}, topic="What changed?", candidates={"paper-A": candidate}, level1_tools={}, level2_tools={}, state={}, candidate_pool=[candidate])
    return planner, calls, args


@pytest.mark.parametrize("revision", [False, True])
def test_assigned_conflicting_card_excluded_before_initial_messages(tmp_path, monkeypatch, revision):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    dump(Path(row["card_path"]), {"paper_identity": {"paper_id": "paper-B", "doi": "10.synthetic/b"}, "general_understanding": {"finding": "CONTRADICTORY_B"}, "review_planning": {"planning_summary": "CONTRADICTORY_B"}})
    planner, calls, args = chapters(tmp_path, row, monkeypatch, revision=revision)
    result = planner._chapter_details(**args, resume=False)[0]
    assert "CONTRADICTORY_B" not in json.dumps(calls[0]["messages"])
    assert result["source_materials"][0]["material_identity_conflict"] is True
    assert not result["source_materials"][0]["study_summary_A"]
    assert not result["source_materials"][0]["review_planning_B"]


def test_refresh_removes_stale_conflicting_card_only_and_recovers(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    source = {**p.build_local_material_payload(row), "card_path": row["card_path"], "deep_read_material": {"question_material": [{"explanation": "VALID_DEEP"}]}, "supplement_gap_material": {"usable_content": "VALID_SUPPLEMENT"}}
    dump(Path(row["card_path"]), {"paper_identity": {"paper_id": "paper-B"}, "general_understanding": {"finding": "WRONG"}, "review_planning": {"planning_summary": "WRONG"}})
    result = p._refresh_local_material_snapshots([{"source_materials": [source]}])[0]["source_materials"][0]
    assert not result.get("study_summary_A") and not result.get("review_planning_B")
    assert result["deep_read_material"] == source["deep_read_material"]
    assert result["supplement_gap_material"] == source["supplement_gap_material"]
    dump(Path(row["card_path"]), {"general_understanding": {"finding": "CORRECTED"}, "review_planning": {"planning_summary": "CORRECTED"}})
    restored = p._refresh_local_material_snapshots([{"source_materials": [result]}])[0]["source_materials"][0]
    assert not restored.get("material_identity_conflict")
    assert restored["study_summary_A"]["finding"] == "CORRECTED"


def test_legacy_card_without_identity_still_reaches_messages(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    dump(Path(row["card_path"]), {"general_understanding": {"finding": "LEGACY_USABLE"}, "review_planning": {"planning_summary": "LEGACY_USABLE"}})
    planner, calls, args = chapters(tmp_path, row, monkeypatch)
    planner._chapter_details(**args, resume=False)
    planner._chapter_details(**args, resume=True)
    assert len(calls) == 1
    assert "LEGACY_USABLE" in json.dumps(calls[0]["messages"])


def test_prior_loader_rejects_explicit_doi_conflict(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    prior = {"paper_id": "paper-A", "paper_identity": {"title": row["title"], "doi": "10.synthetic/b"}, "_progressive_task_signature": p._directed_task_signature(task()), "question_material": [{"question_id": "Q01", "explanation": "WRONG_IDENTITY"}]}
    path = tmp_path / "prior.json"
    dump(path, prior)
    assert p.load_prior_readings([path], [row]) == []


@pytest.mark.parametrize("change", ["body", "references"])
def test_actual_reader_cache_reopens_only_changed_content(tmp_path, monkeypatch, change):
    config, row, snapshot, context, calls, runner = fixture(tmp_path, monkeypatch)
    read = runner()
    first = read([task()], **context)
    old = Path(first["results"][0]["output_dir"])
    old_bytes = (old / "DIRECTED_READING.json").read_bytes()
    if change == "body":
        (snapshot / "READING_VIEW.md").write_text("## Abstract\nREVISED: sample decreased from 4 to 2 at 10 K.\n")
    else:
        dump(snapshot / "REFERENCES.json", {"references": [{"marker": "[1]", "text": "Changed citation identity"}]})
    second = runner(first["materials"])([task()], **context)
    third = runner(second["materials"])([task()], **context)
    assert len(calls) == 2, (first, second, third)
    assert second["results"][0]["status"] == "fulfilled"
    assert third["results"][0]["status"] == "reused_prior_deep_read"
    assert first["results"][0]["task_id"] != second["results"][0]["task_id"]
    assert (old / "DIRECTED_READING.json").read_bytes() == old_bytes
    summary = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1).summary(config.topic_id)
    assert summary["core_count"] == 1 and summary["task_count"] == 2
    assert second["materials"][0]["current_question_material"][0]["explanation"] == "REVISED_ANSWER"
    assert "ORIGINAL_ANSWER" not in json.dumps(p._compact_reading_material(second["materials"][0]))
    assert "ORIGINAL_ANSWER" in old_bytes.decode()
    planner, owner_calls, args = chapters(tmp_path, row, monkeypatch)
    args["chapter_tools"] = {"directed_results": [{"materials": second["materials"]}]}
    planner._chapter_details(**args, resume=False)
    assert "REVISED_ANSWER" in json.dumps(owner_calls[0]["messages"])
    assert "ORIGINAL_ANSWER" not in json.dumps(owner_calls[0]["messages"])
    if change == "body":
        assert "REVISED:" in json.dumps(calls[1])


def test_actual_reader_cache_content_identical_move_and_snapshot_rename(tmp_path, monkeypatch):
    _, row, snapshot, context, calls, runner = fixture(tmp_path, monkeypatch)
    first = runner()([task()], **context)
    moved = snapshot.parent / "snapshot-moved"
    shutil.copytree(snapshot, moved)
    dump(moved / "manifest.json", {"snapshot_id": "renamed-snapshot"})
    card = json.loads(Path(row["card_path"]).read_text())
    card["material"]["snapshot_id"] = "renamed-snapshot"
    dump(Path(row["card_path"]), card)
    dump(Path(row["card_path"]).parent / "SOURCE_UNIT.json", {"snapshot_path": str(moved)})
    # No explicit prior: this exercises the actual SQLite/reader cache too.
    second = runner()([task()], **context)
    third = runner(first["materials"])([task()], **context)
    assert len(calls) == 1
    assert second["results"][0]["reused"] is True
    assert third["results"][0]["status"] == "reused_prior_deep_read"


def test_legacy_prompt_proves_reuse_but_cannot_hide_current_change(tmp_path, monkeypatch):
    _, row, snapshot, context, calls, runner = fixture(tmp_path, monkeypatch)
    first = runner()([task()], **context)
    old = Path(first["results"][0]["output_dir"])
    legacy = {k: v for k, v in first["materials"][0].items() if k not in {"source_hash", "source_provenance", "paper_identity"}}
    dump(old / "EXPORTED_PRIOR.json", legacy)
    prior = p.load_prior_readings([old / "EXPORTED_PRIOR.json"], [row])
    second = runner(prior)([task()], **context)
    assert second["results"][0]["status"] == "reused_prior_deep_read" and len(calls) == 1
    (snapshot / "READING_VIEW.md").write_text("## Abstract\nREVISED: actual content changed.\n")
    third = runner(prior)([task()], **context)
    assert third["results"][0]["status"] == "fulfilled" and len(calls) == 2


def test_review_derived_original_remains_usable_without_own_assets(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    row.pop("card_path")
    row["root_review_note"] = {"paper_id": "review-R", "usable_content": "Original A improved the outcome under condition C"}
    row["supplement_gap_material"] = {"usable_content": "Original A improved the outcome under condition C", "reporting_review_id": "review-R"}
    planner, calls, args = chapters(tmp_path, row, monkeypatch)
    packet = planner._chapter_details(**args, resume=False)[0]
    assert "condition C" in json.dumps(calls[0]["messages"])
    assert packet["source_materials"][0]["paper_id"] == "paper-A"


def test_actual_adaptive_journal_reopens_changed_source(tmp_path, monkeypatch):
    config, row, snapshot, _, calls, runner = fixture(tmp_path, monkeypatch)
    read = runner()
    collect = p.make_retrieval_loop_runner(config, allow_external=False, directed_reader=read)
    args = dict(phase="level1", directed_requests=[task()], pool_rows=[row], plan={"research_question": "What changed?"}, prior_tool_results={}, prior_directed={}, source_handle_map={"P0001": "paper-A"}, resume=True, output_dir=tmp_path / "adaptive")
    first = collect(**args)
    cached = collect(**args)
    assert len(calls) == 1
    (snapshot / "READING_VIEW.md").write_text("## Abstract\nREVISED: measured outcome decreased.\n")
    changed = collect(**args)
    assert len(calls) == 2, (first, cached, changed)
    assert "REVISED_ANSWER" in json.dumps(changed)
    assert len({first["retrieval_loop"]["needs"][0]["need_id"], changed["retrieval_loop"]["needs"][0]["need_id"]}) == 2


def test_wrong_card_does_not_disqualify_compatible_independent_read(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    dump(Path(row["card_path"]), {"paper_identity": {"paper_id": "paper-B"}, "general_understanding": {"finding": "WRONG_CARD"}})
    material = p.build_local_material_payload(row, deep_material={"paper_id": "paper-A", "question_material": [{"explanation": "VALID_DEEP"}]})
    assert p._owner_material_has_content(material)
    assert "WRONG_CARD" not in json.dumps(material)
    assert material["material_identity_conflicts"][0]["channel"] == "saved_card"
    refreshed = p._refresh_local_material_snapshots([{"source_materials": [{**material, "card_path": row["card_path"]}]}])[0]["source_materials"][0]
    assert p._owner_material_has_content(refreshed)


def test_corrected_sparse_card_cannot_revive_flagged_old_content(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    dump(Path(row["card_path"]), {"paper_identity": {"paper_id": "paper-A"}})
    source = {"paper_id": "paper-A", "card_path": row["card_path"], "material_identity_conflict": True, "study_summary_A": {"finding": "OLD_WRONG"}, "review_planning_B": {"planning_summary": "OLD_WRONG"}}
    result = p._refresh_local_material_snapshots([{"source_materials": [source]}])[0]["source_materials"][0]
    assert "OLD_WRONG" not in json.dumps(result)
    assert not result.get("material_identity_conflict")


def test_prior_without_current_snapshot_keeps_history_without_exemption(tmp_path, monkeypatch):
    _, row, snapshot, context, calls, runner = fixture(tmp_path, monkeypatch)
    first = runner()([task()], **context)
    shutil.rmtree(snapshot)
    second = runner(first["materials"])([task()], **context)
    assert len(calls) == 1
    assert second["results"][0]["reason"] == "existing_snapshot_not_found"
    assert second["results"][0]["status"] == "partial"
    assert second["materials"][0]["question_material"][0]["explanation"] == "ORIGINAL_ANSWER"
    assert second["materials"][0]["current_question_material"] == []
    assert not p._directed_material_compatible(task(), second["materials"][0])


def test_explicit_foreign_snapshot_is_blocked_before_reader(tmp_path, monkeypatch):
    _, row, snapshot, context, calls, runner = fixture(tmp_path, monkeypatch)
    dump(snapshot / "manifest.json", {"snapshot_id": "snapshot-original", "paper_identity": {"paper_id": "paper-B", "doi": "10.synthetic/b"}})
    result = runner()([task()], **context)
    assert calls == []
    assert result["results"][0]["reason"] == "source_identity_conflict"


def test_foreign_deep_material_is_not_borrowed_into_correct_card(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    material = p.build_local_material_payload(row, deep_material={"paper_id": "paper-B", "question_material": [{"explanation": "FOREIGN_READ"}]})
    assert "FOREIGN_READ" not in json.dumps(material)
    assert material["study_summary_A"]["finding"] == "A_FINDING"


def test_actual_legacy_store_prompt_reuses_without_migrating_old_artifact(tmp_path, monkeypatch):
    config, row, _, context, calls, runner = fixture(tmp_path, monkeypatch)
    first = runner()([task()], **context)
    original = Path(first["results"][0]["output_dir"])
    store = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1)
    current = store.task(first["results"][0]["task_id"])
    legacy_task = store.add_task(review_id=config.topic_id, paper_id="paper-A", questions=current["questions"], required_outputs=current["required_outputs"], gap_keys=current["gap_keys"], source_hash="")
    old = tmp_path / "legacy"
    old_artifact = json.loads((original / "DIRECTED_READING.json").read_text())
    old_artifact.pop("source_hash", None)
    old_artifact.pop("paper_identity", None)
    old_artifact["task_id"] = legacy_task["task_id"]
    old_artifact["task_hash"] = legacy_task["task_hash"]
    dump(old / "DIRECTED_READING.json", old_artifact)
    shutil.copyfile(original / "PROMPT.json", old / "PROMPT.json")
    store.commit_reading(review_id=config.topic_id, task_id=legacy_task["task_id"], output_dir=str(old), source_hash="", gap_keys=current["gap_keys"])
    before = (old / "DIRECTED_READING.json").read_bytes()
    second = runner()([task()], **context)
    assert len(calls) == 1
    assert second["results"][0]["output_dir"] == str(old)
    assert second["results"][0]["reused"] is True
    assert second["materials"][0]["source_hash"]
    assert (old / "DIRECTED_READING.json").read_bytes() == before


def test_review_derived_prior_keeps_account_without_own_snapshot_or_false_exemption(tmp_path, monkeypatch):
    _, row, _, context, calls, runner = fixture(tmp_path, monkeypatch)
    row.pop("card_path")
    row["root_review_note"] = {"paper_id": "review-R", "usable_content": "Attributed account"}
    prior = {"paper_id": "paper-A", "paper_identity": {"doi": "10.synthetic/a"}, "_progressive_task_signature": p._directed_task_signature(task()), "question_material": [{"question_id": "Q01", "explanation": "REVIEW_ORIGINAL_ACCOUNT"}]}
    result = runner([prior])([task()], **context)
    assert result["results"][0]["status"] == "review_reported_no_reacquire"
    assert result["blocked_paper_ids"] == ["paper-A"]
    assert "REVIEW_ORIGINAL_ACCOUNT" in json.dumps(result["materials"])
    assert not p._directed_material_compatible(task(), result["materials"][0])
    assert calls == []


def test_legacy_cycle_reads_only_nominated_snapshots(tmp_path, monkeypatch):
    config, row, _, _, calls, runner = fixture(tmp_path, monkeypatch)
    unrelated = copy.deepcopy(row)
    unrelated.update(_paper_id="paper-unrelated", card_path=str(tmp_path / "must-not-open.json"))
    dump(Path(unrelated["card_path"]), {"paper_identity": {"paper_id": "paper-unrelated"}})
    original_read = Path.read_text
    def observed_read(path, *args, **kwargs):
        assert path != Path(unrelated["card_path"]), "unrelated card/snapshot opened"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", observed_read)
    planner = p.ProgressiveReviewPlanner(config, planner=lambda *_: {}, directed_reader=runner())
    result = planner._tool_cycle(phase="level1", supplement_requests=[], directed_requests=[task()], pool_rows=[row, unrelated], plan={"research_question": "What changed?"}, prior_directed={}, prior_tool_results={}, source_handle_map={}, resume=True, state={})
    assert len(calls) == 1
    assert result["directed_results"][0]["materials"]


def test_raw_historical_artifact_and_saved_input_reuse_in_fresh_store(tmp_path, monkeypatch):
    config, row, _, context, calls, runner = fixture(tmp_path, monkeypatch)
    first = runner()([task()], **context)
    old = Path(first["results"][0]["output_dir"])
    prior = p.load_prior_readings([old / "DIRECTED_READING.json"], [row])
    fresh_config = p.ProgressivePlannerConfig(topic_id="fresh-review", pool_path=config.pool_path, plan_path=config.plan_path, output_dir=tmp_path / "fresh-run")
    fresh_reader = p.make_directed_reading_runner(fresh_config, key_file=tmp_path / "never-open-key", budget_ledger_path=tmp_path / "never-create-ledger", budget_limit_cny=1, prior_readings=prior)
    result = fresh_reader([task()], **context)
    assert len(calls) == 1
    assert result["results"][0]["status"] == "reused_prior_deep_read"


def test_foreign_deep_cannot_clear_bad_card_quarantine(tmp_path, monkeypatch):
    _, row, _, _, _, _ = fixture(tmp_path, monkeypatch)
    dump(Path(row["card_path"]), {"paper_identity": {"paper_id": "paper-B"}})
    source = {"paper_id": "paper-A", "card_path": row["card_path"], "study_summary_A": {"finding": "WRONG_CARD"}, "deep_read_material": {"paper_id": "paper-B", "question_material": [{"explanation": "WRONG_DEEP"}]}}
    material = p._refresh_local_material_snapshots([{"source_materials": [source]}])[0]["source_materials"][0]
    assert material["material_identity_conflict"]
    assert not p._owner_material_has_content(material)
    assert "WRONG_DEEP" not in json.dumps(material)
