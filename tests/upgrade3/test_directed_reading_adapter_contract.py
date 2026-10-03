"""WO02: real adapter/store/reader; substitute only model transport."""
import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import directed_reading as dr
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, _directed_task_requirements, _directed_material_compatible,
    _decorate_directed_material, make_directed_reading_runner,
)


def fixture(tmp_path, monkeypatch, answers):
    snapshot = tmp_path / "source" / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "READING_VIEW.md").write_text("## Abstract\nSynthetic sample increased from 2 to 4 at 10 K.\n")
    card = tmp_path / "source" / "PAPER_READING_CARD.json"
    card.write_text(json.dumps({"paper_identity": {"title": "Synthetic paper"}, "material": {"material_scope": "fulltext"}}))
    (card.parent / "SOURCE_UNIT.json").write_text(json.dumps({"snapshot_path": str(snapshot)}))
    dummy_key = tmp_path / "synthetic-placeholder.txt"
    dummy_key.write_text("not-a-real-credential")
    calls = []
    class BoundaryClient:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, **kwargs):
            calls.append({"messages": messages, **kwargs})
            return {"content": json.dumps(answers[len(calls) - 1]), "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(dr, "QwenDirectClient", BoundaryClient)
    config = ProgressivePlannerConfig(topic_id="synthetic-review", pool_path=tmp_path / "POOL.jsonl", plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "run", shared_deep_read_budget=1, reader_workers=1)
    runner = make_directed_reading_runner(config, key_file=dummy_key, budget_ledger_path=tmp_path / "ledger.json", budget_limit_cny=30)
    context = {"output_dir": tmp_path / "phase", "pool_by_id": {"paper-1": {"_paper_id": "paper-1", "title": "Synthetic paper", "card_path": str(card)}}}
    return runner, context, calls, config


def task(question="What changed?", output="Report change"):
    return {"paper_id": "paper-1", "questions": [{"question_id": "Q01", "question": question, "purpose": "Compare conditions", "required_output_ids": ["O01"]}], "required_outputs": [{"output_id": "O01", "description": output}]}


def answer(text="Sample increased from 2 to 4", remaining=""):
    return {"question_material": [{"question_id": "Q01", "explanation": text, "remaining_points": remaining}]}


def test_real_adapter_new_tasks_reuse_and_unique_paper_budget(tmp_path, monkeypatch):
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(), answer("At 10 K"), answer("Change was 2 units")])
    first = runner([task()], **context)
    second = runner([task(question="Which conditions?")], **context)
    third = runner([task(output="Give absolute difference")], **context)
    reused = runner([task()], **context)
    assert len(calls) == 3, (first, second, third, reused)
    rows = [result["results"][0] for result in (first, second, third)]
    assert all(row["status"] == "fulfilled" for row in rows)
    assert len({row["task_id"] for row in rows}) == 3
    assert len({row["output_dir"] for row in rows}) == 3
    assert reused["results"][0]["reused"] is True
    assert reused["results"][0]["task_id"] == rows[0]["task_id"]
    summary = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1).summary(config.topic_id)
    assert summary["core_count"] == 1
    assert set(first["consumed_paper_ids"] + second["consumed_paper_ids"] + third["consumed_paper_ids"]) == {"paper-1"}


def test_string_question_has_executable_purpose_and_outputs(tmp_path, monkeypatch):
    runner, context, calls, _ = fixture(tmp_path, monkeypatch, [answer()])
    request = {"paper_id": "paper-1", "questions": ["What changed?"]}
    normalized = _directed_task_requirements(request)
    assert normalized["questions"][0]["purpose"]
    result = runner([request], **context)
    assert len(calls) == 1, result
    assert result["results"][0]["status"] == "fulfilled"


def test_empty_result_explicit_retry_preserves_old_raw_and_result(tmp_path, monkeypatch):
    runner, context, calls, _ = fixture(tmp_path, monkeypatch, [answer(""), answer()])
    empty = runner([task()], **context)
    assert empty["results"][0]["status"] == "unmet"
    old = Path(empty["results"][0]["output_dir"])
    old_bytes = {name: (old / name).read_bytes() for name in ("RAW_RESPONSE.json", "DIRECTED_READING.json")}
    cached = runner([task()], **context)
    assert len(calls) == 1
    assert cached["results"][0]["status"] == "unmet"
    retry = runner([{**task(), "retry_empty_result": True}], **context)
    assert len(calls) == 2
    assert retry["results"][0]["status"] == "fulfilled"
    assert Path(retry["results"][0]["output_dir"]) != old
    assert retry["results"][0]["task_id"] == empty["results"][0]["task_id"]
    for name, data in old_bytes.items():
        assert (old / name).read_bytes() == data


def test_useful_partial_is_retained_but_not_fulfilled(tmp_path, monkeypatch):
    runner, context, calls, _ = fixture(tmp_path, monkeypatch, [answer(remaining="No temperature comparison")])
    partial = runner([task()], **context)
    assert len(calls) == 1
    assert partial["status"] == "partial"
    assert partial["results"][0]["status"] == "partial"
    material = partial["materials"][0]
    assert material["question_material"][0]["explanation"]
    assert not _directed_material_compatible(task(), material)


def test_reader_aggregate_marks_provider_failure_without_changing_attempted_quota(tmp_path, monkeypatch):
    runner, context, _, _ = fixture(tmp_path, monkeypatch, [answer()])

    def fail_reader(**kwargs):
        raise RuntimeError("injected provider failure")

    monkeypatch.setattr(dr, "run_directed_reading", fail_reader)
    result = runner([task()], **context)

    assert result["status"] == "failed"
    assert result["provider_failed"] is True
    assert result["failed_paper_ids"] == ["paper-1"]
    assert result["consumed_paper_ids"] == result["attempted_paper_ids"] == ["paper-1"]
    assert result["results"][0]["status"] == "failed"


def test_reader_aggregate_keeps_useful_partial_and_exposes_failed_rows(tmp_path, monkeypatch):
    runner, context, _, _ = fixture(tmp_path, monkeypatch, [answer()])
    calls = []

    def mixed_reader(**kwargs):
        calls.append(kwargs["output_dir"])
        if len(calls) == 1:
            return {
                "output": answer("Useful partial", remaining="Need another condition"),
                "output_dir": str(kwargs["output_dir"]),
                "network_call": True,
            }
        raise RuntimeError("injected provider failure")

    monkeypatch.setattr(dr, "run_directed_reading", mixed_reader)
    result = runner([task(), task(question="Which conditions?")], **context)

    assert result["status"] == "partial"
    assert result["provider_failed"] is False
    assert result["failed_paper_ids"] == ["paper-1"]
    assert sorted(row["status"] for row in result["results"]) == ["failed", "partial"]
    assert result["materials"][0]["question_material"][0]["explanation"] == "Useful partial"


def test_question_rows_without_answer_do_not_authorize_reuse():
    material = _decorate_directed_material({"question_material": [{"question_id": "Q01", "question": "What changed?", "explanation": "", "remaining_points": ""}]}, task=task())
    assert not _directed_material_compatible(task(), material)


def test_two_questions_require_two_answers_and_partial_does_not_retry(tmp_path, monkeypatch):
    runner, context, calls, _ = fixture(tmp_path, monkeypatch, [answer()])
    request = task()
    request["questions"].append({"question_id": "Q02", "question": "Was change replicated?", "purpose": "Assess repeatability", "required_output_ids": ["O01"]})
    first = runner([request], **context)
    retained = runner([request], **{**context, "output_dir": tmp_path / "another-phase"})
    assert len(calls) == 1
    assert first["results"][0]["status"] == retained["results"][0]["status"] == "partial"
    assert retained["materials"][0]["question_material"][0]["explanation"]


def test_current_empty_answer_cannot_borrow_prior_history():
    from optomind_research.runtime.upgrade3.progressive_review_plan import _directed_material_status
    prior = _decorate_directed_material(answer(), task=task())
    changed = task(question="Was change replicated?")
    material = _decorate_directed_material(answer(""), task=changed, prior_material=prior)
    assert material["prior_question_material"][0]["explanation"]
    assert _directed_material_status(changed, material) == "unmet"
    assert not _directed_material_compatible(changed, material)


def test_legacy_unkeyed_answer_is_retained_as_partial(tmp_path, monkeypatch):
    runner, context, calls, _ = fixture(tmp_path, monkeypatch, [{"question_material": [{"finding": "Unkeyed useful answer"}]}])
    first = runner([task()], **context)
    retained = runner([task()], **context)
    assert len(calls) == 1
    assert first["results"][0]["status"] == retained["results"][0]["status"] == "partial"
    assert retained["materials"][0]["question_material"] == [{"finding": "Unkeyed useful answer"}]


def test_same_paper_batch_counts_one_independent_paper(tmp_path, monkeypatch):
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(), answer("At 10 K")])
    result = runner([task(), task(question="Which conditions?")], **context)
    assert len(calls) == 2
    assert result["status"] == "fulfilled"
    assert result["consumed_paper_ids"] == result["attempted_paper_ids"] == ["paper-1"]
    assert dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1).summary(config.topic_id)["core_count"] == 1


def test_real_retrieval_closure_keeps_partial_status_and_content(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import make_retrieval_loop_runner
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(remaining="Replication unavailable")])
    retrieval = make_retrieval_loop_runner(config, allow_external=False, directed_reader=runner)
    result = retrieval(phase="test", directed_requests=[{**task(), "chapter_ids": ["C1"]}], pool_rows=list(context["pool_by_id"].values()), resume=False)
    assert len(calls) == 1
    state = result["retrieval_loop"]["needs"][0]
    assert state["status"] != "fulfilled", state
    assert "Sample increased" in state["usable_content"]
    assert state["still_missing"]
    assert result["tool_materials_by_chapter"]["C1"][0]["usable_content"]


def test_real_retrieval_closure_routes_provider_failure_to_same_round_retry(tmp_path):
    from optomind_research.runtime.upgrade3.progressive_review_plan import (
        ProgressivePlannerConfig, make_retrieval_loop_runner,
    )

    config = ProgressivePlannerConfig(
        topic_id="provider-failure-review",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=tmp_path / "run",
        shared_deep_read_budget=1,
        reader_workers=1,
    )
    calls = []

    def failed_reader(tasks, **context):
        calls.append(context["output_dir"])
        return {
            "status": "failed",
            "results": [{"paper_id": "paper-1", "status": "failed", "error": "RuntimeError"}],
            "materials": [],
            "consumed_paper_ids": ["paper-1"],
            "attempted_paper_ids": ["paper-1"],
        }

    retrieval = make_retrieval_loop_runner(config, allow_external=True, directed_reader=failed_reader)
    request = {
        "paper_id": "paper-1",
        "chapter_ids": ["C1"],
        "questions": [{"question_id": "Q01", "question": "What answer is available?", "purpose": "Probe"}],
        "required_outputs": [{"output_id": "O01", "output_type": "practical_material", "description": "Answer"}],
        "knowledge_gap": "provider failure probe",
        "reason": "provider failure probe",
        "round_specs": [
            {"round": index, "targeted_queries": [{"query_type": "keyword", "query_text": f"direction-{index}"}]}
            for index in (1, 2, 3)
        ],
    }
    result = retrieval(
        phase="provider-failure", directed_requests=[request], pool_rows=[],
        plan={"research_question": "Synthetic provider failure"}, resume=False,
    )

    state = result["retrieval_loop"]["needs"][0]
    assert len(calls) == 2
    assert all("round_1" in str(path) for path in calls)
    assert state["status"] == "pending"
    assert state["action"] == "provider_retry"
    assert result["directed_results"]
    assert all(row["status"] == "failed" for row in result["directed_results"])


def test_retrieval_explicit_empty_retry_is_one_attempt_across_rounds(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import make_retrieval_loop_runner
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(""), answer("")])
    first = runner([task()], **context)
    old = Path(first["results"][0]["output_dir"])
    saved_raw = (old / "RAW_RESPONSE.json").read_bytes()
    retrieval = make_retrieval_loop_runner(config, allow_external=False, directed_reader=runner)
    request = {**task(), "retry_empty_result": True, "round_specs": [
        {"round": index, "targeted_queries": [{"query_type": "keyword", "query_text": text}]}
        for index, text in enumerate(("synthetic change", "synthetic conditions", "synthetic replication"), 1)
    ]}
    result = retrieval(phase="retry", directed_requests=[request], pool_rows=list(context["pool_by_id"].values()), resume=False)
    state = result["retrieval_loop"]["needs"][0]
    assert len(state["external_results"]) >= 2, state
    assert len(calls) == 2
    assert (old / "RAW_RESPONSE.json").read_bytes() == saved_raw
    paths = [row["results"][0]["output_dir"] for row in state["external_results"]]
    assert len(set(paths)) == 1
    assert Path(paths[0]) != old


def test_current_plain_prose_survives_retained_question_history():
    from optomind_research.runtime.upgrade3.progressive_review_plan import _directed_material_status
    prior = _decorate_directed_material(answer(), task=task())
    changed = task(question="Was change replicated?")
    material = _decorate_directed_material({"question_material": [], "plain_text": "Replication used three synthetic samples."}, task=changed, prior_material=prior)
    assert _directed_material_status(changed, material) == "partial"
    assert material["plain_text"] == "Replication used three synthetic samples."


def test_real_tool_cycle_preserves_distinct_same_paper_task_bindings(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import ProgressiveReviewPlanner, _directed_task_signature, _merge_directed_tasks
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(), answer("Difference was 2")])
    first = {**task(), "chapter_ids": ["C1"], "knowledge_gaps": ["Need the experimental conditions for this synthetic sample"]}
    equivalent = {**first, "chapter_ids": ["C2"]}
    changed = {**task(output="Report absolute difference"), "chapter_ids": ["C3"]}
    merged = _merge_directed_tasks([first, equivalent, changed])
    assert len(merged) == 2
    assert merged[0]["chapter_ids"] == ["C1", "C2"]
    planner = ProgressiveReviewPlanner(config, planner=lambda *_: {}, directed_reader=runner)
    arguments = dict(phase="test", supplement_requests=[], directed_requests=[first, equivalent, changed], pool_rows=list(context["pool_by_id"].values()), plan={"research_question": "Synthetic study"}, prior_directed=None, prior_tool_results={}, source_handle_map={}, resume=False, state={})
    result = planner._tool_cycle(**arguments)
    assert len(calls) == 2, result
    group = result["directed_results"][0]
    assert group["selected_unique_papers"] == group["requested_unique_papers"] == 1
    assert not group["deferred_tasks"]
    assert len({row["task_id"] for row in group["materials"]}) == 2
    assert {row["_progressive_task_signature"] for row in group["materials"]} == {_directed_task_signature(first), _directed_task_signature(changed)}
    assert all(_directed_material_compatible(request, next(row for row in group["materials"] if row["_progressive_task_signature"] == _directed_task_signature(request))) for request in (first, changed))
    repeated = planner._tool_cycle(**{**arguments, "phase": "next", "prior_directed": result})
    assert len(calls) == 2, repeated
    summary = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1).summary(config.topic_id)
    assert summary["task_count"] == 2
    assert summary["core_count"] == 1


def test_real_retrieval_same_question_changed_outputs_have_distinct_needs(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import make_retrieval_loop_runner
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(), answer("Difference was 2")])
    retrieval = make_retrieval_loop_runner(config, allow_external=False, directed_reader=runner)
    result = retrieval(phase="test", directed_requests=[task(), task(output="Report absolute difference")], pool_rows=list(context["pool_by_id"].values()), resume=False)
    states = result["retrieval_loop"]["needs"]
    assert len(states) == 2
    assert len({row["need_id"] for row in states}) == 2
    assert len(calls) == 2, result
    assert result["consumed_paper_ids"] == ["paper-1"]


def test_gap_provenance_reaches_real_store_task_identity(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import _merge_directed_tasks
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [answer(), answer("Replication used three samples")])
    first = {**task(), "knowledge_gaps": ["Need experimental sample conditions"]}
    second = {**task(), "knowledge_gaps": ["Need independent sample replication"]}
    result = runner(_merge_directed_tasks([first, second]), **context)
    assert len(calls) == 2, result
    store = dr.DirectedReadingStore(config.output_dir / "directed_reading.sqlite", core_cap=1)
    rows = [store.task(row["task_id"]) for row in result["results"]]
    assert len({row["task_id"] for row in rows}) == 2
    assert {tuple(row["gap_keys"]) for row in rows} == {(first["knowledge_gaps"][0],), (second["knowledge_gaps"][0],)}
    runner(_merge_directed_tasks([first]), **context)
    assert len(calls) == 2


def test_real_retrieval_preserves_top_level_prose_with_empty_answer_rows(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.progressive_review_plan import make_retrieval_loop_runner
    payload = {**answer(""), "prose": "Replication used three synthetic samples."}
    runner, context, calls, config = fixture(tmp_path, monkeypatch, [payload])
    retrieval = make_retrieval_loop_runner(config, allow_external=False, directed_reader=runner)
    result = retrieval(phase="test", directed_requests=[task()], pool_rows=list(context["pool_by_id"].values()), resume=False)
    assert len(calls) == 1
    state = result["retrieval_loop"]["needs"][0]
    assert state["status"] == "partial"
    assert "Replication used three synthetic samples" in state["usable_content"]
