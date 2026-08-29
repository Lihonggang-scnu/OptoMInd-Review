"""Offline tests for the generic per-section closure orchestrator."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.section_supplementary_orchestrator import (
    PLAN_JSON,
    REPORT_JSON,
    STATE_JSON,
    STATUS_IMPROVED_STOP,
    STATUS_NO_PROGRESS,
    ClosureResumeMismatchError,
    SectionClosureError,
    _base_cache_fingerprint,
    _aggregate_usage,
    _collect_revision_usage,
    _final_snapshot_from_jobs,
    build_section_closure_plan,
    make_qwen_task_worthiness_adjudicator,
    run_section_supplementary_closure,
)
from optomind_research.runtime.supplementary_gap_closure import (
    _task_and_registry_from_spec,
)
from optomind_research.runtime.supplementary_retrieval_contract import (
    DEFAULT_EXPANSION_POLICIES,
)


@pytest.fixture
def tmp_path(request):
    base = Path(tempfile.gettempdir()) / "optomind-section-closure-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _probe() -> dict:
    return {
        "schema_version": "blueprint_quality_probe.v1",
        "probe_timestamp": "2026-08-10T00:00:00+00:00",
        "section_id": "S05",
        "section_title": "Simulation Credibility",
        "research_context": {
            "user_question": "How do electromagnetic simulations gain credibility?",
            "scope_definition": "optical electromagnetic simulation credibility",
            "problem_understanding": "PINN reliability",
            "provisional_section_title": "Simulation Credibility",
            "provisional_argument_role": "body",
            "key_questions": [
                "Which error sources dominate?",
                "How should validation proceed?",
            ],
        },
        "final_claims": [
            {
                "claim_id": "c5.1",
                "statement": "Claim five one statement.",
                "role": "load_bearing",
                "ready_for_write": False,
            },
            {
                "claim_id": "c5.2",
                "statement": "Claim five two statement.",
                "role": "supporting",
                "ready_for_write": False,
            },
            {
                "claim_id": "c5.3",
                "statement": "Claim five three statement.",
                "role": "supporting",
                "ready_for_write": False,
            },
        ],
        "evidence_gap_records": [
            {
                "claim_id": "c5.1",
                "component_id": "c5.1",
                "importance": "high",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit one"],
                "required_evidence": "exact quote",
                "current_evidence_summary": [],
                "follow_up_retrieval_task": {
                    "success_criteria": "exact quote found"
                },
            },
            {
                "claim_id": "c5.2",
                "component_id": "c5.2",
                "importance": "medium",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit two"],
                "required_evidence": "exact quote",
                "current_evidence_summary": [],
            },
            {
                "claim_id": "c5.3",
                "component_id": "c5.3",
                "importance": "high",
                "disposition": "salvageable_by_narrowing",
                "why_current_evidence_fails": "needs narrowing",
                "missing_fact_units": ["unit three"],
                "required_revision_or_qualification": "narrow statement",
                "current_evidence_summary": [],
            },
        ],
    }


def _write_probe(tmp_path: Path, probe: dict | None = None) -> Path:
    path = tmp_path / "probe.json"
    path.write_text(
        json.dumps(probe if probe is not None else _probe(), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_base_cache(tmp_path: Path) -> Path:
    root = tmp_path / "base_cache"
    root.mkdir()
    (root / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": []}), encoding="utf-8"
    )
    (root / "material_vectors.sqlite").write_bytes(b"fake-vectors")
    return root


def _fake_retrieval_wave(
    outcomes: dict[str, str],
    calls: list,
    snapshots: dict[str, str] | None = None,
):
    def callback(context: dict) -> dict:
        claim_id = str(context["claim_id"])
        calls.append(claim_id)
        status = outcomes[claim_id]
        result = {
            "task_id": str(context["task_id"]),
            "claim_id": claim_id,
            "status": status,
            "wave": int(context["wave"]),
            "query_fingerprint": f"q-{claim_id}",
            "detail": f"outcome {status}",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "estimated_cost_cny": 0.0,
            },
        }
        if snapshots and claim_id in snapshots:
            result["snapshot_path"] = snapshots[claim_id]
        if status == STATUS_IMPROVED_STOP:
            result["new_unit_ids"] = [f"unit-{claim_id}"]
            result["per_target_results"] = [{
                "target_id": claim_id,
                "target_type": "claim",
                "residual_reviewer_comments": [
                    f"residual limit for {claim_id}"
                ],
            }]
        else:
            result["per_target_results"] = [{
                "target_id": claim_id,
                "target_type": "claim",
                "next_action": "narrow_or_delete",
            }]
        return result

    return callback


def test_blocker_selection_default_and_include_nonblocking() -> None:
    plan = build_section_closure_plan(probe_report=_probe())
    assert plan["blocking_count"] == 1
    assert plan["nonblocking_count"] == 0
    assert len(plan["specs"]) == 1
    assert plan["specs"][0]["record"]["component_id"] == "c5.1"

    plan2 = build_section_closure_plan(
        probe_report=_probe(), include_nonblocking=True
    )
    assert plan2["blocking_count"] == 1
    assert plan2["nonblocking_count"] == 1
    assert {spec["record"]["component_id"] for spec in plan2["specs"]} == {
        "c5.1",
        "c5.3",
    }

    plan3 = build_section_closure_plan(
        probe_report=_probe(),
        include_nonblocking=True,
        include_medium=True,
    )
    assert {spec["record"]["component_id"] for spec in plan3["specs"]} == {
        "c5.1",
        "c5.2",
        "c5.3",
    }


def test_stable_ids_context_completeness_and_policy() -> None:
    first = build_section_closure_plan(probe_report=_probe())
    second = build_section_closure_plan(probe_report=_probe())
    assert first["task_ids"] == second["task_ids"]
    assert first["probe_fingerprint"] == second["probe_fingerprint"]

    for spec in first["specs"]:
        task, registry = _task_and_registry_from_spec(spec)
        assert task.gap_type == "claim_evidence_gap"
        assert registry.resolve(task.context_refs)
    policy = first["expansion_policy"]
    expected = DEFAULT_EXPANSION_POLICIES["claim_evidence_gap"].to_dict()
    assert policy == expected

    changed = _probe()
    changed["final_claims"][0]["statement"] = "Changed statement."
    changed_plan = build_section_closure_plan(probe_report=changed)
    assert changed_plan["probe_fingerprint"] != first["probe_fingerprint"]
    assert changed_plan["task_ids"] != first["task_ids"]


def test_default_initial_retrieval_isolation_and_override_refusal() -> None:
    plan = build_section_closure_plan(probe_report=_probe())
    policy = plan["expansion_policy"]
    for flag in (
        "allow_role_expansion",
        "allow_batch_enrichment",
        "allow_reference_expansion",
        "allow_citation_expansion",
        "allow_recommendation_expansion",
        "allow_multi_seed_graph",
    ):
        assert policy[flag] is False
    assert policy["graph_seed_cap"] == 0
    assert policy["allow_exact_paper_followup"] is True
    assert policy["allow_oa_fulltext_fallback"] is True

    with pytest.raises(SectionClosureError, match="allow_role_expansion"):
        build_section_closure_plan(
            probe_report=_probe(),
            expansion_policy_overrides={"allow_role_expansion": True},
        )


def test_dry_run_no_external_calls_and_artifacts(tmp_path: Path) -> None:
    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"

    def boom(*_args, **_kwargs):
        raise AssertionError("dry run must not call external executors")

    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        dry_run=True,
        retrieval_wave_callback=boom,
        revision_callback=boom,
        merge_cache_callback=boom,
    )

    assert report["mode"] == "dry_run"
    assert report["selected_task_count"] == 1
    assert (output / PLAN_JSON).is_file()
    assert (output / STATE_JSON).is_file()
    assert (output / REPORT_JSON).is_file()
    state = json.loads((output / STATE_JSON).read_text(encoding="utf-8"))
    assert all(
        task["status"] == "pending" for task in state["tasks"].values()
    )
    assert report["cost"]["query_generation"]["call_count"] == 0
    assert all(
        (task.get("worthiness") or {}).get("decision") == "ambiguous"
        for task in state["tasks"].values()
    )


def test_one_wave_no_progress_routes_revision_and_never_second_search(
    tmp_path: Path,
) -> None:
    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    retrieval_calls: list[str] = []
    revision_calls: list[str] = []
    merge_calls: list[str] = []
    outcomes = {
        "c5.1": STATUS_IMPROVED_STOP,
        "c5.2": STATUS_NO_PROGRESS,
        "c5.3": STATUS_NO_PROGRESS,
    }
    retrieval = _fake_retrieval_wave(outcomes, retrieval_calls)

    def revision(context: dict, outcome: dict):
        revision_calls.append(str(context["claim_id"]))
        return {"action": "narrow_or_delete", "claim_id": context["claim_id"]}

    def merge(context: dict):
        merge_calls.append(str(context["output_dir"]))
        return {"status": "merged", "snapshot_version": "v2"}

    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        base_cache_dir=_write_base_cache(tmp_path),
        include_nonblocking=True,
        include_medium=True,
        dry_run=False,
        retrieval_wave_callback=retrieval,
        revision_callback=revision,
        merge_cache_callback=merge,
    )

    assert sorted(retrieval_calls) == ["c5.1", "c5.2", "c5.3"]
    assert sorted(revision_calls) == ["c5.2", "c5.3"]
    assert len(merge_calls) == 1
    assert report["terminal_counts"][STATUS_IMPROVED_STOP] == 1
    assert report["terminal_counts"][STATUS_NO_PROGRESS] == 2
    by_claim = {
        row["claim_id"]: row for row in report["tasks"]
    }
    assert by_claim["c5.1"]["revalidation_required"] is True
    assert by_claim["c5.2"]["revision_action"] == {
        "action": "narrow_or_delete",
        "claim_id": "c5.2",
    }
    assert by_claim["c5.2"]["retrieval_wave_count"] == 1
    assert report["cache_merge"]["snapshot_version"] == "v2"

    retrieval_calls.clear()
    revision_calls.clear()
    merge_calls.clear()
    resumed = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        base_cache_dir=tmp_path / "base_cache",
        include_nonblocking=True,
        include_medium=True,
        dry_run=False,
        resume=True,
        retrieval_wave_callback=retrieval,
        revision_callback=revision,
        merge_cache_callback=merge,
    )
    assert retrieval_calls == []
    assert revision_calls == []
    assert merge_calls == []
    assert resumed["resumed"] is True
    assert resumed["terminal_counts"][STATUS_NO_PROGRESS] == 2


def test_injected_no_progress_non_delete_revision_becomes_improved_stop(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.gap_closure_downstream import (
        build_gap_closure_writing_context,
        merge_gap_closure_reports,
    )

    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    retrieval_calls: list[str] = []
    revision_calls: list[str] = []
    retrieval = _fake_retrieval_wave(
        {"c5.1": STATUS_NO_PROGRESS}, retrieval_calls
    )

    def revision(context: dict, outcome: dict):
        revision_calls.append(str(context["claim_id"]))
        return {
            "action": "narrow",
            "revised_claim": "Narrowed statement.",
            "residual_reviewer_comments": ["keep limit"],
        }

    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        dry_run=False,
        retrieval_wave_callback=retrieval,
        revision_callback=revision,
    )

    assert retrieval_calls == ["c5.1"]
    assert revision_calls == ["c5.1"]
    assert report["terminal_counts"][STATUS_IMPROVED_STOP] == 1
    task = report["tasks"][0]
    assert task["status"] == STATUS_IMPROVED_STOP
    assert task["per_target_results"][0]["next_action"] == "narrow"
    assert task["per_target_results"][0]["revised_claim"] == (
        "Narrowed statement."
    )
    context = build_gap_closure_writing_context(
        merge_gap_closure_reports(report)
    )
    assert "c5.1" in context["write_with_limits_claim_ids"]
    assert "keep limit" in context["retained_reviewer_comments"].get(
        "c5.1", []
    )


def test_resume_mismatch_refused(tmp_path: Path) -> None:
    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    base_cache = _write_base_cache(tmp_path)
    run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        base_cache_dir=base_cache,
        dry_run=True,
    )

    changed = _probe()
    changed["final_claims"][0]["statement"] = "Changed."
    changed_path = tmp_path / "changed_probe.json"
    changed_path.write_text(
        json.dumps(changed, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ClosureResumeMismatchError, match="probe_fingerprint"):
        run_section_supplementary_closure(
            probe_report=changed_path,
            output_dir=output,
            base_cache_dir=base_cache,
            resume=True,
            dry_run=True,
        )

    with pytest.raises(ClosureResumeMismatchError, match="config_fingerprint"):
        run_section_supplementary_closure(
            probe_report=probe_path,
            output_dir=output,
            base_cache_dir=base_cache,
            resume=True,
            dry_run=True,
            include_nonblocking=True,
        )

    (base_cache / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": [{"unit_id": "u"}]}), encoding="utf-8"
    )
    with pytest.raises(
        ClosureResumeMismatchError, match="base_cache_fingerprint"
    ):
        run_section_supplementary_closure(
            probe_report=probe_path,
            output_dir=output,
            base_cache_dir=base_cache,
            resume=True,
            dry_run=True,
        )


def test_immutable_inputs(tmp_path: Path) -> None:
    probe_path = _write_probe(tmp_path)
    probe_bytes = probe_path.read_bytes()
    base_cache = _write_base_cache(tmp_path)
    cache_bytes = {
        path.name: path.read_bytes()
        for path in base_cache.iterdir()
    }
    output = tmp_path / "out"
    retrieval = _fake_retrieval_wave(
        {"c5.1": STATUS_IMPROVED_STOP}, []
    )

    run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        base_cache_dir=base_cache,
        dry_run=False,
        retrieval_wave_callback=retrieval,
    )

    assert probe_path.read_bytes() == probe_bytes
    for name, content in cache_bytes.items():
        assert (base_cache / name).read_bytes() == content


def test_live_default_binding_requires_base_cache(tmp_path: Path) -> None:
    probe_path = _write_probe(tmp_path)
    with pytest.raises(SectionClosureError, match="--base-cache-dir"):
        run_section_supplementary_closure(
            probe_report=probe_path,
            output_dir=tmp_path / "out",
            dry_run=False,
        )


def test_live_default_binding_uses_production_path(
    tmp_path: Path, monkeypatch
) -> None:
    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    calls: list = []

    def fake_production(**kwargs):
        calls.append(kwargs)
        return (
            {
                "tasks": {
                    "gap-x": {
                        "claim_id": "c5.1",
                        "component_id": "c5.1",
                        "status": STATUS_IMPROVED_STOP,
                        "retrieval_wave_count": 1,
                        "worthiness": {
                            "decision": "retrieval",
                            "reason": "x",
                        },
                        "outcome": {
                            "status": STATUS_IMPROVED_STOP,
                            "new_unit_ids": ["u"],
                            "per_target_results": [],
                        },
                    }
                }
            },
            [],
            [],
            {"tasks": [], "notifications": []},
        )

    monkeypatch.setattr(
        "optomind_research.runtime.section_supplementary_orchestrator."
        "_run_default_live_production",
        fake_production,
    )
    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        base_cache_dir=_write_base_cache(tmp_path),
        dry_run=False,
    )
    assert len(calls) == 1
    assert report["mode"] == "live"


def test_base_cache_fingerprint_ignores_sidecars(tmp_path: Path) -> None:
    base = _write_base_cache(tmp_path)
    before = _base_cache_fingerprint(base)

    (base / "material_vectors.sqlite-wal").write_bytes(b"wal")
    (base / "material_vectors.sqlite-shm").write_bytes(b"shm")
    (base / "tmp-units.json.tmp").write_bytes(b"tmp")
    assert _base_cache_fingerprint(base) == before

    (base / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": [{"unit_id": "u"}]}), encoding="utf-8"
    )
    assert _base_cache_fingerprint(base) != before


def test_blocker_selection_uses_write_gate_blocker_set() -> None:
    probe = _probe()
    probe["write_gate"] = {
        "blueprint_claim_audit": {
            "unadopted_unready_load_bearing_claim_ids": ["c5.2"]
        }
    }
    plan = build_section_closure_plan(probe_report=probe)
    assert [spec["record"]["component_id"] for spec in plan["specs"]] == [
        "c5.2"
    ]
    assert plan["blocking_count"] == 1

    plan2 = build_section_closure_plan(
        probe_report=probe, include_nonblocking=True
    )
    assert {spec["record"]["component_id"] for spec in plan2["specs"]} == {
        "c5.1",
        "c5.2",
        "c5.3",
    }


def test_task_worthiness_direct_revision_skips_retrieval(
    tmp_path: Path,
) -> None:
    probe = _probe()
    probe["evidence_gap_records"][0]["claim_statement"] = (
        "This severely undermines reliability"
    )
    probe["evidence_gap_records"][0]["missing_fact_units"] = []
    plan = build_section_closure_plan(probe_report=probe)
    assert plan["retrieval_candidate_task_ids"] == []
    assert len(plan["direct_revision_task_ids"]) == 1

    retrieval_calls: list[str] = []
    revision_calls: list[str] = []

    def retrieval(context: dict):
        retrieval_calls.append(str(context["claim_id"]))
        raise AssertionError("direct revision must not retrieve")


    def revision(context: dict, outcome: dict):
        revision_calls.append(str(context["claim_id"]))
        return {
            "action": "narrow",
            "revised_claim": "Narrowed claim statement.",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "estimated_cost_cny": 0.001,
            },
        }

    report = run_section_supplementary_closure(
        probe_report=probe,
        output_dir=tmp_path / "out",
        dry_run=False,
        retrieval_wave_callback=retrieval,
        revision_callback=revision,
    )
    assert retrieval_calls == []
    assert revision_calls == ["c5.1"]
    assert report["tasks"][0]["revision_action"]["action"] == "narrow"
    assert report["tasks"][0]["per_target_results"][0][
        "revised_claim"
    ] == "Narrowed claim statement."
    assert report["terminal_counts"][STATUS_IMPROVED_STOP] == 1
    assert report["terminal_counts"][STATUS_NO_PROGRESS] == 0
    assert report["cost"]["revision"]["input_tokens"] == 5


def test_accept_reasoned_inference_skips_retrieval_and_is_eligible(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.gap_closure_downstream import (
        build_gap_closure_writing_context,
        merge_gap_closure_reports,
    )

    retrieval_calls: list[str] = []
    revision_calls: list[str] = []

    def retrieval(context: dict):
        retrieval_calls.append(str(context["claim_id"]))
        raise AssertionError("accept must not retrieve")

    def revision(*_args, **_kwargs):
        revision_calls.append(True)
        raise AssertionError("accept must not call revision author")

    def accept_adjudicator(record: dict, spec: dict) -> dict:
        return {
            "decision": "accept_reasoned_inference",
            "reason": "exact supplied quotes support cautious synthesis",
            "inference_rationale": (
                "premises are quoted exactly; no new empirical fact"
            ),
        }

    report = run_section_supplementary_closure(
        probe_report=_probe(),
        output_dir=tmp_path / "out",
        dry_run=False,
        retrieval_wave_callback=retrieval,
        revision_callback=revision,
        task_worthiness_adjudicator=accept_adjudicator,
    )

    assert retrieval_calls == []
    assert revision_calls == []
    task = report["tasks"][0]
    assert task["claim_id"] == "c5.1"
    assert task["status"] == STATUS_IMPROVED_STOP
    assert task["retrieval_wave_count"] == 0
    per_target = task["per_target_results"][0]
    assert per_target["next_action"] == "accept_reasoned_inference"
    assert per_target["inference_rationale"] == (
        "premises are quoted exactly; no new empirical fact"
    )

    context = build_gap_closure_writing_context(
        merge_gap_closure_reports(report)
    )
    assert "c5.1" in context["write_with_limits_claim_ids"]
    assert "c5.1" in context["eligible_claim_ids"]


def test_direct_revision_delete_stays_not_eligible(tmp_path: Path) -> None:
    from optomind_research.runtime.gap_closure_downstream import (
        build_gap_closure_writing_context,
        merge_gap_closure_reports,
    )

    retrieval_calls: list[str] = []

    def retrieval(context: dict):
        retrieval_calls.append(str(context["claim_id"]))
        raise AssertionError("direct revision must not retrieve")

    def revision_delete(context: dict, outcome: dict) -> dict:
        return {
            "action": "delete",
            "revised_claim": "",
            "residual_reviewer_comments": ["delete unsupported claim"],
        }

    report = run_section_supplementary_closure(
        probe_report=_probe(),
        output_dir=tmp_path / "out-delete",
        dry_run=False,
        retrieval_wave_callback=retrieval,
        revision_callback=revision_delete,
        task_worthiness_adjudicator=lambda record, spec: {
            "decision": "direct_revision",
            "reason": "delete unsupported statement",
        },
    )

    assert retrieval_calls == []
    task = report["tasks"][0]
    assert task["status"] == "revision_required"
    context = build_gap_closure_writing_context(
        merge_gap_closure_reports(report)
    )
    assert "c5.1" in context["revise_before_write_claim_ids"]
    assert "c5.1" not in context["eligible_claim_ids"]


def test_adjudicator_model_failure_routes_direct_revision_not_retrieval(
    tmp_path: Path,
) -> None:
    retrieval_calls: list[str] = []

    def retrieval(context: dict):
        retrieval_calls.append(str(context["claim_id"]))
        raise AssertionError("model failure must not launch retrieval")

    report = run_section_supplementary_closure(
        probe_report=_probe(),
        output_dir=tmp_path / "out",
        dry_run=False,
        retrieval_wave_callback=retrieval,
        task_worthiness_adjudicator=lambda record, spec: {
            "decision": "not-a-decision",
        },
    )

    assert retrieval_calls == []
    task = report["tasks"][0]
    assert task["status"] == "revision_required"
    state = json.loads(
        (tmp_path / "out" / STATE_JSON).read_text(encoding="utf-8")
    )
    state_task = state["tasks"][task["task_id"]]
    assert state_task["worthiness"]["decision"] == "direct_revision"
    assert (
        state_task["worthiness"]["reason"]
        == "adjudicator_invalid_decision_default_direct_revision"
    )


def test_local_cache_preflight_exact_quote_closes_without_llm() -> None:
    import optomind_research.runtime.supplementary_production_adapter as s04

    registry = {
        "target_claim_or_sentence": {"statement": "claim statement"},
        "missing_fact_units": ["exact required phrase"],
        "reviewer_feedback": {},
    }
    units = [{
        "unit_id": "u1",
        "unit_kind": "text_chunk",
        "identity": {"paper_id": "p1"},
        "durable_content": {
            "raw_text": "prefix exact required phrase suffix",
            "content_depth": "fulltext",
        },
        "durable_content_card": {
            "content_quality": {"source_kind": "fulltext"}
        },
        "query_annotations": [],
    }]

    result = s04.run_local_cache_evidence_preflight(
        registry,
        units,
        target_id="c1",
        target_type="claim",
        qwen_call=None,
    )

    assert result["progress"] == "closed"
    assert result["conclusion"] == "direct_support"
    assert result["exact_quote_matches"]["exact required phrase"] == ["u1"]
    assert result["source"] == "local_cache"


def test_local_cache_preflight_reasoned_inference_improved() -> None:
    import optomind_research.runtime.supplementary_production_adapter as s04

    registry = {
        "target_claim_or_sentence": {"statement": "claim statement"},
        "missing_fact_units": ["zebra quantum measurement"],
        "reviewer_feedback": {},
    }
    units = [{
        "unit_id": "u1",
        "unit_kind": "text_chunk",
        "identity": {"paper_id": "p1", "chunk_id": "c1"},
        "durable_content": {
            "raw_text": "candidate text zebra quantum measured validation",
            "content_depth": "fulltext",
        },
        "durable_content_card": {
            "content_quality": {"source_kind": "fulltext"}
        },
        "query_annotations": [],
    }]

    def fake_qwen(*args, **kwargs):
        return {
            "content": json.dumps({
                "conclusion": "reasoned_inference",
                "unit_quotes": [{
                    "unit_id": "u1",
                    "quote": "zebra quantum",
                }],
                "reason": "cautious synthesis",
            }),
            "_llm_usage": {"input_tokens": 7, "output_tokens": 3},
        }

    result = s04.run_local_cache_evidence_preflight(
        registry,
        units,
        target_id="c1",
        target_type="claim",
        qwen_call=fake_qwen,
    )

    assert result["progress"] == "improved"
    assert result["conclusion"] == "reasoned_inference"
    assert result["locally_validated_quotes"][0]["quote"] == "zebra quantum"
    assert result["locally_validated_quotes"][0]["paper_id"] == "p1"
    assert result["usage"]["input_tokens"] == 7


def test_local_cache_preflight_semantic_rank_precedence() -> None:
    import optomind_research.runtime.supplementary_production_adapter as s04

    registry = {
        "target_claim_or_sentence": {"statement": "claim statement"},
        "missing_fact_units": ["zebra quantum measurement"],
        "reviewer_feedback": {},
    }
    lexical_top = {
        "unit_id": "lexical-top",
        "unit_kind": "text_chunk",
        "identity": {"paper_id": "p1"},
        "durable_content": {
            "raw_text": "zebra quantum validated wording",
            "content_depth": "fulltext",
        },
        "durable_content_card": {
            "content_quality": {"source_kind": "fulltext"}
        },
        "query_annotations": [],
    }
    semantic_top = {
        "unit_id": "semantic-top",
        "unit_kind": "text_chunk",
        "identity": {"paper_id": "p2"},
        "durable_content": {
            "raw_text": "completely unrelated filler text",
            "content_depth": "fulltext",
        },
        "durable_content_card": {
            "content_quality": {"source_kind": "fulltext"}
        },
        "query_annotations": [],
    }
    captured: dict = {}

    def fake_qwen(*args, **kwargs):
        captured["payload"] = json.loads(args[1][-1]["content"])
        return {
            "content": json.dumps({
                "conclusion": "insufficient",
                "unit_quotes": [],
                "reason": "none",
            }),
            "_llm_usage": {},
        }

    def semantic_ranker(pool, *, query_text):
        by_id = {str(unit.get("unit_id") or ""): unit for unit in pool}
        return [by_id["semantic-top"], by_id["lexical-top"]]

    result = s04.run_local_cache_evidence_preflight(
        registry,
        [lexical_top, semantic_top],
        target_id="c1",
        target_type="claim",
        qwen_call=fake_qwen,
        semantic_ranker=semantic_ranker,
        max_candidates=1,
    )

    candidate_ids = [
        item["unit_id"]
        for item in captured["payload"]["candidate_units"]
    ]
    assert candidate_ids == ["semantic-top"]
    assert result["ranking_audit"]["ranking_mode"] == "semantic"
    assert result["ranking_audit"]["selected_count"] == 1


class _NoNetworkCoordinator:
    def __init__(
        self,
        db_path,
        *,
        base_units_path,
        base_vectors_path,
        snapshot_root,
        pipeline,
        revalidator,
        revision_callback,
    ):
        self.enqueue_calls = 0
        self.process_calls = 0
        self.snapshot_path = None

    def enqueue_gap_closure_spec(self, spec):
        self.enqueue_calls += 1
        return {"idempotency_key": str(spec["task_id"])}

    def process_next(self):
        self.process_calls += 1
        return {
            "idempotency_key": "job-key",
            "status": "closed",
            "retrieval_wave_count": 1,
            "result": {
                "per_target_results": [{
                    "target_id": "c5.1",
                    "target_type": "claim",
                    "progress": "closed",
                }],
                "snapshot_path": self.snapshot_path,
            },
        }

    def list_notifications(self):
        return []

    def close(self):
        pass


def test_default_production_local_closed_skips_network_retrieval(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    coordinator_instance = _NoNetworkCoordinator(
        tmp_path / "coordinator.sqlite",
        base_units_path=tmp_path / "base" / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=tmp_path / "base" / "material_vectors.sqlite",
        snapshot_root=tmp_path / "snapshots",
        pipeline=object(),
        revalidator=None,
        revision_callback=None,
    )
    monkeypatch.setattr(
        sgc,
        "SupplementaryGapClosureCoordinator",
        lambda *a, **k: coordinator_instance,
    )
    monkeypatch.setattr(s04, "make_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(
        s04,
        "make_revalidator",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )

    def boom_revision(**kwargs):
        raise AssertionError("closed local cache must not call revision")

    monkeypatch.setattr(
        s04, "make_revision_callback", lambda contexts, **kwargs: boom_revision
    )

    def local_closed(registry, units, **kwargs):
        return {
            "source": "local_cache",
            "progress": "closed",
            "conclusion": "direct_support",
            "reason": "local exact quote",
            "exact_quote_matches": {"unit one": ["u1"]},
            "locally_validated_quotes": [],
            "eligible_unit_count": 1,
            "snapshot_unit_count": 1,
            "ranking_audit": {},
            "adjudication": None,
            "usage": None,
        }

    monkeypatch.setattr(
        s04, "run_local_cache_evidence_preflight", local_closed
    )

    base_cache = _write_base_cache(tmp_path)
    report = run_section_supplementary_closure(
        probe_report=_write_probe(tmp_path),
        output_dir=tmp_path / "out",
        base_cache_dir=base_cache,
        dry_run=False,
        qwen_call=lambda *a, **k: {
            "content": json.dumps({
                "decision": "retrieval",
                "reason": "needs check",
            })
        },
    )

    assert coordinator_instance.enqueue_calls == 0
    assert coordinator_instance.process_calls == 0
    assert report["output_snapshot"] == str(base_cache)
    assert report["final_snapshot"] == str(base_cache)
    task = report["tasks"][0]
    assert task["claim_id"] == "c5.1"
    assert task["status"] == "closed"
    assert task["retrieval_wave_count"] == 0
    assert task["snapshot_path"] == str(base_cache)
    state = json.loads(
        (tmp_path / "out" / STATE_JSON).read_text(encoding="utf-8")
    )
    state_task = state["tasks"][task["task_id"]]
    assert state_task["outcome"]["local_cache"] is True
    assert state_task["outcome"]["local_cache_outcome"]["progress"] == "closed"
    assert state_task["revalidation_required"] is False


def test_local_cache_preflight_model_failure_falls_back_to_network(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    base = tmp_path / "base"
    base.mkdir()
    (base / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": [{
            "unit_id": "u1",
            "unit_kind": "text_chunk",
            "identity": {"paper_id": "p1"},
            "durable_content": {
                "raw_text": "zebra quantum validated wording",
                "content_depth": "fulltext",
            },
            "durable_content_card": {
                "content_quality": {"source_kind": "fulltext"}
            },
            "query_annotations": [],
        }]}),
        encoding="utf-8",
    )
    (base / "material_vectors.sqlite").write_bytes(b"fake-vectors")

    coordinator_instance = _NoNetworkCoordinator(
        tmp_path / "coordinator.sqlite",
        base_units_path=base / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=base / "material_vectors.sqlite",
        snapshot_root=tmp_path / "snapshots",
        pipeline=object(),
        revalidator=None,
        revision_callback=None,
    )
    monkeypatch.setattr(
        sgc,
        "SupplementaryGapClosureCoordinator",
        lambda *a, **k: coordinator_instance,
    )
    monkeypatch.setattr(s04, "make_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(
        s04,
        "make_revalidator",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )
    monkeypatch.setattr(
        s04,
        "make_revision_callback",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )
    monkeypatch.setattr(
        s04,
        "_default_semantic_embedder",
        lambda texts, **kwargs: [[1.0, 0.0]],
    )

    def qwen(agent_name, *args, **kwargs):
        if agent_name == "S04GapClosureRecheck":
            raise RuntimeError("transport exploded")
        return {
            "content": json.dumps({
                "decision": "retrieval",
                "reason": "needs check",
            })
        }

    report = run_section_supplementary_closure(
        probe_report=_write_probe(tmp_path),
        output_dir=tmp_path / "out",
        base_cache_dir=base,
        dry_run=False,
        qwen_call=qwen,
    )

    assert coordinator_instance.enqueue_calls == 1
    assert coordinator_instance.process_calls == 1
    assert report["terminal_counts"]["committed"] == 1
    state = json.loads(
        (tmp_path / "out" / STATE_JSON).read_text(encoding="utf-8")
    )
    state_task = next(
        task
        for task in state["tasks"].values()
        if task["claim_id"] == "c5.1"
    )
    local_outcome = state_task["outcome"]["local_cache_outcome"]
    assert local_outcome["progress"] == "no_progress"
    assert local_outcome["local_preflight_error"] == (
        "local_preflight_recheck_failed:RuntimeError"
    )
    assert state_task["outcome"]["local_cache"] is False


def test_default_production_local_improved_runs_revision_without_network(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    coordinator_instance = _NoNetworkCoordinator(
        tmp_path / "coordinator.sqlite",
        base_units_path=tmp_path / "base" / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=tmp_path / "base" / "material_vectors.sqlite",
        snapshot_root=tmp_path / "snapshots",
        pipeline=object(),
        revalidator=None,
        revision_callback=None,
    )
    monkeypatch.setattr(
        sgc,
        "SupplementaryGapClosureCoordinator",
        lambda *a, **k: coordinator_instance,
    )
    monkeypatch.setattr(s04, "make_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(
        s04,
        "make_revalidator",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )
    revision_calls: list = []

    def local_revision(**kwargs):
        revision_calls.append(kwargs)
        return {
            "results": [{
                "target_id": "c5.1",
                "target_type": "claim",
                "next_action": "narrow",
                "revised_claim": "narrowed from local evidence",
                "residual_reviewer_comments": ["keep"],
                "usage": {"input_tokens": 3},
            }]
        }

    monkeypatch.setattr(
        s04,
        "make_revision_callback",
        lambda contexts, **kwargs: local_revision,
    )

    def local_improved(registry, units, **kwargs):
        return {
            "source": "local_cache",
            "target_id": "c5.1",
            "target_type": "claim",
            "progress": "improved",
            "conclusion": "reasoned_inference",
            "reason": "local validated quote",
            "exact_quote_matches": {},
            "locally_validated_quotes": [{
                "unit_id": "u1",
                "quote": "local quote",
                "paper_id": "p1",
                "chunk_id": "c1",
            }],
            "eligible_unit_count": 1,
            "snapshot_unit_count": 1,
            "ranking_audit": {"ranking_mode": "lexical"},
            "adjudication": {"usage": {"input_tokens": 7}},
            "usage": {"input_tokens": 7},
        }

    monkeypatch.setattr(
        s04, "run_local_cache_evidence_preflight", local_improved
    )

    report = run_section_supplementary_closure(
        probe_report=_write_probe(tmp_path),
        output_dir=tmp_path / "out",
        base_cache_dir=_write_base_cache(tmp_path),
        dry_run=False,
        qwen_call=lambda *a, **k: {
            "content": json.dumps({
                "decision": "retrieval",
                "reason": "needs check",
            })
        },
    )

    assert coordinator_instance.enqueue_calls == 0
    assert coordinator_instance.process_calls == 0
    assert len(revision_calls) == 1
    task = report["tasks"][0]
    assert task["status"] == STATUS_IMPROVED_STOP
    assert task["retrieval_wave_count"] == 0
    assert task["per_target_results"][0]["next_action"] == "narrow"
    assert report["cost"]["revision"]["call_count"] == 1
    assert report["cost"]["revalidation"]["call_count"] == 1
    state = json.loads(
        (tmp_path / "out" / STATE_JSON).read_text(encoding="utf-8")
    )
    state_task = state["tasks"][task["task_id"]]
    assert state_task["outcome"]["local_cache"] is True
    assert state_task["outcome"]["local_cache_outcome"]["progress"] == (
        "improved"
    )
    assert state_task["revalidation_required"] is False


def test_default_production_sequential_accumulated_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    probe = _probe()
    probe["write_gate"] = {"blocker_claim_ids": ["c5.1", "c5.2"]}
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(
        json.dumps(probe, ensure_ascii=False), encoding="utf-8"
    )
    fake_snapshot = tmp_path / "snapshot-0001"
    fake_snapshot.mkdir()
    (fake_snapshot / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": []}), encoding="utf-8"
    )
    coordinator_instance = _NoNetworkCoordinator(
        tmp_path / "coordinator.sqlite",
        base_units_path=tmp_path / "base" / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=tmp_path / "base" / "material_vectors.sqlite",
        snapshot_root=tmp_path / "snapshots",
        pipeline=object(),
        revalidator=None,
        revision_callback=None,
    )
    coordinator_instance.snapshot_path = str(fake_snapshot)
    monkeypatch.setattr(
        sgc, "SupplementaryGapClosureCoordinator", lambda *a, **k: coordinator_instance
    )
    monkeypatch.setattr(s04, "make_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(
        s04,
        "make_revalidator",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )
    monkeypatch.setattr(
        s04,
        "make_revision_callback",
        lambda contexts, **kwargs: lambda **kw: {"results": []},
    )
    preflight_calls: list = []

    def sequential_preflight(registry, units, **kwargs):
        preflight_calls.append(kwargs.get("target_id"))
        progress = "closed" if len(preflight_calls) > 1 else "no_progress"
        return {
            "source": "local_cache",
            "progress": progress,
            "conclusion": "direct_support",
            "reason": "local",
            "exact_quote_matches": {},
            "locally_validated_quotes": [],
            "eligible_unit_count": 0,
            "snapshot_unit_count": 0,
            "ranking_audit": {},
            "adjudication": None,
            "usage": None,
        }

    monkeypatch.setattr(
        s04, "run_local_cache_evidence_preflight", sequential_preflight
    )

    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=tmp_path / "out",
        base_cache_dir=_write_base_cache(tmp_path),
        dry_run=False,
        qwen_call=lambda *a, **k: {
            "content": json.dumps({
                "decision": "retrieval",
                "reason": "needs check",
            })
        },
    )

    assert preflight_calls == ["c5.1", "c5.2"]
    assert coordinator_instance.enqueue_calls == 1
    assert coordinator_instance.process_calls == 1
    assert report["terminal_counts"]["committed"] == 2
    by_claim = {row["claim_id"]: row for row in report["tasks"]}
    assert by_claim["c5.1"]["retrieval_wave_count"] == 1
    assert by_claim["c5.2"]["retrieval_wave_count"] == 0
    state = json.loads(
        (tmp_path / "out" / STATE_JSON).read_text(encoding="utf-8")
    )
    second = next(
        task
        for task in state["tasks"].values()
        if task["claim_id"] == "c5.2"
    )
    assert second["outcome"]["local_cache"] is True
    assert second["outcome"]["local_cache_snapshot_path"] == str(fake_snapshot)


def test_report_consumable_by_gap_closure_downstream(tmp_path: Path) -> None:
    from optomind_research.runtime.gap_closure_downstream import (
        apply_gap_closure_to_report,
        build_gap_closure_writing_context,
        merge_gap_closure_reports,
    )

    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    retrieval_calls: list[str] = []
    outcomes = {
        "c5.1": STATUS_IMPROVED_STOP,
        "c5.2": STATUS_NO_PROGRESS,
        "c5.3": STATUS_NO_PROGRESS,
    }
    retrieval = _fake_retrieval_wave(outcomes, retrieval_calls)
    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        include_nonblocking=True,
        include_medium=True,
        dry_run=False,
        retrieval_wave_callback=retrieval,
    )

    merged = merge_gap_closure_reports(report)
    context = build_gap_closure_writing_context(merged)
    assert "c5.1" in context["write_with_limits_claim_ids"]
    assert "c5.2" in context["revise_before_write_claim_ids"]
    assert "c5.3" in context["revise_before_write_claim_ids"]
    assert "residual limit for c5.1" in (
        context["retained_reviewer_comments"].get("c5.1") or []
    )

    probe = _probe()
    revised = apply_gap_closure_to_report(probe, context)
    assert revised["final_claims"][0]["ready_for_write"] is True
    assert revised["final_claims"][1]["ready_for_write"] is False


def test_usage_aggregation_buckets_and_total() -> None:
    summary = _aggregate_usage([
        {
            "stage": "query_generation",
            "input_tokens": 100,
            "output_tokens": 10,
            "estimated_cost_cny": 0.01,
        },
        {
            "stage": "material_cards",
            "input_tokens": 200,
            "output_tokens": 20,
            "estimated_cost_cny": 0.02,
        },
        {
            "stage": "revalidation",
            "input_tokens": 50,
            "output_tokens": 5,
            "estimated_cost_cny": 0.005,
        },
    ])
    assert summary["provider_usage_available"] is True
    assert summary["query_generation"]["call_count"] == 1
    assert summary["material_cards"]["input_tokens"] == 200
    assert summary["revalidation"]["estimated_cost_cny"] == 0.005
    assert summary["total_input_tokens"] == 350
    assert summary["total_estimated_cost_cny"] == 0.035


def test_final_snapshot_uses_last_cumulative_job() -> None:
    assert _final_snapshot_from_jobs([
        {"result": {"snapshot_path": "snapshot-v1"}},
        {"result": {}},
        {"result": {"snapshot_path": "snapshot-v3"}},
    ]) == "snapshot-v3"


def test_report_final_snapshot_from_callback_outcomes(tmp_path: Path) -> None:
    probe_path = _write_probe(tmp_path)
    output = tmp_path / "out"
    retrieval_calls: list[str] = []
    outcomes = {
        "c5.1": STATUS_IMPROVED_STOP,
        "c5.2": STATUS_IMPROVED_STOP,
        "c5.3": STATUS_NO_PROGRESS,
    }
    snapshots = {
        "c5.1": "snapshot-v1",
        "c5.2": "snapshot-v2",
        "c5.3": "snapshot-v3",
    }
    retrieval = _fake_retrieval_wave(
        outcomes, retrieval_calls, snapshots=snapshots
    )
    report = run_section_supplementary_closure(
        probe_report=probe_path,
        output_dir=output,
        include_nonblocking=True,
        include_medium=True,
        dry_run=False,
        retrieval_wave_callback=retrieval,
    )
    assert report["final_snapshot"] == "snapshot-v3"


def test_collect_revision_usage_nested() -> None:
    records = _collect_revision_usage({
        "results": [
            {"usage": {"input_tokens": 10, "estimated_cost_cny": 0.01}}
        ],
        "model_usage": {
            "input_tokens": 20,
            "output_tokens": 5,
            "estimated_cost_cny": 0.02,
        },
    })
    assert len(records) == 2
    assert all(record["stage"] == "revision" for record in records)
    assert {record["estimated_cost_cny"] for record in records} == {
        0.01,
        0.02,
    }


def test_adjudication_payload_uses_resolved_context_fields() -> None:
    probe = _probe()
    probe["evidence_gap_records"][0]["current_evidence_summary"] = [
        {
            "chunk_id": "chunk1",
            "paper_id": "paper-one",
            "title": "Source title",
            "raw_text": "Existing quote text",
        }
    ]
    probe["evidence_gap_records"][0]["claim_role"] = "load_bearing"
    probe["evidence_gap_records"][0]["evidence_strength"] = "exact_quote"
    plan = build_section_closure_plan(probe_report=probe)
    spec = plan["specs"][0]
    captured: dict[str, Any] = {}

    def fake_qwen(agent_name: str, messages: list, **kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        return {
            "content": json.dumps({
                "decision": "retrieval",
                "reason": "new factual evidence could support",
            })
        }

    adjudicator = make_qwen_task_worthiness_adjudicator(fake_qwen)
    adjudicator(spec["record"], spec)

    payload = captured["payload"]
    assert payload["current_evidence_summary"][0]["paper_id"] == "paper-one"
    assert payload["current_evidence_summary"][0]["evidence"] == (
        "Existing quote text"
    )
    assert "required_material_strength" in payload
    assert "success_criteria" in payload
    assert "bound_papers_and_quotes" in payload
    assert "missing_fact_units" in payload
    assert payload["user_question"] == (
        "How do electromagnetic simulations gain credibility?"
    )
    assert payload["section_task"] == "Simulation Credibility"
    assert payload["target_claim"]["statement"] == "Claim five one statement."
    assert payload["claim_role"] == "load_bearing"
    assert payload["evidence_strength"] == "exact_quote"
    assert payload["failure_reason"] == "no exact quote"
    assert payload["sibling_verified_context"] == []


def test_adjudication_payload_carries_sibling_verified_context() -> None:
    probe = _probe()
    probe["final_claims"][0]["parent_claim_id"] = "parent-1"
    probe["final_claims"][0]["evidence_strength"] = "exact_quote"
    probe["final_claims"][0]["ready_for_write"] = False
    probe["final_claims"].append({
        "claim_id": "c5.4",
        "statement": "Verified sibling statement.",
        "role": "supporting",
        "evidence_strength": "fulltext_quote",
        "parent_claim_id": "parent-1",
        "ready_for_write": True,
        "caveats": ["narrow scope"],
        "verified_quotes": [{
            "quote": "sibling verified quote",
            "paper_id": "paper-9",
            "chunk_id": "chunk-9",
            "title": "Sibling source",
        }],
    })
    probe["final_claims"].append({
        "claim_id": "c5.5",
        "statement": "Unverified sibling statement.",
        "parent_claim_id": "parent-1",
        "ready_for_write": False,
    })
    plan = build_section_closure_plan(probe_report=probe)
    spec = plan["specs"][0]
    captured: dict[str, Any] = {}

    def fake_qwen(agent_name: str, messages: list, **kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        return {
            "content": json.dumps({
                "decision": "direct_revision",
                "reason": "verified sibling supports narrower statement",
            })
        }

    adjudicator = make_qwen_task_worthiness_adjudicator(fake_qwen)
    adjudicator(spec["record"], spec)

    context = captured["payload"]["sibling_verified_context"]
    assert [item["claim_id"] for item in context] == ["c5.4"]
    assert context[0]["statement"] == "Verified sibling statement."
    assert context[0]["caveats"] == ["narrow scope"]
    assert context[0]["verified_quotes"] == [{
        "quote": "sibling verified quote",
        "paper_id": "paper-9",
        "chunk_id": "chunk-9",
        "title": "Sibling source",
    }]


def test_qwen_call_propagated_and_usage_buckets_separated(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    captured: dict[str, Any] = {}
    fake_qwen = lambda *args, **kwargs: {
        "content": json.dumps({
            "decision": "retrieval",
            "reason": "missing factual unit",
        })
    }

    class FakePipeline:
        pass

    class FakeCoordinator:
        def __init__(
            self,
            db_path,
            *,
            base_units_path,
            base_vectors_path,
            snapshot_root,
            pipeline,
            revalidator,
            revision_callback,
        ):
            captured["revalidator"] = revalidator
            captured["revision_callback"] = revision_callback
            self.revalidator = revalidator

        def enqueue_gap_closure_spec(self, spec):
            return {"idempotency_key": str(spec["task_id"])}

        def process_next(self):
            per_target = self.revalidator(
                job_key="job-key",
                affected_targets=[("c5.1", "claim")],
                snapshot_path="snapshot-v1",
                retrieval_wave_count=1,
            )
            return {
                "idempotency_key": "job-key",
                "status": "closed",
                "result": {
                    "per_target_results": list(
                        per_target.get("results") or []
                    ),
                    "snapshot_path": "snapshot-v1",
                },
                "revision": {
                    "results": [
                        {
                            "usage": {
                                "output_tokens": 9,
                                "estimated_cost_cny": 0.02,
                            }
                        }
                    ]
                },
            }

        def list_notifications(self):
            return []

        def close(self):
            pass

    def fake_make_pipeline(
        output_dir,
        *,
        results_limit,
        snippet_limit,
        cards_model_tier,
        usage,
        qwen_call=None,
        **kwargs,
    ):
        captured["pipeline_qwen"] = qwen_call
        return FakePipeline()

    def fake_make_revalidator(contexts, *, qwen_call=None, **kwargs):
        captured["revalidator_qwen"] = qwen_call

        def revalidator(**kw):
            return {
                "results": [
                    {
                        "usage": {
                            "input_tokens": 7,
                            "estimated_cost_cny": 0.01,
                        }
                    }
                ]
            }

        return revalidator

    def fake_make_revision_callback(contexts, *, qwen_call=None, **kwargs):
        captured["revision_qwen"] = qwen_call
        return lambda **kw: {"results": []}

    monkeypatch.setattr(s04, "make_pipeline", fake_make_pipeline)
    monkeypatch.setattr(s04, "make_revalidator", fake_make_revalidator)
    monkeypatch.setattr(
        s04, "make_revision_callback", fake_make_revision_callback
    )
    monkeypatch.setattr(
        sgc, "SupplementaryGapClosureCoordinator", FakeCoordinator
    )

    report = run_section_supplementary_closure(
        probe_report=_write_probe(tmp_path),
        output_dir=tmp_path / "out",
        base_cache_dir=_write_base_cache(tmp_path),
        dry_run=False,
        qwen_call=fake_qwen,
    )

    assert captured["pipeline_qwen"] is fake_qwen
    assert captured["revalidator_qwen"] is fake_qwen
    assert captured["revision_qwen"] is fake_qwen
    assert report["cost"]["revalidation"]["call_count"] == 1
    assert report["cost"]["revalidation"]["input_tokens"] == 7
    assert report["cost"]["revision"]["call_count"] == 1
    assert report["cost"]["revision"]["output_tokens"] == 9


def test_qwen_adjudicator_decisions_and_failure_default() -> None:
    calls: list = []

    def fake_qwen(agent_name: str, messages: list, **kwargs):
        calls.append((agent_name, kwargs))
        content = messages[-1]["content"].casefold()
        if "narrower qualifier" in content:
            return {
                "content": json.dumps({
                    "decision": "direct_revision",
                    "reason": "existing quote supports narrower claim",
                }),
                "_llm_usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost_cny": 0.01,
                },
            }
        if "cautious author synthesis" in content:
            return {
                "content": json.dumps({
                    "decision": "accept_reasoned_inference",
                    "reason": "exact quotes support cautious synthesis",
                    "inference_rationale": "premises are quoted exactly",
                }),
                "_llm_usage": {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "estimated_cost_cny": 0.008,
                },
            }
        if "factual subdomain method" in content:
            return {
                "content": json.dumps({
                    "decision": "retrieval",
                    "reason": "new factual evidence could support",
                })
            }
        return {"content": "not-json"}

    adjudicator = make_qwen_task_worthiness_adjudicator(fake_qwen)
    direct = adjudicator(
        {"claim_statement": "Existing quote needs a narrower qualifier"},
        {},
    )
    retrieval = adjudicator(
        {"claim_statement": "Factual subdomain method comparison"},
        {},
    )
    accept = adjudicator(
        {"claim_statement": "Cautious author synthesis from exact quotes"},
        {},
    )
    failure = adjudicator({}, {})

    assert direct["decision"] == "direct_revision"
    assert direct["reason"] == "existing quote supports narrower claim"
    assert direct["usage"]["input_tokens"] == 10
    assert accept["decision"] == "accept_reasoned_inference"
    assert accept["inference_rationale"] == "premises are quoted exactly"
    assert accept["usage"]["input_tokens"] == 8
    assert retrieval["decision"] == "retrieval"
    assert failure["decision"] == "direct_revision"
    assert (
        failure["reason"]
        == "adjudicator_model_failure_default_direct_revision"
    )
    assert len(calls) == 4
    for _agent, kwargs in calls:
        assert kwargs["enable_thinking"] is False
        assert kwargs["temperature"] == 0
        assert kwargs["model_tier"] == "c2_model"


def test_production_adjudication_routes_direct_revision_and_retrieval(
    tmp_path: Path, monkeypatch
) -> None:
    import optomind_research.runtime.supplementary_gap_closure as sgc
    import optomind_research.runtime.supplementary_production_adapter as s04

    qwen_calls: list = []
    revision_calls: list = []

    def fake_qwen(agent_name: str, messages: list, **kwargs):
        qwen_calls.append((agent_name, kwargs))
        payload = json.loads(messages[-1]["content"])
        claim_id = str(payload["target_claim"].get("claim_id") or "")
        if claim_id == "c5.1":
            return {
                "content": json.dumps({
                    "decision": "direct_revision",
                    "reason": "existing quote supports narrower claim",
                }),
                "_llm_usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost_cny": 0.01,
                },
            }
        if claim_id == "c5.3":
            return {
                "content": json.dumps({
                    "decision": "retrieval",
                    "reason": "new factual evidence could support",
                }),
                "_llm_usage": {
                    "input_tokens": 12,
                    "output_tokens": 6,
                    "estimated_cost_cny": 0.012,
                },
            }
        return {"content": "{}"}

    class FakeCoordinator:
        def __init__(
            self,
            db_path,
            *,
            base_units_path,
            base_vectors_path,
            snapshot_root,
            pipeline,
            revalidator,
            revision_callback,
        ):
            self.revalidator = revalidator
            self.process_calls = 0

        def enqueue_gap_closure_spec(self, spec):
            return {"idempotency_key": str(spec["task_id"])}

        def process_next(self):
            self.process_calls += 1
            per_target = self.revalidator(
                job_key="job-key",
                affected_targets=[("c5.3", "claim")],
                snapshot_path="snapshot-v1",
                retrieval_wave_count=1,
            )
            return {
                "idempotency_key": "job-key",
                "status": "closed",
                "result": {
                    "per_target_results": list(
                        per_target.get("results") or []
                    ),
                    "snapshot_path": "snapshot-v1",
                },
            }

        def list_notifications(self):
            return []

        def close(self):
            pass

    def fake_make_pipeline(*args, **kwargs):
        return object()

    def fake_make_revalidator(contexts, *, qwen_call=None, **kwargs):
        def revalidator(**kw):
            return {"results": []}

        return revalidator

    def fake_make_revision_callback(contexts, *, qwen_call=None, **kwargs):
        def revision(**kw):
            revision_calls.append(kw)
            return {"results": [{
                "target_id": "c5.1",
                "target_type": "claim",
                "next_action": "narrow",
                "revised_claim": "Narrowed c5.1.",
                "usage": {"input_tokens": 3},
            }]}

        return revision

    monkeypatch.setattr(s04, "make_pipeline", fake_make_pipeline)
    monkeypatch.setattr(s04, "make_revalidator", fake_make_revalidator)
    monkeypatch.setattr(
        s04, "make_revision_callback", fake_make_revision_callback
    )
    monkeypatch.setattr(
        sgc, "SupplementaryGapClosureCoordinator", FakeCoordinator
    )

    output = tmp_path / "out"
    report = run_section_supplementary_closure(
        probe_report=_write_probe(tmp_path),
        output_dir=output,
        base_cache_dir=_write_base_cache(tmp_path),
        include_nonblocking=True,
        dry_run=False,
        qwen_call=fake_qwen,
    )

    assert len(qwen_calls) == 2
    for _agent, kwargs in qwen_calls:
        assert kwargs["enable_thinking"] is False
        assert kwargs["temperature"] == 0
    assert kwargs["model_tier"] == "c2_model"
    assert len(revision_calls) == 1
    assert revision_calls[0]["affected_targets"] == [("c5.1", "claim")]
    assert report["cost"]["query_generation"]["call_count"] == 2
    by_claim = {row["claim_id"]: row for row in report["tasks"]}
    assert by_claim["c5.1"]["status"] == STATUS_IMPROVED_STOP
    assert by_claim["c5.3"]["status"] == "closed"
    state = json.loads((output / STATE_JSON).read_text(encoding="utf-8"))
    assert any(
        task["claim_id"] == "c5.1"
        and task["worthiness"]["decision"] == "direct_revision"
        for task in state["tasks"].values()
    )
