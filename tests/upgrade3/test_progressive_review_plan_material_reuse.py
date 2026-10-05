"""Offline regression tests for provider-alias material reuse."""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import directed_reading, planning_retrieval_loop, planning_supplement
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    ProgressiveReviewPlanner,
    _decorate_directed_material,
    _directed_task_signature,
    _resolve_planner_handles,
    make_directed_reading_runner,
    make_planning_supplement_runner,
    make_retrieval_loop_runner,
)


DOI = "10.1038/s41467-022-33116-z"


def _pool_row(tmp_path: Path, *, with_assets: bool = True) -> dict:
    row = {
        "paper_id": "CorpusId:252309032",
        "planning_view": {
            "paper_identity": {
                "canonical_paper_id": "CorpusId:252309032",
                "doi": DOI,
                "title": "Inhibition of UBA6 by inosine augments tumour immunogenicity and responses",
            }
        },
        "_b_summary": {"declared_content_depth": "fulltext"},
    }
    if not with_assets:
        row["card_path"] = str(tmp_path / "missing" / "PAPER_READING_CARD.json")
        return row

    source_unit = tmp_path / "existing" 
    card_path = source_unit / "card" / "PAPER_READING_CARD.json"
    snapshot = source_unit / "materials" / "CorpusId_252309032" / "snapshot-existing"
    card_path.parent.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text(json.dumps({"snapshot_id": "snapshot-existing"}), encoding="utf-8")
    (source_unit / "SOURCE_UNIT.json").write_text(
        json.dumps({"snapshot_path": str(snapshot)}), encoding="utf-8"
    )
    card_path.write_text(
        json.dumps({"material": {"snapshot_id": "snapshot-existing"}}), encoding="utf-8"
    )
    row["card_path"] = str(card_path)
    return row


def _reuse_callback(tmp_path: Path, monkeypatch, pool_row: dict):
    captured = {}
    acquire_calls = []

    def fake_judge(**kwargs):
        return object()

    def fake_supplement(
        request_path, *, output_dir, acquirer_factory, reuse_material, **kwargs
    ):
        record = {
            "canonical_paper_id": "OpenAlex:W4296038319",
            "doi": "https://doi.org/" + DOI,
            "title": "Inhibition of UBA6 by inosine augments tumour immunogenicity and responses",
        }
        reused = reuse_material(record=record, pool_rows=[pool_row])
        captured["reused"] = reused
        if reused is None:
            acquire_calls.append(record)
        return {"status": "fulfilled", "output_dir": str(output_dir)}

    monkeypatch.setattr(planning_supplement, "run_qwen_fulfillment_judge", fake_judge)
    monkeypatch.setattr(planning_supplement, "run_planning_supplement", fake_supplement)
    config = ProgressivePlannerConfig(
        topic_id="reuse-test",
        pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json",
        output_dir=tmp_path / "run",
    )
    runner = make_planning_supplement_runner(
        config,
        key_file=tmp_path / "missing-key.txt",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=50,
    )
    runner(
        [{"gap_id": "G1", "gap_question": "test"}],
        phase="level1",
        output_dir=tmp_path / "phase",
        pool_rows=[pool_row],
        plan={"research_question": "test"},
        skip_local_triage=True,
    )
    return captured["reused"], acquire_calls


def test_unique_doi_alias_reuses_existing_material_without_acquisition(tmp_path, monkeypatch):
    reused, acquire_calls = _reuse_callback(tmp_path, monkeypatch, _pool_row(tmp_path))

    assert reused is not None
    assert reused["snapshot"].name == "snapshot-existing"
    assert reused["card_path"].endswith("PAPER_READING_CARD.json")
    assert acquire_calls == []


def test_unrelated_doi_does_not_false_match(tmp_path, monkeypatch):
    row = _pool_row(tmp_path)
    row["planning_view"]["paper_identity"]["doi"] = "10.9999/unrelated"
    reused, acquire_calls = _reuse_callback(tmp_path, monkeypatch, row)

    assert reused is None
    assert len(acquire_calls) == 1


def test_doi_match_without_assets_falls_back_to_acquisition(tmp_path, monkeypatch):
    reused, acquire_calls = _reuse_callback(
        tmp_path, monkeypatch, _pool_row(tmp_path, with_assets=False)
    )

    assert reused is None
    assert len(acquire_calls) == 1


def _directed_task(*, question="What finding?", output="Reported finding"):
    return {
        "paper_id": "paper-1",
        "questions": [{"question_id": "Q1", "question": question, "purpose": "Use the finding with its conditions."}],
        "required_outputs": [{"output_id": "O1", "output_type": "practical_material", "description": output}],
        "knowledge_gaps": ["Need the paper-specific finding."],
    }


def _tool_cycle_fixture(tmp_path, *, prior_directed=None, reader=None, budget=1):
    calls = []
    snapshot = tmp_path / "source" / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "READING_VIEW.md").write_text("Synthetic unchanged source material")
    card = snapshot.parent / "PAPER_READING_CARD.json"
    card.write_text(json.dumps({"paper_identity": {"paper_id": "paper-1"}}))
    (snapshot.parent / "SOURCE_UNIT.json").write_text(json.dumps({"snapshot_path": str(snapshot)}))
    if prior_directed:
        for group in prior_directed.get("directed_results") or []:
            for material in group.get("materials") or []:
                material.setdefault("source_hash", directed_reading.sha256_value(directed_reading.load_practical_material(snapshot)))

    def fake_reader(tasks, **_kwargs):
        calls.extend(dict(item) for item in tasks)
        if reader is not None:
            return reader(tasks)
        return {
            "status": "complete",
            "materials": [{
                "paper_id": "paper-1",
                "question_material": [{"finding": "new answer", "conditions": "new conditions"}],
            }],
            "consumed_paper_ids": ["paper-1"],
        }

    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="directed-reuse",
            pool_path=tmp_path / "POOL.jsonl",
            plan_path=tmp_path / "PLAN.json",
            output_dir=tmp_path / "run",
            shared_deep_read_budget=budget,
        ),
        planner=lambda _stage, _payload: {},
        directed_reader=fake_reader,
        prior_readings=(),
    )
    result = planner._tool_cycle(
        phase="level1",
        supplement_requests=[],
        directed_requests=[_directed_task()],
        pool_rows=[{"_paper_id": "paper-1", "card_path": str(card)}],
        plan={"research_question": "test"},
        prior_directed=prior_directed,
        prior_tool_results={},
        source_handle_map={},
        resume=False,
        state={},
    )
    return result, calls


def test_exact_directed_task_reuses_without_provider_and_enters_materials(tmp_path):
    task = _directed_task()
    prior = {
        "paper_id": "paper-1",
        "runtime_config": directed_reading.directed_reader_runtime_config(),
        "_progressive_task_signature": _directed_task_signature(task),
        "question_material": [{"question_id": "Q1", "finding": "old answer", "conditions": "old conditions"}],
    }
    result, calls = _tool_cycle_fixture(
        tmp_path,
        prior_directed={"consumed_paper_ids": ["paper-1"], "directed_results": [{"materials": [prior]}]},
    )

    assert calls == []
    directed = result["directed_results"][0]
    assert directed["reused_prior_tasks"][0]["status"] == "reused_prior_deep_read"
    assert directed["materials"][0]["question_material"][0]["finding"] == "old answer"


def test_changed_question_runs_same_paper_at_existing_unique_slot_and_keeps_old_answer(tmp_path):
    task = _directed_task()
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(task),
        "question_material": [{"finding": "old answer"}],
    }
    changed = _directed_task(question="What limitation was reported?")
    calls = []

    def reader(tasks, **_kwargs):
        calls.extend(tasks)
        return {"status": "complete", "materials": [{"paper_id": "paper-1", "question_material": [{"finding": "new limitation"}]}], "consumed_paper_ids": ["paper-1"]}

    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="changed-question",
            pool_path=tmp_path / "POOL.jsonl",
            plan_path=tmp_path / "PLAN.json",
            output_dir=tmp_path / "run",
            shared_deep_read_budget=1,
        ),
        planner=lambda _stage, _payload: {},
        directed_reader=reader,
    )
    result = planner._tool_cycle(
        phase="level1", supplement_requests=[], directed_requests=[changed],
        pool_rows=[{"_paper_id": "paper-1"}], plan={"research_question": "test"},
        prior_directed={"consumed_paper_ids": ["paper-1"], "directed_results": [{"materials": [prior]}]},
        prior_tool_results={}, source_handle_map={}, resume=False, state={},
    )

    assert len(calls) == 1
    material = result["directed_results"][0]["materials"][0]
    assert [row["finding"] for row in material["question_material"]] == ["old answer", "new limitation"]
    assert material["current_question_material"] == [{"finding": "new limitation"}]
    assert material["prior_question_material"] == [{"finding": "old answer"}]


def test_unknown_legacy_reading_is_context_only_and_new_output_preserves_it(tmp_path):
    legacy = {"paper_id": "paper-1", "question_material": [{"finding": "legacy context"}]}
    result, calls = _tool_cycle_fixture(
        tmp_path,
        prior_directed={"consumed_paper_ids": ["paper-1"], "directed_results": [{"materials": [legacy]}]},
        budget=1,
    )

    assert len(calls) == 1
    material = result["directed_results"][0]["materials"][0]
    assert [row["finding"] for row in material["question_material"]] == ["legacy context", "new answer"]
    assert material["current_question_material"] == [{"finding": "new answer", "conditions": "new conditions"}]
    assert material["prior_question_material"] == [{"finding": "legacy context"}]


def test_harmonized_handle_aliases_reach_adaptive_queue_with_context(tmp_path):
    handle_to_id = {
        "P0583": "paper-583",
        "P0578": "paper-578",
        "P0582": "paper-582",
        "P0585": "paper-585",
        "P0478": "paper-478",
        "P0327": "paper-327",
    }
    directed_reads = [
        # An explicit source_handle remains authoritative over the short alias.
        {"source_handle": "P0583", "handle": "P0578", "chapter_ids": ["Ch5", "Ch6"], "reason": "FMT mechanism"},
        {"handle": "P0578", "chapter_ids": ["Ch5"], "reason": "MITRIC context"},
        {"handle": "P0582", "chapter_ids": ["Ch3"], "reason": "UBA6 evidence"},
        {"handle": "P0585", "chapter_ids": ["Ch1", "Ch4"], "reason": "JCOG context"},
        {"handle": "P0478", "chapter_ids": ["Ch1", "Ch7"], "reason": "cross-domain comparison"},
        {"handle": "P0327", "chapter_ids": ["Ch1"], "reason": "negative evidence"},
        {"handle": "P9999", "chapter_ids": ["Ch7"], "reason": "unknown handle"},
    ]
    resolved = _resolve_planner_handles({"directed_reads": directed_reads}, handle_to_id)

    assert [row["paper_id"] for row in resolved["directed_reads"][:6]] == [
        "paper-583", "paper-578", "paper-582", "paper-585", "paper-478", "paper-327",
    ]
    assert resolved["directed_reads"][0]["chapter_ids"] == ["Ch5", "Ch6"]
    assert resolved["directed_reads"][0]["reason"] == "FMT mechanism"
    assert resolved["directed_reads"][-1]["paper_id"] == "P9999"
    assert resolved["directed_reads"][-1]["paper_id"] not in set(handle_to_id.values())

    captured = {}

    def fake_adaptive(**kwargs):
        captured.update(kwargs)
        return {
            "phase": "level2",
            "status": "complete",
            "directed_results": [],
            "supplement_results": [],
            "tool_materials_by_chapter": {},
            "consumed_paper_ids": [],
        }

    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="harmonized-aliases",
            pool_path=tmp_path / "POOL.jsonl",
            plan_path=tmp_path / "PLAN.json",
            output_dir=tmp_path / "run",
        ),
        planner=lambda _stage, _payload: {},
        retrieval_loop_runner=fake_adaptive,
    )
    planner._tool_cycle(
        phase="level2",
        supplement_requests=[],
        directed_requests=resolved["directed_reads"][:6],
        pool_rows=[{"_paper_id": paper_id, "_source_handle": handle} for handle, paper_id in handle_to_id.items()],
        plan={"research_question": "test"},
        prior_directed=None,
        prior_tool_results={},
        source_handle_map=handle_to_id,
        resume=False,
        state={},
    )

    queued = captured["directed_requests"]
    assert [row["paper_id"] for row in queued] == [
        "paper-583", "paper-578", "paper-582", "paper-585", "paper-478", "paper-327",
    ]
    assert queued[0]["chapter_ids"] == ["Ch5", "Ch6"]
    assert queued[0]["reasons"] == ["FMT mechanism"]
    assert queued[4]["chapter_ids"] == ["Ch1", "Ch7"]
    assert all(row["paper_id"] != "P9999" for row in queued)


def test_changed_required_output_runs_same_paper_and_keeps_history(tmp_path):
    original = _directed_task()
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(original),
        "question_material": [{"finding": "old answer"}],
    }
    changed = _directed_task(output="A limitation with conditions")
    calls = []

    def reader(tasks, **_kwargs):
        calls.extend(tasks)
        return {
            "status": "complete",
            "materials": [{"paper_id": "paper-1", "question_material": [{"finding": "new limitation"}]}],
            "consumed_paper_ids": ["paper-1"],
        }

    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="changed-output",
            pool_path=tmp_path / "POOL.jsonl",
            plan_path=tmp_path / "PLAN.json",
            output_dir=tmp_path / "run",
            shared_deep_read_budget=1,
        ),
        planner=lambda _stage, _payload: {},
        directed_reader=reader,
    )
    result = planner._tool_cycle(
        phase="level1", supplement_requests=[], directed_requests=[changed],
        pool_rows=[{"_paper_id": "paper-1"}], plan={"research_question": "test"},
        prior_directed={"consumed_paper_ids": ["paper-1"], "directed_results": [{"materials": [prior]}]},
        prior_tool_results={}, source_handle_map={}, resume=False, state={},
    )

    assert len(calls) == 1
    material = result["directed_results"][0]["materials"][0]
    assert [row["finding"] for row in material["question_material"]] == ["old answer", "new limitation"]


def test_three_changed_tasks_retain_all_history_but_current_rows_are_separate(tmp_path):
    first = _directed_task(question="What finding?")
    second = _directed_task(question="What limitation?")
    third = _directed_task(question="What condition?")
    calls = []

    def reader(tasks, **_kwargs):
        calls.extend(tasks)
        question = tasks[0]["questions"][0]["question"]
        return {
            "status": "complete",
            "materials": [{"paper_id": "paper-1", "question_material": [{"finding": question}]}],
            "consumed_paper_ids": ["paper-1"],
        }

    planner = ProgressiveReviewPlanner(
        ProgressivePlannerConfig(
            topic_id="three-reads",
            pool_path=tmp_path / "POOL.jsonl",
            plan_path=tmp_path / "PLAN.json",
            output_dir=tmp_path / "run",
            shared_deep_read_budget=1,
        ),
        planner=lambda _stage, _payload: {},
        directed_reader=reader,
    )

    def run(task, prior):
        return planner._tool_cycle(
            phase="level1", supplement_requests=[], directed_requests=[task],
            pool_rows=[{"_paper_id": "paper-1"}], plan={"research_question": "test"},
            prior_directed=prior, prior_tool_results={}, source_handle_map={}, resume=False, state={},
        )

    first_result = run(first, None)
    first_material = first_result["directed_results"][0]["materials"][0]
    second_result = run(second, first_result["directed_results"][0])
    second_material = second_result["directed_results"][0]["materials"][0]
    third_result = run(third, second_result["directed_results"][0])
    third_material = third_result["directed_results"][0]["materials"][0]

    assert len(calls) == 3
    assert [row["finding"] for row in first_material["question_material"]] == ["What finding?"]
    assert [row["finding"] for row in second_material["question_material"]] == ["What finding?", "What limitation?"]
    assert [row["finding"] for row in third_material["question_material"]] == ["What finding?", "What limitation?", "What condition?"]
    assert third_material["current_question_material"] == [{"finding": "What condition?"}]
    assert third_material["prior_question_material"] == [
        {"finding": "What finding?"}, {"finding": "What limitation?"},
    ]


def test_nested_decorators_do_not_duplicate_history_or_reclassify_old_rows():
    original = _directed_task()
    changed = _directed_task(question="What limitation?")
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(original),
        "question_material": [{"finding": "old answer"}],
    }
    inner = _decorate_directed_material(
        {"paper_id": "paper-1", "question_material": [{"finding": "new answer"}]},
        task=changed,
        prior_material=prior,
    )
    outer = _decorate_directed_material(inner, task=changed, prior_material=prior)

    assert [row["finding"] for row in outer["question_material"]] == ["old answer", "new answer"]
    assert outer["prior_question_material"] == [{"finding": "old answer"}]
    assert outer["current_question_material"] == [{"finding": "new answer"}]

    blank = _decorate_directed_material(
        {"paper_id": "paper-1", "question_material": []},
        task=changed,
        prior_material=prior,
    )
    assert blank["question_material"] == [{"finding": "old answer"}]
    assert blank["current_question_material"] == []


def _direct_runner(tmp_path, monkeypatch, *, task, prior, provider_result, calls):
    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def admit_candidates(self, *args, **kwargs):
            return [{"paper_id": "paper-1", "admission_status": "approved_core"}]

    def fake_run_directed_reading(**kwargs):
        calls.append(kwargs)
        return provider_result

    monkeypatch.setattr(directed_reading, "DirectedReadingStore", FakeStore)
    monkeypatch.setattr(directed_reading, "run_directed_reading", fake_run_directed_reading)
    config = ProgressivePlannerConfig(
        topic_id="direct-runner-reuse",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=tmp_path / "run",
        shared_deep_read_budget=1,
    )
    pool_row = _pool_row(tmp_path)
    # The adapter fixture must represent the requested paper, rather than
    # disguising a contradictory CorpusId under the dictionary key paper-1.
    pool_row["paper_id"] = "paper-1"
    pool_row["_paper_id"] = "paper-1"
    pool_row["planning_view"]["paper_identity"]["canonical_paper_id"] = "paper-1"
    snapshot = tmp_path / "existing" / "materials" / "CorpusId_252309032" / "snapshot-existing"
    (snapshot / "READING_VIEW.md").write_text("Synthetic unchanged source material")
    if prior is not None:
        prior = {**prior, "source_hash": directed_reading.sha256_value(directed_reading.load_practical_material(snapshot))}
    runner = make_directed_reading_runner(
        config,
        key_file=tmp_path / "missing-key.txt",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=30,
        prior_readings=[prior] if prior is not None else [],
    )
    return runner(
        [task],
        phase="level1",
        output_dir=tmp_path / "phase",
        plan={"research_question": "test"},
        pool_by_id={"paper-1": pool_row},
    )


def test_directed_runner_reuses_exact_useful_material_without_provider(tmp_path, monkeypatch):
    task = _directed_task()
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(task),
        "question_material": [{"question_id": "Q1", "finding": "old answer"}],
    }
    calls = []
    result = _direct_runner(
        tmp_path, monkeypatch, task=task, prior=prior,
        provider_result={"ready": True, "output": {"question_material": [{"finding": "unused"}]}},
        calls=calls,
    )

    assert calls == []
    assert result["results"][0]["status"] == "reused_prior_deep_read"
    assert result["materials"][0]["current_question_material"] == [{"question_id": "Q1", "finding": "old answer"}]


def test_directed_runner_passes_changed_explicit_output_to_provider(tmp_path, monkeypatch):
    original = _directed_task(output="Original requested output")
    changed = _directed_task(output="Changed requested output")
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(original),
        "question_material": [{"question_id": "Q1", "finding": "old answer"}],
    }
    calls = []
    result = _direct_runner(
        tmp_path, monkeypatch, task=changed, prior=prior,
        provider_result={"ready": True, "output": {"question_material": [{"question_id": "Q1", "finding": "new answer"}]}},
        calls=calls,
    )

    assert len(calls) == 1
    provider_task = calls[0]["task"]
    assert provider_task["required_outputs"] == [{
        "output_id": "O1", "output_type": "practical_material",
        "description": "Changed requested output",
    }]
    assert provider_task["questions"][0]["required_output_ids"] == ["O1"]
    assert result["results"][0]["status"] == "fulfilled"
    assert [row["finding"] for row in result["materials"][0]["question_material"]] == ["old answer", "new answer"]


def test_directed_runner_retries_exact_empty_material(tmp_path, monkeypatch):
    task = _directed_task()
    prior = {
        "paper_id": "paper-1",
        "_progressive_task_signature": _directed_task_signature(task),
        "question_material": [{"finding": "old history"}],
        "current_question_material": [],
    }
    calls = []
    result = _direct_runner(
        tmp_path, monkeypatch, task=task, prior=prior,
        provider_result={"ready": True, "output": {"question_material": [], "current_question_material": []}},
        calls=calls,
    )

    assert len(calls) == 1
    assert result["results"][0]["status"] == "unmet"
    material = result["materials"][0]
    assert material["question_material"] == [{"finding": "old history"}]
    assert material["current_question_material"] == []


def test_adaptive_external_closure_does_not_fulfill_from_empty_current_history(tmp_path, monkeypatch):
    task = _directed_task()
    signature = _directed_task_signature(task)
    output_dir = tmp_path / "run"
    output_dir.mkdir(parents=True)
    (output_dir / "DIRECTED_MATERIAL_CACHE.json").write_text(json.dumps({
        "materials": {
            "paper-1": {
                "paper_id": "paper-1",
                "_progressive_task_signature": signature,
                "question_material": [{"finding": "old history"}],
                "current_question_material": [],
            }
        },
        "task_signatures": {"paper-1": signature},
        "consumed_paper_ids": ["paper-1"],
    }), encoding="utf-8")
    reader_calls = []
    captured = {}

    def fake_reader(tasks, **_kwargs):
        reader_calls.extend(tasks)
        return {
            "status": "complete",
            "materials": [{"paper_id": "paper-1", "question_material": [], "current_question_material": []}],
            "consumed_paper_ids": ["paper-1"],
        }

    def fake_loop(needs, _config, *, external_closure, **_kwargs):
        captured["result"] = external_closure(need=needs[0], round_index=1, queries=[], spec={})
        return {"needs": []}

    monkeypatch.setattr(planning_retrieval_loop, "run_retrieval_loop", fake_loop)
    config = ProgressivePlannerConfig(
        topic_id="adaptive-empty-retry",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=output_dir,
        shared_deep_read_budget=1,
    )
    runner = make_retrieval_loop_runner(
        config,
        key_file=tmp_path / "missing-key.txt",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=30,
        allow_external=True,
        directed_reader=fake_reader,
    )
    runner(
        phase="level1", directed_requests=[task], pool_rows=[], plan={"research_question": "test"},
        prior_tool_results={}, prior_directed={}, source_handle_map={}, resume=True,
        output_dir=output_dir / "level1",
    )

    assert len(reader_calls) == 1
    assert captured["result"]["status"] == "unmet"
    assert captured["result"]["usable_content"] == ""



def test_replaying_old_then_partial_task_keeps_history_and_owner_input_stable():
    first_task = _directed_task()
    next_task = _directed_task(question="Which boundary remains unresolved?")
    first_raw = {"paper_id": "paper-1", "question_material": [{"question_id": "Q1", "finding": "original"}]}
    partial_raw = {"paper_id": "paper-1", "question_material": [{"question_id": "Q1", "finding": "partial", "remaining_points": ["boundary"]}]}
    first = _decorate_directed_material(first_raw, task=first_task)
    partial = _decorate_directed_material(partial_raw, task=next_task, prior_material=first)
    # Disk persistence sorts object keys. Revisit the first task while the
    # paper-level cache contains the second, then revisit that partial task.
    restored_first = _decorate_directed_material(json.loads(json.dumps(first_raw, sort_keys=True)),
        task=first_task, prior_material=partial)
    restored_partial = _decorate_directed_material(json.loads(json.dumps(partial_raw, sort_keys=True)),
        task=next_task, prior_material=restored_first)
    assert restored_partial == partial
    assert restored_partial["prior_question_material"] == first_raw["question_material"]
    assert restored_partial["current_question_material"] == partial_raw["question_material"]
    same_task_replay = _decorate_directed_material(partial_raw, task=next_task, prior_material=restored_partial)
    assert same_task_replay == restored_partial
