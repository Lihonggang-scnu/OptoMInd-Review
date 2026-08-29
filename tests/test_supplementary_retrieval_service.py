"""Tests for the resumable idempotent single-writer retrieval task service."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

import optomind_research.runtime.supplementary_retrieval_service as service_module
from optomind_research.runtime.supplementary_retrieval_contract import (
    DEFAULT_PORTFOLIO_LIMITS,
    GAP_TYPES,
    ContextRegistry,
    SupplementaryRetrievalTask,
)
from optomind_research.runtime.supplementary_retrieval_service import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_MATERIALIZING,
    STATUS_NO_PROGRESS,
    STATUS_QUEUED,
    STATUS_RUNNING,
    MaterializationOutcome,
    RetrievalOutcome,
    ROUTE_LITERATURE,
    ROUTE_VISUAL,
    ServiceCallbacks,
    SupplementaryRetrievalService,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest's default creates ACL-blocked dirs)."""
    base = Path(tempfile.gettempdir()) / "optomind-supplementary-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _registry(
    *,
    historical_queries: list | None = None,
    concurrent_queries: list | None = None,
) -> ContextRegistry:
    registry = ContextRegistry()
    registry.set("user_question", "How do radiative cooling multilayers compare?")
    registry.set(
        "dynamic_axes",
        [{"axis_id": "Q01", "description": "multilayer mechanism"}],
    )
    registry.set(
        "section_task",
        {"section_id": "S01", "title": "Mechanism", "task": "Explain physics."},
    )
    registry.set(
        "target_claim_or_sentence",
        {"claim_id": "C1", "statement": "Cooling power exceeds 60 W/m2."},
    )
    registry.set("argument_role", "mechanism_explanation")
    registry.set(
        "bound_papers_and_quotes",
        [{"paper_id": "p1", "quote": "emissivity 0.95"}],
    )
    registry.set("reviewer_feedback", {"mentor": "needs direct measurement support"})
    registry.set("author_revision_history", [{"revision": 1, "outcome": "still_open"}])
    registry.set("missing_fact_units", ["cooling_power_measured"])
    registry.set(
        "required_material_strength",
        {"minimum": "factual_support", "abstract_ceiling": "background_only"},
    )
    registry.set("retrieval_success_criteria", ["has_measured_cooling_power"])
    registry.set("existing_paper_identities", ["doi:10.1/example"])
    registry.set("historical_queries", historical_queries or [])
    registry.set("concurrent_queries", concurrent_queries or [])
    registry.set(
        "current_review_structure",
        {
            "existing_sections": [{"section_id": "S01"}],
            "new_sections": [],
            "new_subsections_per_existing_section": {},
        },
    )
    registry.set(
        "paper_introduction_conclusion_excerpts",
        {
            "current_paper_introduction_excerpt": "Radiative cooling is emerging.",
            "current_paper_conclusion_excerpt": "Fabrication challenges remain.",
        },
    )
    registry.set(
        "whole_review_feedback",
        {"section_count": 8, "uncovered_roles": ["boundary"]},
    )
    registry.set(
        "visual_slots",
        [{"slot_id": "V01", "role": "mechanism_anchor", "section_id": "S01"}],
    )
    registry.set("visual_gaps", ["mechanism_anchor_figure_missing"])
    registry.set("topic_scope", {"topic": "radiative cooling multilayers"})
    registry.set(
        "materialization_policy",
        {
            "priority": ["s2_structured_body", "public_oa_fulltext", "abstract_claim"],
            "abstract_background_only": True,
        },
    )
    registry.set("portfolio_limits", dict(DEFAULT_PORTFOLIO_LIMITS))
    return registry.freeze()


def _task(
    gap_type: str = "claim_evidence_gap",
    *,
    task_id: str = "task-1",
    queries: tuple[str, ...] = ("radiative cooling multilayer inverse design",),
    **kwargs,
) -> SupplementaryRetrievalTask:
    from optomind_research.runtime.supplementary_retrieval_contract import (
        GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    )

    kwargs.setdefault(
        "source_provenance", {"producer": "test", "stage": "unit"}
    )
    kwargs.setdefault("history_refs", ())
    kwargs.setdefault("success_criteria", ("has_adequate_evidence",))
    kwargs.setdefault("material_requirements", ("s2_structured_body",))
    kwargs.setdefault("retrieval_queries", queries)
    kwargs.setdefault("visual_route", gap_type == "visual_material_gap")
    return SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type],
        priority=1,
        **kwargs,
    )


def _make_callbacks(
    holder: dict | None = None,
    meta_log: list | None = None,
) -> tuple[ServiceCallbacks, dict]:
    state = holder if isinstance(holder, dict) else {}
    for key, default in {
        "fail_retrieve": False,
        "fail_materialize": False,
        "adequate": True,
        "total": 10,
        "background": 2,
    }.items():
        state.setdefault(key, default)
    calls = {
        "retrieve": 0,
        "materialize": 0,
        "visual_retrieve": 0,
        "visual_materialize": 0,
    }
    meta_log = meta_log if meta_log is not None else []

    def make_retrieve(name: str, route: str):
        def retrieve(task, queries, context, execution_meta):
            calls[name] += 1
            meta_log.append({"callback": name, "execution_meta": dict(execution_meta)})
            state.setdefault("retrieved_texts", []).extend(
                str(q.get("text", "")) for q in queries
            )
            if state["fail_retrieve"]:
                raise RuntimeError("retrieve failure")
            return RetrievalOutcome(
                candidates=[{"query": q.get("text", "")} for q in queries],
                adequate=True,
                route=route,
            )

        return retrieve

    def make_materialize(name: str):
        def materialize(task, retrieval, context, execution_meta):
            calls[name] += 1
            meta_log.append({"callback": name, "execution_meta": dict(execution_meta)})
            if state["fail_materialize"]:
                raise RuntimeError("materialize failure")
            return MaterializationOutcome(
                sources=[{"id": "s1"}],
                adequate=bool(state["adequate"]),
                total_references=int(state["total"]),
                background_only_references=int(state["background"]),
                materialized_route="s2_structured_body",
            )

        return materialize

    callbacks = ServiceCallbacks(
        retrieve=make_retrieve("retrieve", ROUTE_LITERATURE),
        materialize=make_materialize("materialize"),
        visual_retrieve=make_retrieve("visual_retrieve", ROUTE_VISUAL),
        visual_materialize=make_materialize("visual_materialize"),
    )
    return callbacks, calls


def test_five_gap_types_submit_and_commit(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    for gap_type in GAP_TYPES:
        task = _task(
            gap_type,
            task_id=f"task-{gap_type}",
            queries=(f"{gap_type} radiative cooling multilayer inverse design",),
        )
        submission = service.submit(task, registry)
        assert submission.status == STATUS_QUEUED
        result = service.process_once()
        assert result is not None
        assert result.status == STATUS_COMMITTED
        assert result.route == (
            ROUTE_VISUAL if gap_type == "visual_material_gap" else ROUTE_LITERATURE
        )
    assert calls["retrieve"] == 4
    assert calls["visual_retrieve"] == 1
    assert calls["materialize"] == 4
    assert calls["visual_materialize"] == 1
    service.close()


def test_missing_context_field_is_rejected(tmp_path) -> None:
    callbacks, _ = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = ContextRegistry()
    registry.set("topic_scope", {"topic": "x"})
    registry.set("user_question", "q")
    registry.set("dynamic_axes", [])
    registry.set("bound_papers_and_quotes", [])
    registry.set("missing_fact_units", [])
    registry.set("required_material_strength", {})
    registry.set("retrieval_success_criteria", [])
    registry.set("existing_paper_identities", [])
    registry.set("materialization_policy", {"priority": [], "abstract_background_only": True})
    task = _task(task_id="task-missing")
    with pytest.raises(ValueError, match="target_claim_or_sentence"):
        service.submit(task, registry)
    service.close()


def test_committed_replay_performs_no_callbacks_and_returns_reuse_evidence(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-committed")
    first = service.submit(task, registry)
    service.process_pending()
    assert service.get_task("task-committed")["status"] == STATUS_COMMITTED
    before = dict(calls)
    second = service.submit(task, registry)
    assert second.reused is True
    assert second.reuse_reason == "committed_replay"
    assert second.status == STATUS_COMMITTED
    assert second.result is not None
    assert second.attempt_history
    assert calls == before
    replay = service.replay_task(first.idempotency_key)
    assert replay.reused is True
    assert replay.reuse_reason == "committed_replay"
    service.close()


def test_no_progress_replay_performs_no_callbacks(tmp_path) -> None:
    callbacks, calls = _make_callbacks(holder={"adequate": False})
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-no-progress")
    first = service.submit(task, registry)
    result = service.process_once()
    assert result is not None
    assert result.status == STATUS_NO_PROGRESS
    assert result.reason == "no_adequate_material"
    before = dict(calls)
    second = service.submit(task, registry)
    assert second.reused is True
    assert second.reuse_reason == "no_progress_replay"
    assert calls == before
    service.close()


def test_failed_task_requires_explicit_retry_and_preserves_history(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-failed")
    service.submit(task, registry)

    def failing_materialize(task, retrieval, context, execution_meta):
        raise RuntimeError("materialize failure")

    callbacks.materialize = failing_materialize
    result = service.process_once()
    assert result.status == STATUS_FAILED
    assert result.reason == "callback_error"
    before = dict(calls)
    blocked = service.submit(task, registry)
    assert blocked.reused is False
    assert blocked.reuse_reason == "failed_requires_explicit_retry"
    assert calls == before
    retried = service.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    assert retried.reuse_reason == "explicit_retry"

    def good_materialize(task, retrieval, context, execution_meta):
        return MaterializationOutcome(
            sources=[{"id": "s1"}],
            adequate=True,
            total_references=10,
            background_only_references=2,
            materialized_route="s2_structured_body",
        )

    callbacks.materialize = good_materialize
    ok = service.process_once()
    assert ok.status == STATUS_COMMITTED
    history = service.get_history("task-failed")
    attempt_ids = {event["attempt_id"] for event in history if event.get("attempt_id")}
    assert len(attempt_ids) >= 2
    service.close()


def test_crash_recovery_resets_running_and_materializing_tasks(tmp_path) -> None:
    callbacks, _ = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    service.submit(_task(task_id="task-running", queries=("query alpha",)), registry)
    service.submit(
        _task(task_id="task-materializing", queries=("query beta",)),
        registry,
    )
    connection = sqlite3.connect(tmp_path / "svc.sqlite")
    connection.execute(
        "UPDATE supplementary_tasks SET status=? WHERE task_id=?",
        (STATUS_RUNNING, "task-running"),
    )
    connection.execute(
        "UPDATE supplementary_tasks SET status=? WHERE task_id=?",
        (STATUS_MATERIALIZING, "task-materializing"),
    )
    connection.commit()
    connection.close()
    assert service.recover() == 2
    statuses = {row["task_id"]: row["status"] for row in service.list_tasks()}
    assert statuses["task-running"] == STATUS_QUEUED
    assert statuses["task-materializing"] == STATUS_QUEUED
    results = service.process_pending(max_tasks=2)
    assert {result.status for result in results} == {STATUS_COMMITTED}
    service.close()


def test_import_history_queries_participate_in_dedup_and_are_idempotent(tmp_path) -> None:
    query = "radiative cooling multilayer inverse design"
    callbacks, calls = _make_callbacks()
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    imported = service.import_history_queries(
        [
            {"history_id": "h1", "text": query},
            {"history_id": "h2", "text": "thermal management multilayer inverse design"},
        ]
    )
    assert imported["imported"] == 2
    assert imported["skipped"] == 0
    again = service.import_history_queries(
        [
            {"history_id": "h1", "text": query},
            {"history_id": "h2", "text": "thermal management multilayer inverse design"},
        ]
    )
    assert again["imported"] == 0
    assert again["skipped"] == 2
    assert service.history_query_count() == 2
    before = dict(calls)
    submission = service.submit(
        _task(
            task_id="task-history-dup",
            queries=(query,),
            material_requirements=("public_oa_fulltext",),
        ),
        registry,
    )
    assert submission.status == STATUS_NO_PROGRESS
    assert submission.reuse_reason == "all_queries_duplicate"
    assert calls == before
    service.close()

    reload_callbacks, reload_calls = _make_callbacks()
    reloaded = SupplementaryRetrievalService(db, callbacks=reload_callbacks)
    assert reloaded.history_query_count() == 2
    reimport = reloaded.import_history_queries([{"history_id": "h1", "text": query}])
    assert reimport["skipped"] == 1
    new_task = reloaded.submit(
        _task(
            task_id="task-reload-dup",
            queries=(query,),
            material_requirements=("s2_structured_body", "public_oa_fulltext"),
        ),
        registry,
    )
    assert new_task.status == STATUS_NO_PROGRESS
    assert new_task.reuse_reason == "all_queries_duplicate"
    assert reload_calls == {
        "retrieve": 0,
        "materialize": 0,
        "visual_retrieve": 0,
        "visual_materialize": 0,
    }
    reloaded.close()


def test_registry_historical_queries_participate_in_dedup(tmp_path) -> None:
    query = "radiative cooling multilayer inverse design"
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry(historical_queries=[{"query_id": "h1", "text": query}])
    submission = service.submit(
        _task(
            task_id="task-registry-dup",
            queries=(query,),
            material_requirements=("public_oa_fulltext",),
        ),
        registry,
    )
    assert submission.status == STATUS_NO_PROGRESS
    assert submission.reuse_reason == "all_queries_duplicate"
    assert calls["retrieve"] == 0
    service.close()


def test_failed_queries_do_not_poison_unrelated_tasks(tmp_path) -> None:
    query = "radiative cooling multilayer inverse design"
    holder = {"fail_retrieve": True}
    callbacks, calls = _make_callbacks(holder)
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task_a = _task(task_id="task-fail-a", queries=(query,))
    service.submit(task_a, registry)
    failed = service.process_once()
    assert failed.status == STATUS_FAILED
    assert calls["retrieve"] == 1

    holder["fail_retrieve"] = False
    task_b = _task(
        task_id="task-b",
        queries=(query,),
        material_requirements=("public_oa_fulltext",),
    )
    submission_b = service.submit(task_b, registry)
    assert submission_b.status == STATUS_QUEUED
    committed_b = service.process_once()
    assert committed_b.status == STATUS_COMMITTED
    assert calls["retrieve"] == 2

    retried = service.submit(task_a, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    committed_a = service.process_once()
    assert committed_a.status == STATUS_COMMITTED
    assert len(service.get_history("task-fail-a")) >= 2
    service.close()


def test_same_batch_normalized_equivalent_produces_one_retrieval_query(tmp_path) -> None:
    holder = {}
    callbacks, calls = _make_callbacks(holder)
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(
        task_id="task-collapse",
        queries=(
            "Radiative Cooling Multilayer Inverse Design",
            "radiative cooling multilayer inverse design",
        ),
    )
    submission = service.submit(task, registry)
    assert submission.status == STATUS_QUEUED
    service.process_once()
    assert calls["retrieve"] == 1
    assert holder["retrieved_texts"] == ["Radiative Cooling Multilayer Inverse Design"]
    row = service.get_task("task-collapse")
    assert len(row["queries"]) == 1
    assert row["queries"][0]["text"] == "Radiative Cooling Multilayer Inverse Design"
    service.close()


def test_historical_merge_records_reuse_without_duplicate_callback(tmp_path) -> None:
    seed_query = "radiative cooling multilayer inverse design"
    candidate_query = "radiative cooling multilayer inverse design optimization"
    adjudicator_calls: list[int] = []

    def adjudicator(groups):
        adjudicator_calls.append(len(groups))
        query = groups[0].queries[0]
        target = groups[0].refs[0].query_id
        return {
            "decisions": [
                {
                    "query_id": query.query_id,
                    "action": "merge",
                    "merged_into_query_id": target,
                    "reason": "semantic duplicate of historical query",
                }
            ]
        }

    callbacks, calls = _make_callbacks()
    callbacks.adjudicator = adjudicator
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    service.submit(_task(task_id="task-seed", queries=(seed_query,)), registry)
    service.process_once()
    before = dict(calls)
    submission = service.submit(
        _task(
            task_id="task-merge",
            queries=(candidate_query,),
            material_requirements=("public_oa_fulltext",),
        ),
        registry,
    )
    assert submission.status == STATUS_NO_PROGRESS
    assert submission.reuse_reason == "all_queries_merged"
    assert calls == before
    assert adjudicator_calls == [1]
    row = service.get_task("task-merge")
    assert len(row["merge_refs"]) == 1
    assert row["merge_refs"][0]["merged_into_text"] == seed_query
    assert "task-seed" in row["merge_refs"][0]["preserved_task_ids"]
    assert "task-merge" in row["merge_refs"][0]["preserved_task_ids"]
    assert row["result"]["merge_refs"] == row["merge_refs"]
    assert row["result"]["dedup_outcome"]["merged_queries"]
    service.close()

    reloaded = SupplementaryRetrievalService(db, callbacks=_make_callbacks()[0])
    row_after_reload = reloaded.get_task("task-merge")
    assert row_after_reload["merge_refs"] == row["merge_refs"]
    assert row_after_reload["dedup_outcome"]["merged_queries"]
    reloaded.close()


def test_same_batch_merge_executes_one_canonical_query(tmp_path) -> None:
    query_a = "radiative cooling multilayer inverse design"
    query_b = "radiative cooling multilayer inverse design optimization methods"
    adjudicator_calls: list[int] = []

    def adjudicator(groups):
        adjudicator_calls.append(len(groups))
        canonical_id = next(
            q.query_id
            for group in groups
            for q in group.queries
            if q.text == query_a
        )
        decisions = []
        for group in groups:
            for q in group.queries:
                if q.query_id == canonical_id:
                    decisions.append(
                        {"query_id": q.query_id, "action": "keep", "reason": "canonical"}
                    )
                else:
                    decisions.append(
                        {
                            "query_id": q.query_id,
                            "action": "merge",
                            "merged_into_query_id": canonical_id,
                            "reason": "merge into canonical",
                        }
                    )
        return {"decisions": decisions}

    holder = {}
    callbacks, calls = _make_callbacks(holder)
    callbacks.adjudicator = adjudicator
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-batch-merge", queries=(query_a, query_b))
    submission = service.submit(task, registry)
    assert submission.status == STATUS_QUEUED
    result = service.process_once()
    assert result.status == STATUS_COMMITTED
    assert calls["retrieve"] == 1
    assert holder["retrieved_texts"] == [query_a]
    row = service.get_task("task-batch-merge")
    assert len(row["queries"]) == 1
    assert row["queries"][0]["text"] == query_a
    assert "task-batch-merge" in row["queries"][0]["preserved_task_ids"]
    assert len(row["merge_refs"]) == 1
    assert row["merge_refs"][0]["merged_into_query_id"] == row["queries"][0]["query_id"]
    assert row["merge_refs"][0]["target_in_submission"] is True
    assert row["result"]["merge_refs"] == row["merge_refs"]
    assert row["result"]["dedup_outcome"]["merged_queries"]
    service.close()

    reloaded = SupplementaryRetrievalService(db, callbacks=_make_callbacks()[0])
    reloaded_row = reloaded.get_task("task-batch-merge")
    assert reloaded_row["merge_refs"] == row["merge_refs"]
    reloaded.close()


def test_submit_persists_and_unions_local_coverage_metadata(tmp_path) -> None:
    callbacks, _ = _make_callbacks()
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    task = _task(task_id="task-local-meta", queries=())
    submission = service.submit(
        task,
        registry,
        query_records=[
            {
                "query": "Radiative Cooling Multilayer Inverse Design",
                "coverage_ids": ["F1"],
                "reason": "first generation reason",
            },
            {
                "query": "radiative cooling multilayer inverse design",
                "coverage_ids": ["F2", "F1"],
                "generation_reasons": ["second generation reason"],
            },
        ],
    )
    assert submission.status == STATUS_QUEUED
    row = service.get_task("task-local-meta")
    assert len(row["queries"]) == 1
    record = row["queries"][0]
    assert record["text"] == "Radiative Cooling Multilayer Inverse Design"
    assert record["coverage_ids"] == ["F1", "F2"]
    assert record["generation_reasons"] == [
        "first generation reason",
        "second generation reason",
    ]
    service.close()


def test_submit_backward_compatible_string_queries_add_empty_metadata(
    tmp_path,
) -> None:
    callbacks, _ = _make_callbacks()
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    task = _task(task_id="task-string-only")
    submission = service.submit(task, registry)
    assert submission.status == STATUS_QUEUED
    row = service.get_task("task-string-only")
    assert len(row["queries"]) == 1
    assert row["queries"][0]["coverage_ids"] == []
    assert row["queries"][0]["generation_reasons"] == []
    service.close()


def test_metadata_round_trip_to_callbacks_and_row(tmp_path) -> None:
    from optomind_research.runtime.supplementary_retrieval_contract import (
        resolve_expansion_policy,
    )

    captured: dict = {}

    def retrieve(task, queries, context, execution_meta):
        captured["retrieve_task"] = task
        return RetrievalOutcome(
            candidates=[],
            adequate=True,
            route=ROUTE_LITERATURE,
        )

    def materialize(task, retrieval, context, execution_meta):
        captured["materialize_task"] = task
        return MaterializationOutcome(
            sources=[{"id": "s1"}],
            adequate=True,
            total_references=1,
        )

    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite",
        callbacks=ServiceCallbacks(
            retrieve=retrieve,
            materialize=materialize,
        ),
    )
    registry = _registry()
    task = _task(
        task_id="task-metadata-roundtrip",
        metadata={"expansion_policy": {"result_cap": 99}},
    )
    service.submit(task, registry)
    result = service.process_once()
    assert result.status == STATUS_COMMITTED
    assert captured["retrieve_task"].metadata == {
        "expansion_policy": {"result_cap": 99}
    }
    assert captured["materialize_task"].metadata == {
        "expansion_policy": {"result_cap": 99}
    }
    assert resolve_expansion_policy(
        captured["materialize_task"]
    ).result_cap == 99
    row = service.get_task("task-metadata-roundtrip")
    assert row["metadata"] == {"expansion_policy": {"result_cap": 99}}
    service.close()


def test_legacy_db_without_metadata_json_is_migrated_in_place(tmp_path) -> None:
    db = tmp_path / "legacy.sqlite"
    old_ddl = service_module._DDL.replace(
        "    material_requirements_json TEXT NOT NULL,\n"
        "    metadata_json TEXT NOT NULL DEFAULT '{}',\n",
        "    material_requirements_json TEXT NOT NULL,\n",
    )
    connection = sqlite3.connect(db)
    connection.executescript(old_ddl)
    connection.execute(
        """
        INSERT INTO supplementary_tasks(
            idempotency_key, task_id, gap_type, status, priority,
            visual_route, context_refs_json, context_snapshot,
            fingerprint, queries_json, merge_refs_json,
            dedup_outcome_json, source_provenance_json,
            history_refs_json, success_criteria_json,
            material_requirements_json, attempt_count, attempts_json,
            result_json, error, reuse_reason, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "legacy:key",
            "task-legacy",
            "claim_evidence_gap",
            STATUS_COMMITTED,
            0,
            0,
            "[]",
            "{}",
            "fingerprint:legacy",
            "[]",
            "[]",
            "{}",
            '{"producer":"legacy"}',
            "[]",
            '["has_adequate_evidence"]',
            '["s2_structured_body"]',
            0,
            "[]",
            '{"legacy":true}',
            None,
            None,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    connection.commit()
    connection.close()

    service = SupplementaryRetrievalService(db)
    columns = {
        str(row[1])
        for row in service._conn.execute(
            "PRAGMA table_info(supplementary_tasks)"
        ).fetchall()
    }
    assert "metadata_json" in columns
    legacy_row = service.get_task_by_idempotency("legacy:key")
    assert legacy_row is not None
    assert legacy_row["metadata"] == {}
    assert legacy_row["status"] == STATUS_COMMITTED
    assert legacy_row["result"] == {"legacy": True}
    assert legacy_row["task_id"] == "task-legacy"
    service.close()


def test_changed_expansion_policy_changes_idempotency_and_keeps_replay(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    base_task = _task(task_id="task-policy-idempotency")
    service.submit(base_task, registry)
    assert service.process_once().status == STATUS_COMMITTED
    assert calls["retrieve"] == 1

    # Identical task replays without callbacks.
    replay = service.submit(base_task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "committed_replay"
    assert calls["retrieve"] == 1

    # A different expansion policy is a different identity: new queued task.
    overridden = _task(
        task_id="task-policy-idempotency",
        metadata={"expansion_policy": {"result_cap": 99}},
    )
    changed = service.submit(overridden, registry)
    assert changed.status == STATUS_QUEUED
    assert changed.reused is False
    assert service.process_once().status == STATUS_COMMITTED
    assert calls["retrieve"] == 2
    service.close()


def test_same_batch_merge_transfers_coverage_metadata(tmp_path) -> None:
    query_a = "radiative cooling multilayer inverse design"
    query_b = "radiative cooling multilayer inverse design optimization methods"

    def adjudicator(groups):
        canonical_id = next(
            q.query_id
            for group in groups
            for q in group.queries
            if q.text == query_a
        )
        decisions = []
        for group in groups:
            for q in group.queries:
                if q.query_id == canonical_id:
                    decisions.append(
                        {
                            "query_id": q.query_id,
                            "action": "keep",
                            "reason": "canonical",
                        }
                    )
                else:
                    decisions.append(
                        {
                            "query_id": q.query_id,
                            "action": "merge",
                            "merged_into_query_id": canonical_id,
                            "reason": "merge into canonical",
                        }
                    )
        return {"decisions": decisions}

    callbacks, _ = _make_callbacks()
    callbacks.adjudicator = adjudicator
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    task = _task(task_id="task-meta-merge", queries=())
    submission = service.submit(
        task,
        registry,
        query_records=[
            {
                "query": query_a,
                "coverage_ids": ["F1"],
                "reason": "reason a",
            },
            {
                "query": query_b,
                "coverage_ids": ["F2", "F3"],
                "reason": "reason b",
            },
        ],
    )
    assert submission.status == STATUS_QUEUED
    row = service.get_task("task-meta-merge")
    assert len(row["queries"]) == 1
    record = row["queries"][0]
    assert record["text"] == query_a
    assert record["coverage_ids"] == ["F1", "F2", "F3"]
    assert record["generation_reasons"] == ["reason a", "reason b"]
    merge_ref = row["merge_refs"][0]
    assert merge_ref["coverage_ids"] == ["F2", "F3"]
    assert merge_ref["generation_reasons"] == ["reason b"]
    service.close()


def test_historical_merge_preserves_coverage_metadata_in_merge_refs(
    tmp_path,
) -> None:
    seed_query = "radiative cooling multilayer inverse design"
    candidate_query = "radiative cooling multilayer inverse design optimization"

    def adjudicator(groups):
        query = groups[0].queries[0]
        target = groups[0].refs[0].query_id
        return {
            "decisions": [
                {
                    "query_id": query.query_id,
                    "action": "merge",
                    "merged_into_query_id": target,
                    "reason": "semantic duplicate of historical query",
                }
            ]
        }

    callbacks, _ = _make_callbacks()
    callbacks.adjudicator = adjudicator
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    service.submit(_task(task_id="task-seed", queries=(seed_query,)), registry)
    service.process_once()
    submission = service.submit(
        _task(task_id="task-meta-hist", queries=()),
        registry,
        query_records=[
            {
                "query": candidate_query,
                "coverage_ids": ["F9"],
                "reason": "historical merge reason",
            }
        ],
    )
    assert submission.status == STATUS_NO_PROGRESS
    row = service.get_task("task-meta-hist")
    assert row["queries"] == []
    merge_ref = row["merge_refs"][0]
    assert merge_ref["coverage_ids"] == ["F9"]
    assert merge_ref["generation_reasons"] == ["historical merge reason"]
    service.close()


def test_stable_execution_metadata_across_retry_and_crash_recovery(tmp_path) -> None:
    holder = {"fail_materialize": True}
    meta_log: list[dict] = []
    callbacks, _ = _make_callbacks(holder, meta_log)
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-meta")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED

    # Simulate a crash while the retried attempt was materializing.
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE supplementary_tasks SET status=? WHERE task_id=?",
        (STATUS_MATERIALIZING, "task-meta"),
    )
    connection.commit()
    connection.close()
    assert service.recover() == 1
    holder["fail_materialize"] = False
    second = service.process_once()
    assert second.status == STATUS_COMMITTED

    retrieve_meta = [m for m in meta_log if m["callback"] == "retrieve"]
    materialize_meta = [m for m in meta_log if m["callback"] == "materialize"]
    # The valid retrieval checkpoint is reused after crash recovery: retrieval
    # runs exactly once, while materialization is retried on the new attempt.
    assert len(retrieve_meta) == 1
    assert len(materialize_meta) == 2
    required_keys = {
        "schema_version",
        "idempotency_key",
        "task_fingerprint",
        "task_id",
        "attempt_id",
        "route",
        "gap_type",
    }
    for entry in meta_log:
        assert required_keys <= set(entry["execution_meta"])
    stable_keys = ("idempotency_key", "task_fingerprint", "route")
    for left, right in (
        (materialize_meta[0]["execution_meta"], materialize_meta[1]["execution_meta"]),
    ):
        for key in stable_keys:
            assert left[key] == right[key]
    assert materialize_meta[0]["execution_meta"]["attempt_id"] != (
        materialize_meta[1]["execution_meta"]["attempt_id"]
    )
    events = [
        str(item.get("event")) for item in service.get_history("task-meta")
    ]
    assert "checkpoint_created" in events
    assert "checkpoint_reused" in events
    service.close()


def test_materialize_failure_then_explicit_retry_resumes_without_reread(
    tmp_path,
) -> None:
    holder = {"fail_materialize": True}
    meta_log: list[dict] = []
    callbacks, calls = _make_callbacks(holder, meta_log)
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-checkpoint-retry")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED
    assert calls["retrieve"] == 1
    assert calls["materialize"] == 1
    row = service.get_task("task-checkpoint-retry")
    assert row["result"]["kind"] == "retrieval_checkpoint"
    assert any(
        item.get("event") == "checkpoint_created"
        for item in row["attempts"]
    )

    holder["fail_materialize"] = False
    retried = service.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    second = service.process_once()
    assert second.status == STATUS_COMMITTED
    # Retrieval is not rerun; only the unfinished materialization is retried.
    assert calls["retrieve"] == 1
    assert calls["materialize"] == 2
    row = service.get_task("task-checkpoint-retry")
    assert row["result"].get("kind") != "retrieval_checkpoint"
    events = [str(item.get("event")) for item in row["attempts"]]
    assert "checkpoint_reused" in events
    service.close()


def test_checkpoint_resume_does_not_require_retrieval_callback(
    tmp_path,
) -> None:
    holder = {"fail_materialize": True}
    callbacks, _ = _make_callbacks(holder)
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-cp-no-retriever")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED
    service.close()

    materialize_calls: list[dict] = []

    def materialize(task, retrieval, context, execution_meta):
        materialize_calls.append(dict(execution_meta))
        return MaterializationOutcome(
            sources=[{"id": "s1"}],
            adequate=True,
            total_references=1,
        )

    # Resume with materialization wired but no retrieval callback at all.
    resume = SupplementaryRetrievalService(
        db,
        callbacks=ServiceCallbacks(materialize=materialize),
    )
    retried = resume.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    result = resume.process_once()
    assert result.status == STATUS_COMMITTED
    assert result.reason == "committed"
    assert "missing_retrieval_callback" not in result.error
    assert len(materialize_calls) == 1
    events = [
        str(item.get("event"))
        for item in resume.get_history("task-cp-no-retriever")
    ]
    assert "checkpoint_reused" in events
    resume.close()


def test_checkpoint_nested_route_mismatch_is_cleared_and_reruns_retrieval(
    tmp_path,
) -> None:
    holder = {"fail_materialize": True}
    callbacks, calls = _make_callbacks(holder)
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-nested-route-mismatch")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED
    assert calls["retrieve"] == 1

    # Outer identity matches, but the nested retrieval.route is for a
    # different route; the checkpoint is stale and must be cleared.
    connection = sqlite3.connect(db)
    row = connection.execute(
        "SELECT result_json FROM supplementary_tasks WHERE task_id=?",
        ("task-nested-route-mismatch",),
    ).fetchone()
    payload = json.loads(row[0])
    payload["retrieval"]["route"] = ROUTE_VISUAL
    connection.execute(
        "UPDATE supplementary_tasks SET result_json=? WHERE task_id=?",
        (json.dumps(payload), "task-nested-route-mismatch"),
    )
    connection.commit()
    connection.close()

    holder["fail_materialize"] = False
    retried = service.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    second = service.process_once()
    assert second.status == STATUS_COMMITTED
    assert calls["retrieve"] == 2
    row = service.get_task("task-nested-route-mismatch")
    assert any(
        item.get("event") == "checkpoint_cleared"
        for item in row["attempts"]
    )
    service.close()


def test_stale_checkpoint_is_cleared_and_retrieval_reruns(tmp_path) -> None:
    holder = {"fail_materialize": True}
    callbacks, calls = _make_callbacks(holder)
    db = tmp_path / "svc.sqlite"
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-stale-checkpoint")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED
    assert calls["retrieve"] == 1

    # Corrupt the durable checkpoint identity so it can never be resumed.
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE supplementary_tasks SET result_json=? WHERE task_id=?",
        (
            '{"kind":"retrieval_checkpoint","schema_version":'
            '"supplementary_retrieval.retrieval_checkpoint.v1",'
            '"idempotency_key":"WRONG","task_fingerprint":"WRONG",'
            '"route":"literature","retrieval":{}}',
            "task-stale-checkpoint",
        ),
    )
    connection.commit()
    connection.close()

    holder["fail_materialize"] = False
    retried = service.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    second = service.process_once()
    assert second.status == STATUS_COMMITTED
    assert calls["retrieve"] == 2
    assert calls["materialize"] == 2
    row = service.get_task("task-stale-checkpoint")
    assert any(
        item.get("event") == "checkpoint_cleared"
        for item in row["attempts"]
    )
    service.close()


def test_success_replaces_checkpoint_with_final_result(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    task = _task(task_id="task-success-checkpoint")
    service.submit(task, registry)
    result = service.process_once()
    assert result.status == STATUS_COMMITTED
    assert calls["retrieve"] == 1
    assert calls["materialize"] == 1
    row = service.get_task("task-success-checkpoint")
    assert row["result"].get("kind") != "retrieval_checkpoint"
    assert "retrieval" in row["result"]
    assert any(
        item.get("event") == "checkpoint_created"
        for item in row["attempts"]
    )
    service.close()


def test_retrieval_failure_before_checkpoint_reruns_retrieval_on_retry(
    tmp_path,
) -> None:
    holder = {"fail_retrieve": True}
    callbacks, calls = _make_callbacks(holder)
    service = SupplementaryRetrievalService(
        tmp_path / "svc.sqlite", callbacks=callbacks
    )
    registry = _registry()
    task = _task(task_id="task-checkpoint-retrieve-fail")
    service.submit(task, registry)
    first = service.process_once()
    assert first.status == STATUS_FAILED
    assert calls["retrieve"] == 1
    row = service.get_task("task-checkpoint-retrieve-fail")
    assert row["result"] is None
    assert not any(
        item.get("event") == "checkpoint_created"
        for item in row["attempts"]
    )

    holder["fail_retrieve"] = False
    retried = service.submit(task, registry, allow_retry=True)
    assert retried.status == STATUS_QUEUED
    second = service.process_once()
    assert second.status == STATUS_COMMITTED
    assert calls["retrieve"] == 2
    assert calls["materialize"] == 1
    service.close()


def test_callbacks_can_read_db_without_open_transaction(tmp_path) -> None:
    db = tmp_path / "svc.sqlite"
    observed: dict[str, str] = {}

    def retrieve(task, queries, context, execution_meta):
        connection = sqlite3.connect(db)
        row = connection.execute(
            "SELECT status FROM supplementary_tasks WHERE idempotency_key=?",
            (execution_meta["idempotency_key"],),
        ).fetchone()
        observed["status"] = str(row[0])
        connection.close()
        return RetrievalOutcome(candidates=[], adequate=True, route=ROUTE_LITERATURE)

    callbacks = ServiceCallbacks(
        retrieve=retrieve,
        materialize=lambda task, retrieval, context, meta: MaterializationOutcome(
            sources=[{"id": "s1"}], adequate=True, total_references=1
        ),
    )
    service = SupplementaryRetrievalService(db, callbacks=callbacks)
    registry = _registry()
    service.submit(_task(task_id="task-lock"), registry)
    result = service.process_once()
    assert result.status == STATUS_COMMITTED
    assert observed["status"] == STATUS_RUNNING
    service.close()


def test_visual_route_uses_distinct_callbacks(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    visual = _task("visual_material_gap", task_id="task-visual")
    service.submit(visual, registry)
    result = service.process_once()
    assert result.route == ROUTE_VISUAL
    assert calls["visual_retrieve"] == 1
    assert calls["visual_materialize"] == 1
    assert calls["retrieve"] == 0
    assert calls["materialize"] == 0
    service.close()


def test_material_library_commit_accepts_more_than_200_references(tmp_path) -> None:
    holder = {"total": 201, "background": 60}
    callbacks, _ = _make_callbacks(holder)
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-library-201")
    service.submit(task, registry)
    result = service.process_once()
    assert result.status == STATUS_COMMITTED
    assert result.result["materialization"]["total_references"] == 201
    assert result.result["materialization"]["background_only_references"] == 60
    service.close()

    holder2 = {"total": 100, "background": 60}
    callbacks2, _ = _make_callbacks(holder2)
    service2 = SupplementaryRetrievalService(
        tmp_path / "svc2.sqlite", callbacks=callbacks2
    )
    task2 = _task(task_id="task-library-bg")
    service2.submit(task2, registry)
    result2 = service2.process_once()
    assert result2.status == STATUS_COMMITTED
    service2.close()


def test_cross_instance_persistence_and_replay(tmp_path) -> None:
    db = tmp_path / "svc.sqlite"
    first_callbacks, _ = _make_callbacks()
    service_one = SupplementaryRetrievalService(db, callbacks=first_callbacks)
    registry = _registry()
    task = _task(task_id="task-cross")
    submission = service_one.submit(task, registry)
    service_one.process_pending()
    service_one.close()

    second_callbacks, second_calls = _make_callbacks()
    service_two = SupplementaryRetrievalService(db, callbacks=second_callbacks)
    assert service_two.get_task("task-cross")["status"] == STATUS_COMMITTED
    replay = service_two.submit(task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "committed_replay"
    assert replay.result is not None
    assert second_calls == {
        "retrieve": 0,
        "materialize": 0,
        "visual_retrieve": 0,
        "visual_materialize": 0,
    }
    assert service_two.replay_task(submission.idempotency_key).reused is True
    service_two.close()


def test_task_without_queries_is_immediate_no_progress(tmp_path) -> None:
    callbacks, calls = _make_callbacks()
    service = SupplementaryRetrievalService(tmp_path / "svc.sqlite", callbacks=callbacks)
    registry = _registry()
    task = _task(task_id="task-no-queries", queries=())
    submission = service.submit(task, registry)
    assert submission.status == STATUS_NO_PROGRESS
    assert submission.reuse_reason == "no_queries"
    assert calls == {
        "retrieve": 0,
        "materialize": 0,
        "visual_retrieve": 0,
        "visual_materialize": 0,
    }
    replay = service.submit(task, registry)
    assert replay.reused is True
    assert replay.reuse_reason == "no_progress_replay"
    service.close()
