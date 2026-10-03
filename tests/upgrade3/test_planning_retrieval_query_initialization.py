"""WO03/F06 offline checks: real index, triage, loop and append-only journal.

Only query-model and external-retrieval boundaries use controlled callbacks.
No credentials, live retrieval, full planning run or paper quota policy changes.
"""

import json
from dataclasses import replace

import pytest

from optomind_research.runtime.upgrade3.planning_material_search import PlanningMaterialIndex
from optomind_research.runtime.upgrade3.planning_retrieval_loop import (
    InformationNeed,
    LoopConfig,
    RetrievalJournal,
    merge_similar_needs,
    needs_from_planner_gap_rows,
    run_retrieval_loop,
)


QUESTION = "Which material conditions determine thermal conductivity?"
QUERY = {"query_type": "keyword", "query_text": "thermal conductivity material conditions", "facet_id": "F1"}


@pytest.fixture
def config(tmp_path):
    index_path = tmp_path / "material.sqlite"
    with PlanningMaterialIndex(index_path) as index:
        index.commit()
    return LoopConfig(index_path=index_path, journal_path=tmp_path / "retrieval.jsonl", max_rounds=1)


def need(**values):
    return InformationNeed("WO03-F06", QUESTION, owners=("C1",), **values)


def entries(config):
    return [json.loads(line) for line in config.journal_path.read_text(encoding="utf-8").splitlines()]


def test_first_empty_query_initializes_once_and_is_durable_before_retrieval(config):
    calls = {"refine": 0, "external": 0}

    def refine(current_need, round_index, previous, useful, missing):
        calls["refine"] += 1
        assert current_need.question == missing == QUESTION
        assert round_index == 1 and previous == [] and useful == ""
        return {"targeted_queries": [QUERY]}

    def external(**kwargs):
        calls["external"] += 1
        checkpoint = entries(config)[-1]
        assert checkpoint["status"] == "query_ready"
        assert checkpoint["counts_as_round"] is False
        assert checkpoint["query_source"] == "refiner"
        assert checkpoint["queries"] == kwargs["queries"] == [QUERY]
        assert checkpoint["query_spec"]["targeted_queries"] == [QUERY]
        return {"status": "fulfilled", "usable_content": "A measured conductivity at a stated temperature.",
                "new_handles": ["P1"], "consumed_paper_ids": ["paper-1"]}

    first = run_retrieval_loop([need()], config, refine_queries=refine, external_closure=external,
                               prior_consumed_paper_ids=["historical-paper"])
    resumed = run_retrieval_loop([need()], config, refine_queries=refine, external_closure=external,
                                 prior_consumed_paper_ids=["historical-paper"])
    assert calls == {"refine": 1, "external": 1}
    assert first["needs"][0]["status"] == resumed["needs"][0]["status"] == "answered"
    assert first["needs"][0]["query_status"] == resumed["needs"][0]["query_status"] == "ready"
    assert first["needs"][0]["empty_rounds"] == 0
    assert first["consumed_paper_ids"] == resumed["consumed_paper_ids"] == ["historical-paper", "paper-1"]
    assert first["shared_deep_reads_used"] == resumed["shared_deep_reads_used"] == 2


@pytest.mark.parametrize("source", ["round_spec", "gap_query", "empty_round_spec"])
def test_existing_effective_query_is_inherited_without_refiner(config, source):
    row = {"gap_question": QUESTION, "chapter_ids": ["C1"], "targeted_queries": [QUERY]}
    if source == "round_spec":
        row["round_specs"] = [{"round": 1, "targeted_queries": [QUERY]}]
        row.pop("targeted_queries")
    elif source == "empty_round_spec":
        row["round_specs"] = [{"round": 1, "targeted_queries": []}]

    def unexpected_refine(*args):
        pytest.fail("an existing query must not call the model")

    calls = []

    def external(**kwargs):
        calls.append(kwargs["queries"])
        assert entries(config)[-1]["status"] == "query_ready"
        return {"status": "fulfilled", "usable_content": "Measured conductivity with conditions."}

    result = run_retrieval_loop(needs_from_planner_gap_rows([row]), config,
                                refine_queries=unexpected_refine, external_closure=external)
    assert calls == [[QUERY]]
    assert result["needs"][0]["query_source"] == "need"
    assert result["needs"][0]["status"] == "answered"


@pytest.mark.parametrize("failure", ["empty", "exception", "unavailable", "invalid_type"])
def test_initialization_failure_reports_query_not_formed_without_scientific_empty_round(config, failure):
    config.max_rounds = 3
    calls = []

    def refine(*args):
        calls.append(args)
        if failure == "exception":
            raise RuntimeError("controlled model failure")
        if failure == "invalid_type":
            return [{"query_type": "unsupported", "query_text": "invalid"}]
        return []

    def external(**kwargs):
        pytest.fail("retrieval must not run without a formed query")

    result = run_retrieval_loop([need()], config, refine_queries=None if failure == "unavailable" else refine,
                                external_closure=external)
    state = result["needs"][0]
    assert len(calls) == (0 if failure == "unavailable" else 1)
    assert state["status"] == "stopped"
    assert state["query_status"] == "query_not_formed"
    assert state["last_error"]
    assert state["still_missing"] == QUESTION
    assert state["empty_rounds"] == 0
    assert result["external_calls"] == 0
    assert entries(config)[-1]["status"] == "query_not_formed"
    assert entries(config)[-1]["counts_as_round"] is False
    assert RetrievalJournal(config.journal_path).rounds_completed("WO03-F06") == 0
    feedback = result["owner_content"]["WO03-F06"]["C1"]
    assert feedback["query_status"] == "query_not_formed"
    assert feedback["query_error"] == state["last_error"]
    assert "no literature" not in json.dumps(result).lower()


def test_provider_failure_resume_reuses_query_checkpoint(config):
    config.max_provider_retries = 0
    config.max_rounds = 3
    calls = {"refine": 0, "external": 0}

    def refine(*args):
        calls["refine"] += 1
        return [QUERY]

    def external(**kwargs):
        calls["external"] += 1
        assert kwargs["round_index"] == 1 and kwargs["queries"] == [QUERY]
        if calls["external"] == 1:
            raise RuntimeError("controlled retrieval failure")
        return {"status": "fulfilled", "usable_content": "Recovered material."}

    first = run_retrieval_loop([need()], config, refine_queries=refine, external_closure=external)
    assert first["needs"][0]["action"] == "provider_retry"
    second = run_retrieval_loop([need()], config, refine_queries=refine, external_closure=external)
    assert second["needs"][0]["status"] == "answered"
    assert calls == {"refine": 1, "external": 2}
    assert len([row for row in entries(config) if row["status"] == "query_ready"]) == 1


def test_directed_first_read_does_not_require_query(config):
    calls = []

    def external(**kwargs):
        calls.append(kwargs)
        assert kwargs["queries"] == []
        return {"status": "partial", "usable_content": "A useful directed observation.", "still_missing": "Another setting."}

    def refine(*args):
        pytest.fail("directed first reads do not initialize retrieval queries")

    result = run_retrieval_loop([need(kind="directed")], config, refine_queries=refine, external_closure=external)
    assert len(calls) == 1
    assert result["needs"][0]["query_status"] == "not_required"


def test_budget_floor_prevents_initialization_and_preserves_historical_consumption(config):
    def unexpected(*args, **kwargs):
        pytest.fail("budget floor must stop both model and retrieval boundaries")

    result = run_retrieval_loop([need()], config, refine_queries=unexpected, external_closure=unexpected,
                                budget_available_cny=0, prior_consumed_paper_ids=["old-paper"])
    assert entries(config)[-1]["status"] == "stopped_budget"
    assert result["consumed_paper_ids"] == ["old-paper"]
    assert result["shared_deep_reads_remaining"] == config.shared_deep_read_budget - 1


def test_legacy_first_query_failure_can_initialize_on_resume(config):
    current = need()
    RetrievalJournal(config.journal_path).record({
        "need_id": current.need_id, "need_signature": json.dumps(current.to_dict(), sort_keys=True),
        "round": 1, "status": "stopped_no_query", "action": "stop", "queries": [], "empty_rounds": 1,
    })
    calls = []

    def external(**kwargs):
        calls.append(kwargs)
        return {"status": "fulfilled", "usable_content": "Material obtained after initialization."}

    result = run_retrieval_loop([current], config, refine_queries=lambda *args: [QUERY], external_closure=external)
    assert len(calls) == 1
    assert result["needs"][0]["status"] == "answered"
    assert entries(config)[0]["status"] == "stopped_no_query"


def test_owner_change_rebinds_legacy_answer_without_retrieval(config):
    original = need()
    RetrievalJournal(config.journal_path).record({
        "need_id": original.need_id, "need_signature": json.dumps(original.to_dict(), sort_keys=True),
        "round": 1, "status": "answered", "action": "external_research", "queries": [QUERY],
        "usable_content": "Previously fulfilled material.", "still_missing": "", "new_handles": ["P1"],
        "consumed_paper_ids": ["paper-1"],
    })

    def unexpected(*args, **kwargs):
        pytest.fail("owner-only change must reuse compatible answer")

    result = run_retrieval_loop([replace(original, owners=("C2",))], config,
                                refine_queries=unexpected, external_closure=unexpected)
    assert result["needs"][0]["status"] == "answered"
    assert result["owner_content"][original.need_id]["C2"]["usable_content"] == "Previously fulfilled material."
    assert result["paper_owners"]["P1"] == ["C2"]
    assert result["consumed_paper_ids"] == ["paper-1"]


def test_changed_outputs_or_scope_do_not_merge_as_one_answer():
    row = {"gap_question": QUESTION, "chapter_ids": ["C1"], "required_outputs": [{"output_id": "O1", "description": "Conductivity"}]}
    first = needs_from_planner_gap_rows([row])[0]
    second = needs_from_planner_gap_rows([{**row, "chapter_ids": ["C2"], "required_outputs": [*row["required_outputs"], {"output_id": "O2", "description": "Temperature conditions"}]}])[0]
    assert first.success_criteria != second.success_criteria
    assert len(merge_similar_needs([first, second])) == 2
    different_scope = replace(first, user_scope="Different experimental population")
    assert len(merge_similar_needs([first, different_scope])) == 2
    compatible = merge_similar_needs([first, replace(first, owners=("C2",))])
    assert len(compatible) == 1 and compatible[0].owners == ("C1", "C2")


def test_new_material_supplements_partial_content_instead_of_replacing_it(config):
    config.max_rounds = 2
    current = need(round_specs=({"round": 1, "targeted_queries": [QUERY]},
                               {"round": 2, "targeted_queries": [{**QUERY, "query_text": "thermal conductivity temperature settings"}]}))

    def external(**kwargs):
        if kwargs["round_index"] == 1:
            return {"status": "partial", "usable_content": "Existing measurement.", "still_missing": "Temperature conditions."}
        return {"status": "fulfilled", "usable_content": "New temperature conditions."}

    result = run_retrieval_loop([current], config, external_closure=external)
    assert result["needs"][0]["status"] == "answered"
    assert result["needs"][0]["usable_content"] == "Existing measurement.\n\nNew temperature conditions."
    assert result["needs"][0]["still_missing"] == ""


def test_fulfilled_label_and_handles_without_content_are_not_an_answer(config):
    result = run_retrieval_loop([need(round_specs=({"round": 1, "targeted_queries": [QUERY]},))], config,
                                external_closure=lambda **kwargs: {"status": "fulfilled", "new_handles": ["P1"]})
    assert result["needs"][0]["status"] != "answered"
    assert not result["needs"][0]["usable_content"]
    assert result["needs"][0]["still_missing"] == QUESTION


def test_fulfilled_label_with_explicit_remaining_gap_stays_partial(config):
    result = run_retrieval_loop([need(round_specs=({"round": 1, "targeted_queries": [QUERY]},))], config,
                                external_closure=lambda **kwargs: {"status": "fulfilled", "usable_content": "A partial measurement.",
                                                                   "still_missing": "Temperature conditions remain unknown."})
    state = result["needs"][0]
    assert state["status"] != "answered"
    assert state["usable_content"] == "A partial measurement."
    assert state["still_missing"] == "Temperature conditions remain unknown."
    assert entries(config)[-1]["status"] == "partial"


@pytest.mark.parametrize("same_run_retry", [True, False])
def test_failed_attempt_diagnostics_survive_success_and_resume(config, tmp_path, same_run_retry):
    config.max_provider_retries = 1 if same_run_retry else 0
    current = need(round_specs=({"round": 1, "targeted_queries": [QUERY]},))
    calls = []

    def external(**kwargs):
        attempt_dir = tmp_path / f"attempt_{len(calls) + 1:04d}"
        attempt_dir.mkdir()
        raw = {"status": "failed" if not calls else "completed", "output_dir": str(attempt_dir),
               "reason": "controlled provider failure" if not calls else "Recovered material"}
        (attempt_dir / "RESULT.json").write_text(json.dumps(raw), encoding="utf-8")
        calls.append(raw)
        if len(calls) == 1:
            return {"status": "failed", "provider_failed": True, "error": raw["reason"], "raw_result": raw,
                    "usable_content": "Usable measurement before provider failure.", "still_missing": "Temperature conditions.",
                    "consumed_paper_ids": ["failed-paper"]}
        assert kwargs["prior_consumed_paper_ids"] == ["failed-paper", "historical-paper"]
        assert kwargs["remaining_deep_read_budget"] == config.shared_deep_read_budget - 2
        return {"status": "fulfilled", "usable_content": "Recovered material.", "raw_result": raw,
                "consumed_paper_ids": ["successful-paper"]}

    first = run_retrieval_loop([current], config, external_closure=external, prior_consumed_paper_ids=["historical-paper"])
    failed_bytes = (tmp_path / "attempt_0001" / "RESULT.json").read_bytes()
    assert first["needs"][0]["external_results"][0] == calls[0]
    failed_entry = next(row for row in entries(config) if row["status"] == "provider_retry")
    assert failed_entry["external_result"] == calls[0]
    assert failed_entry["counts_as_round"] is False
    assert "external_status" not in failed_entry
    assert failed_entry["usable_content"] == "Usable measurement before provider failure."
    assert failed_entry["still_missing"] == "Temperature conditions."
    assert failed_entry["consumed_paper_ids"] == ["failed-paper"]
    if not same_run_retry:
        assert first["needs"][0]["action"] == "provider_retry"
        assert first["external_calls"] == 0
        assert first["consumed_paper_ids"] == ["failed-paper", "historical-paper"]
        assert RetrievalJournal(config.journal_path).rounds_completed(current.need_id) == 0

    resumed = run_retrieval_loop([current], config, external_closure=external, prior_consumed_paper_ids=["historical-paper"])
    repeated = run_retrieval_loop([current], config, external_closure=external, prior_consumed_paper_ids=["historical-paper"])
    assert len(calls) == 2
    assert resumed["needs"][0]["status"] == repeated["needs"][0]["status"] == "answered"
    assert resumed["needs"][0]["external_results"] == repeated["needs"][0]["external_results"] == calls
    assert resumed["needs"][0]["usable_content"] == "Usable measurement before provider failure.\n\nRecovered material."
    assert resumed["consumed_paper_ids"] == repeated["consumed_paper_ids"] == ["failed-paper", "historical-paper", "successful-paper"]
    assert resumed["shared_deep_reads_used"] == 3
    assert resumed["external_calls"] == 1
    assert RetrievalJournal(config.journal_path).rounds_completed(current.need_id) == 1
    assert all((tmp_path / f"attempt_{index:04d}" / "RESULT.json").is_file() for index in (1, 2))
    assert (tmp_path / "attempt_0001" / "RESULT.json").read_bytes() == failed_bytes


def test_owner_requirement_labels_and_text_format_do_not_split_compatible_needs():
    first = {"gap_question": QUESTION, "chapter_ids": ["C1"], "user_scope": "Composite materials", "required_outputs": [
        {"output_id": "O1", "question_id": "Q1", "description": "Measured conductivity", "output_type": "comparison"}]}
    second = {"gap_question": "Which  material conditions determine thermal conductivity?", "chapter_ids": ["C2"],
              "user_scope": " composite  MATERIALS ", "required_outputs": [
                  {"output_id": "O9", "question_id": "Q9", "description": "Measured conductivity", "output_type": "comparison"}]}
    combined = merge_similar_needs(needs_from_planner_gap_rows([first, second]))
    assert len(combined) == 1
    assert combined[0].owners == ("C1", "C2")
    assert '"output_id"' not in combined[0].success_criteria[0]
    assert '"question_id"' not in combined[0].success_criteria[0]
