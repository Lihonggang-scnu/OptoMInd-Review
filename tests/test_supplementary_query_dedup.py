"""Tests for two-stage query deduplication."""

from __future__ import annotations

from optomind_research.runtime.supplementary_query_dedup import (
    ACTION_KEEP,
    ACTION_MERGE,
    ACTION_REJECT,
    DECISION_AMBIGUOUS,
    DECISION_DUPLICATE,
    DECISION_SAME_TASK_REPLAY,
    DECISION_UNIQUE,
    SOURCE_BATCH,
    SOURCE_HISTORICAL,
    SOURCE_QUEUED,
    KnownQuery,
    QueryCandidate,
    finalize_dedup,
    stage1_deduplicate,
)


def _candidate(
    query_id: str,
    text: str,
    *,
    task_id: str = "task-a",
) -> QueryCandidate:
    return QueryCandidate(
        query_id=query_id,
        text=text,
        source_task_id=task_id,
        batch_id="batch-1",
    )


def _known(
    query_id: str,
    text: str,
    *,
    task_id: str = "task-historical",
    source: str = SOURCE_HISTORICAL,
) -> KnownQuery:
    return KnownQuery(
        query_id=query_id,
        text=text,
        source_task_id=task_id,
        source=source,
    )


def test_exact_normalized_duplicate_is_rejected_across_tasks() -> None:
    candidate = _candidate("q1", "Radiative Cooling Multilayer  Inverse Design")
    result = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    decision = result.decisions[0]
    assert decision.decision == DECISION_DUPLICATE
    assert "exact_normalized_duplicate" in decision.reasons
    assert decision.matched_refs[0].source == SOURCE_HISTORICAL
    assert result.stats["duplicate"] == 1


def test_same_task_historical_replay_is_kept() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design", task_id="task-a")
    result = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design", task_id="task-a")],
    )
    decision = result.decisions[0]
    assert decision.decision == DECISION_SAME_TASK_REPLAY
    assert "same_task_exact_replay" in decision.reasons
    outcome = finalize_dedup(result, adjudicator=None)
    assert [item.query_id for item in outcome.kept_queries] == ["q1"]


def test_same_batch_same_task_normalized_equivalent_collapses_to_one_query() -> None:
    candidates = [
        _candidate("q1", "radiative cooling multilayer inverse design", task_id="task-a"),
        _candidate(
            "q2",
            "Radiative Cooling Multilayer Inverse Design",
            task_id="task-a",
        ),
    ]
    known = [
        _known("q1", candidates[0].text, task_id="task-a", source=SOURCE_BATCH),
        _known("q2", candidates[1].text, task_id="task-a", source=SOURCE_BATCH),
    ]
    result = stage1_deduplicate(candidates, known)
    by_id = {decision.query_id: decision for decision in result.decisions}
    assert by_id["q1"].decision == DECISION_UNIQUE
    assert by_id["q2"].decision == DECISION_DUPLICATE
    assert "same_batch_normalized_duplicate" in by_id["q2"].reasons
    outcome = finalize_dedup(result, adjudicator=None)
    assert [item.query_id for item in outcome.kept_queries] == ["q1"]
    assert [item.query_id for item in outcome.rejected_queries] == ["q2"]


def test_same_batch_cross_task_exact_duplicate_is_rejected() -> None:
    candidates = [
        _candidate("q1", "radiative cooling multilayer inverse design", task_id="task-a"),
        _candidate("q2", "radiative cooling multilayer inverse design", task_id="task-b"),
    ]
    known = [
        _known("q1", candidates[0].text, task_id="task-a", source=SOURCE_BATCH),
        _known("q2", candidates[1].text, task_id="task-b", source=SOURCE_BATCH),
    ]
    result = stage1_deduplicate(candidates, known)
    assert {d.decision for d in result.decisions} == {DECISION_UNIQUE, DECISION_DUPLICATE}


def test_lexical_containment_obvious_duplicate_is_rejected() -> None:
    candidate = _candidate("q1", "multilayer inverse design radiative cooling")
    result = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    decision = result.decisions[0]
    assert decision.decision == DECISION_DUPLICATE
    assert "lexical_obvious_duplicate" in decision.reasons
    assert decision.matched_refs[0].similarity is not None
    assert decision.matched_refs[0].similarity.jaccard == 1.0


def test_ambiguous_query_is_grouped_for_semantic_adjudication() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    result = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    decision = result.decisions[0]
    assert decision.decision == DECISION_AMBIGUOUS
    assert len(result.ambiguous_groups) == 1
    group = result.ambiguous_groups[0]
    assert [item.query_id for item in group.queries] == ["q1"]
    assert [ref.query_id for ref in group.refs] == ["h1"]
    assert result.stats["ambiguous"] == 1


def test_unique_query_passes_stage1() -> None:
    candidate = _candidate("q1", "quantum dot display color conversion")
    result = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    assert result.decisions[0].decision == DECISION_UNIQUE
    assert result.stats["unique"] == 1


def test_historical_queued_and_batch_sources_are_labeled() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    result = stage1_deduplicate(
        [candidate],
        [
            _known("h1", "radiative cooling multilayer inverse design", source=SOURCE_HISTORICAL),
            _known("h2", "multilayer radiative cooling inverse design methods", source=SOURCE_QUEUED),
            _known(
                "h3",
                "inverse design radiative cooling multilayer optimization methods",
                source=SOURCE_BATCH,
            ),
        ],
    )
    decision = result.decisions[0]
    assert decision.decision == DECISION_AMBIGUOUS
    assert {ref.source for ref in decision.matched_refs} == {
        SOURCE_HISTORICAL,
        SOURCE_QUEUED,
        SOURCE_BATCH,
    }


def test_no_adjudicator_keeps_ambiguous_with_semantic_review_flag() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    stage1 = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    outcome = finalize_dedup(stage1, adjudicator=None)
    decision = outcome.decisions[0]
    assert decision.decision == ACTION_KEEP
    assert decision.needs_semantic_review is True
    assert "no_adjudicator_conservative_keep" in decision.reasons
    assert outcome.adjudicator_calls == 0
    assert [item.query_id for item in outcome.kept_queries] == ["q1"]
    assert outcome.rejected_queries == ()


def test_adjudicator_called_once_with_all_groups_and_can_reject() -> None:
    candidates = [
        _candidate("q1", "radiative cooling multilayer inverse design optimization"),
        _candidate("q2", "thermal management multilayer inverse design radiative cooling"),
    ]
    known = [
        _known("h1", "radiative cooling multilayer inverse design"),
        _known("h2", "thermal management multilayer inverse design"),
    ]
    stage1 = stage1_deduplicate(candidates, known)
    calls: list[int] = []

    def adjudicator(groups):
        calls.append(len(groups))
        return {
            "decisions": [
                {"query_id": "q1", "action": ACTION_REJECT, "reason": "covered by h1"},
                {"query_id": "q2", "action": ACTION_REJECT, "reason": "covered by h2"},
            ]
        }

    outcome = finalize_dedup(stage1, adjudicator=adjudicator)
    assert len(calls) == 1
    assert {d.query_id for d in outcome.rejected_queries} == {"q1", "q2"}
    assert outcome.kept_queries == ()
    assert outcome.adjudicator_calls == 1
    rejected_reasons = {d.reasons for d in outcome.rejected_queries}
    assert any("covered by h1" in reasons for reasons in rejected_reasons)


def test_adjudicator_can_keep_and_merge_preserving_task_ids() -> None:
    candidate_a = _candidate("q1", "radiative cooling multilayer inverse design optimization", task_id="task-a")
    candidate_b = _candidate("q2", "radiative cooling multilayer inverse design methods", task_id="task-b")
    stage1 = stage1_deduplicate(
        [candidate_a, candidate_b],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )
    assert len(stage1.ambiguous_groups) == 1

    def adjudicator(groups):
        return {
            "decisions": [
                {"query_id": "q1", "action": ACTION_KEEP, "reason": "distinct optimization angle"},
                {"query_id": "q2", "action": ACTION_MERGE, "merged_into_query_id": "q1", "reason": "same methods angle"},
            ]
        }

    outcome = finalize_dedup(stage1, adjudicator=adjudicator)
    by_id = {d.query_id: d for d in outcome.decisions}
    assert by_id["q1"].decision == ACTION_KEEP
    assert by_id["q2"].decision == ACTION_MERGE
    assert by_id["q2"].merged_into_query_id == "q1"
    assert "task-b" in by_id["q2"].preserved_task_ids
    assert "task-a" in by_id["q2"].preserved_task_ids
    assert [d.query_id for d in outcome.kept_queries] == ["q1"]
    assert [d.query_id for d in outcome.merged_queries] == ["q2"]


def test_merge_into_historical_query_is_not_executable() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    stage1 = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )

    def adjudicator(groups):
        return {
            "decisions": [
                {
                    "query_id": "q1",
                    "action": ACTION_MERGE,
                    "merged_into_query_id": "h1",
                    "reason": "semantic duplicate of historical query",
                }
            ]
        }

    outcome = finalize_dedup(stage1, adjudicator=adjudicator)
    assert outcome.kept_queries == ()
    assert [d.query_id for d in outcome.merged_queries] == ["q1"]
    assert outcome.merged_queries[0].merged_into_query_id == "h1"
    assert "task-historical" in outcome.merged_queries[0].preserved_task_ids


def test_invalid_adjudicator_output_falls_back_to_conservative_keep() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    stage1 = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )

    def bad_adjudicator(groups):
        return {"decisions": [{"query_id": "unknown", "action": ACTION_KEEP, "reason": "nope"}]}

    outcome = finalize_dedup(stage1, adjudicator=bad_adjudicator)
    decision = outcome.decisions[0]
    assert decision.decision == ACTION_KEEP
    assert decision.needs_semantic_review is True
    assert "adjudicator_invalid_fallback_conservative_keep" in decision.reasons


def test_adjudicator_exception_falls_back_to_conservative_keep() -> None:
    candidate = _candidate("q1", "radiative cooling multilayer inverse design optimization")
    stage1 = stage1_deduplicate(
        [candidate],
        [_known("h1", "radiative cooling multilayer inverse design")],
    )

    def exploding_adjudicator(groups):
        raise RuntimeError("model unavailable")

    outcome = finalize_dedup(stage1, adjudicator=exploding_adjudicator)
    decision = outcome.decisions[0]
    assert decision.decision == ACTION_KEEP
    assert decision.needs_semantic_review is True
    assert any("conservative_keep" in reason for reason in decision.reasons)


def test_finalize_mixed_batch_without_adjudicator() -> None:
    candidates = [
        _candidate("q1", "radiative cooling multilayer inverse design"),
        _candidate("q2", "perovskite light emitting diode external quantum efficiency"),
        _candidate("q3", "radiative cooling multilayer inverse design optimization"),
    ]
    known = [
        _known("h1", "radiative cooling multilayer inverse design"),
        _known("h2", "quantum dot display color conversion"),
    ]
    stage1 = stage1_deduplicate(candidates, known)
    outcome = finalize_dedup(stage1, adjudicator=None)
    assert {d.query_id for d in outcome.rejected_queries} == {"q1"}
    kept_ids = {d.query_id for d in outcome.kept_queries}
    assert {"q2", "q3"} <= kept_ids
    assert outcome.adjudicator_calls == 0
    q3 = next(d for d in outcome.decisions if d.query_id == "q3")
    assert q3.needs_semantic_review is True
