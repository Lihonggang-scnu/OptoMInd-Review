"""Offline tests for the one-wave supplementary gap-closure coordinator."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.supplementary_gap_closure import (
    MAX_RETRIEVAL_WAVES,
    STATUS_CLOSED,
    STATUS_FAILED,
    STATUS_IMPROVED_STOP,
    STATUS_MERGING,
    STATUS_QUEUED,
    STATUS_REVISION_REQUIRED,
    GapClosureError,
    SupplementaryGapClosureCoordinator,
    _task_and_registry_from_spec,
    build_claim_evidence_gap_specs_from_probe,
    claim_evidence_gap_job_spec,
    typed_gap_job_spec,
    v19_claim_evidence_gap_job_specs,
)
from optomind_research.runtime.supplementary_retrieval_contract import (
    ContextRegistry,
    SupplementaryRetrievalTask,
)


REPORT_PATH = (
    Path("outputs")
    / "blueprint_quality_probe_20260807_cross_section_s04_v19_two_output_live"
    / "probe_S04_20260807T100307.json"
)
EXPECTED_V19_COMPONENTS = {
    "c1.3",
    "c2.2",
    "c4.2",
    "c5.3",
    "c10.2",
    "c14.2",
}


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""
    base = Path(tempfile.gettempdir()) / "optomind-gap-closure-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _shared_context() -> dict:
    return {
        "user_question": "How do electromagnetic simulation methods compare?",
        "topic_scope": {
            "main_scope": "optical electromagnetic simulation credibility",
            "lenses": ["mechanism", "validation"],
            "inclusion_boundaries": ["near-field fidelity", "far-field error"],
            "exclusion_boundaries": ["unrelated cross-domain material"],
        },
        "dynamic_axes": [
            {"axis_id": "Q01", "description": "method comparison"}
        ],
        "materialization_policy": {
            "priority": [
                "s2_structured_body",
                "public_oa_fulltext",
                "abstract_claim",
            ],
            "abstract_background_only": True,
        },
    }


def _record(record_id: str, claim_id: str = "C1") -> dict:
    return {
        "gap_id": record_id,
        "claim_id": claim_id,
        "failure_reason": "missing direct measurement evidence",
        "author_revision_suggestion": "narrow to measured near-field data",
        "missing_fact_units": ["measured near-field truncation error"],
        "bound_papers_and_quotes": [
            {"paper_id": "p1", "quote": "0.95 emissivity"}
        ],
        "required_material_strength": {
            "minimum": "factual_support",
            "abstract_ceiling": "background_only",
        },
        "success_criteria": ["has_measured_near_field_evidence"],
        "existing_paper_identities": ["doi:10.1/example"],
        "affected_targets": [
            {"target_id": claim_id, "target_type": "claim"}
        ],
    }


def _spec(record_id: str, claim_id: str = "C1", **overrides) -> dict:
    spec = claim_evidence_gap_job_spec(
        _record(record_id, claim_id),
        shared_context=_shared_context(),
    )
    spec.update(overrides)
    return spec


def _task_registry(spec: dict):
    return _task_and_registry_from_spec(spec)


class _FakePipeline:
    def __init__(
        self,
        results=None,
        *,
        idempotency_key="key-task",
        task_id="task-fixed",
    ):
        self.results = list(results or [])
        self.generate_calls = 0
        self.run_calls = 0
        self.idempotency_key = idempotency_key
        self.task_id = task_id

    def generate_and_submit(self, task, registry):
        self.generate_calls += 1
        return SimpleNamespace(
            status="queued",
            reused=False,
            idempotency_key=f"key-{task.task_id}",
            task_id=task.task_id,
            result=None,
        )

    def run_pending(self, *, max_tasks=1):
        self.run_calls += 1
        if not self.results:
            return []
        return [self.results.pop(0)]


def _committed_run(
    work_dir: Path,
    *,
    task_id="task-fixed",
    idempotency_key=None,
) -> SimpleNamespace:
    idempotency_key = (
        idempotency_key if idempotency_key is not None else f"key-{task_id}"
    )
    vector_dir = work_dir / "material_vectors"
    vector_dir.mkdir(parents=True, exist_ok=True)
    units_path = vector_dir / "MATERIAL_UNITS_FINAL.json"
    units_path.write_text('{"units": []}', encoding="utf-8")
    (vector_dir / "material_vectors.sqlite").write_bytes(b"vectors")
    return SimpleNamespace(
        status="committed",
        reason="committed",
        error="",
        task_id=task_id,
        idempotency_key=idempotency_key,
        result={
            "materialization": {
                "metadata": {
                    "final_units_path": str(units_path),
                    "vector_dir": str(vector_dir),
                }
            }
        },
    )


def _no_progress_run(
    *,
    task_id="task-fixed",
    idempotency_key=None,
) -> SimpleNamespace:
    idempotency_key = (
        idempotency_key if idempotency_key is not None else f"key-{task_id}"
    )
    return SimpleNamespace(
        status="no_progress",
        reason="no_adequate_material",
        error="",
        task_id=task_id,
        idempotency_key=idempotency_key,
        result=None,
    )


def _coordinator(
    tmp_path: Path,
    *,
    pipeline=None,
    revalidator=None,
    revision_callback=None,
    monkeypatch=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    (base / "MATERIAL_UNITS_FINAL.json").write_text("{}", encoding="utf-8")
    (base / "material_vectors.sqlite").write_bytes(b"base")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir(exist_ok=True)
    coordinator = SupplementaryGapClosureCoordinator(
        tmp_path / "coordinator.sqlite",
        base_units_path=base / "MATERIAL_UNITS_FINAL.json",
        base_vectors_path=base / "material_vectors.sqlite",
        snapshot_root=snapshots,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=revision_callback,
    )
    return coordinator, base, snapshots


def _install_fake_merge(monkeypatch, calls: list):
    import optomind_research.runtime.supplementary_gap_closure as module

    def fake_merge(
        *,
        base_units_path,
        base_vectors_path,
        increments,
        output_root,
        supplementary_conflict_policy=False,
    ):
        calls.append(
            {
                "base_units_path": str(base_units_path),
                "base_vectors_path": str(base_vectors_path),
                "increment_units": [
                    str(item.units_path) for item in increments
                ],
                "output_root": str(output_root),
                "supplementary_conflict_policy": bool(
                    supplementary_conflict_policy
                ),
            }
        )
        output_root = Path(output_root)
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "LONG_TERM_CACHE_MERGE_REPORT.json").write_text(
            '{"status":"completed"}', encoding="utf-8"
        )
        (output_root / "MATERIAL_UNITS_FINAL.json").write_text(
            '{"units": []}', encoding="utf-8"
        )
        return {"status": "completed"}

    monkeypatch.setattr(module, "merge_material_cache", fake_merge)
    return calls


def _progress_result(
    target_id: str,
    target_type: str,
    progress: str,
    **extra,
) -> dict:
    return {
        "target_id": target_id,
        "target_type": target_type,
        "progress": progress,
        "reason": f"{progress}-reason-{target_id}",
        "residual_reviewer_comments": [],
        **extra,
    }


def _make_revalidator(progress_map, default="no_progress"):
    def revalidator(*, job_key, affected_targets, snapshot_path, retrieval_wave_count):
        return {
            "results": [
                _progress_result(
                    target_id,
                    target_type,
                    progress_map.get((target_id, target_type), default),
                )
                for target_id, target_type in affected_targets
            ]
        }

    return revalidator


def _make_revision(actions_map=None, default="qualify"):
    def revision(
        *,
        job_key,
        affected_targets,
        snapshot_path,
        per_target_results=None,
    ):
        return {
            "results": [
                {
                    "target_id": target_id,
                    "target_type": target_type,
                    "next_action": (actions_map or {}).get(
                        (target_id, target_type), default
                    ),
                    "revised_claim": f"revised {target_id}",
                    "residual_reviewer_comments": [
                        f"residual {target_id}"
                    ],
                    "reason": "no_outcome_level_progress",
                }
                for target_id, target_type in affected_targets
            ]
        }

    return revision


def _enqueue_simple(
    coordinator,
    record_id,
    claim_id,
    *,
    task_id="task-fixed",
    extra_targets=(),
):
    spec = _spec(record_id, claim_id=claim_id, task_id=task_id)
    task, registry = _task_registry(spec)
    targets = [{"target_id": claim_id, "target_type": "claim"}]
    targets.extend(extra_targets)
    return coordinator.enqueue_gap_closure(
        task=task,
        registry=registry,
        affected_targets=targets,
        source_provenance={"producer": "test"},
        source_record_id=record_id,
    )


def test_enqueue_is_side_effect_free_beyond_coordinator_sqlite(
    tmp_path, monkeypatch
) -> None:
    pipeline = _FakePipeline()

    def boom(*args, **kwargs):
        raise AssertionError("must not be called during enqueue")

    pipeline.run_pending = boom
    revalidator = boom
    revision_callback = boom
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=revision_callback,
        monkeypatch=monkeypatch,
    )
    for index in range(6):
        task, registry = _task_registry(
            _spec(f"record-{index}", claim_id=f"C{index}")
        )
        coordinator.enqueue_gap_closure(
            task=task,
            registry=registry,
            affected_targets=[
                {"target_id": f"C{index}", "target_type": "claim"}
            ],
            source_provenance={"producer": "test"},
            source_record_id=f"record-{index}",
        )
    assert len(coordinator.list_jobs(status=STATUS_QUEUED)) == 6
    assert pipeline.generate_calls == 0
    assert pipeline.run_calls == 0
    assert merge_calls == []
    coordinator.close()


def test_duplicate_enqueue_reuses_job_and_unions_targets(tmp_path) -> None:
    coordinator, _base, _snapshots = _coordinator(tmp_path)
    task, registry = _task_registry(_spec("dup-record", claim_id="C1"))
    first = coordinator.enqueue_gap_closure(
        task=task,
        registry=registry,
        affected_targets=[{"target_id": "C1", "target_type": "claim"}],
        source_provenance={"producer": "test"},
        source_record_id="dup-record",
    )
    second = coordinator.enqueue_gap_closure(
        task=task,
        registry=registry,
        affected_targets=[
            {"target_id": "C1", "target_type": "claim"},
            {"target_id": "S1", "target_type": "section"},
        ],
        source_provenance={"producer": "test"},
        source_record_id="dup-record",
    )
    assert first["idempotency_key"] == second["idempotency_key"]
    assert len(coordinator.list_jobs()) == 1
    targets = {
        (row[0], row[1])
        for row in coordinator._conn.execute(
            "SELECT target_id, target_type FROM gap_closure_affected_targets"
        ).fetchall()
    }
    assert targets == {("C1", "claim"), ("S1", "section")}
    coordinator.close()


@pytest.mark.skipif(
    not REPORT_PATH.is_file(),
    reason="historical v19 probe fixture is archived and is not a mainline dependency",
)
def test_v19_adapter_six_claim_gaps_complete_context_and_determinism(
    tmp_path, monkeypatch
) -> None:
    assert REPORT_PATH.is_file(), f"missing v19 report: {REPORT_PATH}"
    specs = v19_claim_evidence_gap_job_specs(REPORT_PATH)
    assert len(specs) == 6
    assert all(spec["gap_type"] == "claim_evidence_gap" for spec in specs)
    components = {
        str(spec["record"].get("component_id") or spec["claim_id"])
        for spec in specs
    }
    assert components == EXPECTED_V19_COMPONENTS

    task_registry_by_component = {}
    for spec in specs:
        component = str(
            spec["record"].get("component_id") or spec["claim_id"]
        )
        task_registry_by_component[component] = _task_registry(spec)

    task_c13, registry_c13 = task_registry_by_component["c1.3"]
    assert task_c13.gap_type == "claim_evidence_gap"
    fields = registry_c13.fields
    assert fields["user_question"] == (
        "Compare PINN methods with differentiable electromagnetic solvers, "
        "including simulation credibility and the path from simulation to "
        "experiment."
    )
    assert fields["topic_scope"]["section_id"] == "S04"
    assert fields["topic_scope"]["report_fingerprint"]
    assert fields["reviewer_feedback"]["failure_reason"].startswith(
        "The claim asserts that PINNs enable 'dataset-free inverse design'."
    )
    assert fields["author_revision_history"][0]["note"].startswith(
        "Narrow the claim to reflect"
    )
    assert fields["retrieval_success_criteria"] == [
        "An exact contiguous quote from eligible current or newly retrieved "
        "evidence that entails the missing factual units and satisfies the "
        "claim's evidence permission ceiling."
    ]
    assert fields["missing_fact_units"] == [
        "The reviewed study reports that PINNs enable dataset-free inverse design."
    ]
    assert {
        entry["chunk_id"]
        for entry in fields["bound_papers_and_quotes"]
    } == {
        "m3gap:fa409a9eab91:0023",
        "s2chunk:280141441:11952:14166:1e8a0efd532fa450",
    }
    assert "8b1d611374f2e12210bb07d97d25710f3ef9fde0" in fields[
        "existing_paper_identities"
    ]
    assert fields["target_claim_or_sentence"]["statement"] == (
        "The reviewed study reports that PINNs enable dataset-free inverse design."
    )
    assert fields["required_material_strength"]["required_evidence"] == (
        "Exact source quote supporting: The reviewed study reports that "
        "PINNs enable dataset-free inverse design."
    )
    assert fields["reviewer_feedback"]["follow_up_retrieval_task"][
        "query_hints"
    ] == [
        "pinns enable dataset free",
        "pinns enable dataset free inverse design.",
    ]

    # Every one of the six specs builds a valid task+registry.
    assert set(task_registry_by_component) == EXPECTED_V19_COMPONENTS

    # Deterministic identities across two independent loads.
    specs2 = v19_claim_evidence_gap_job_specs(REPORT_PATH)
    for first, second in zip(specs, specs2):
        assert first["source_record_id"] == second["source_record_id"]
        assert first["task_id"] == second["task_id"]
        task_a, registry_a = _task_registry(first)
        task_b, registry_b = _task_registry(second)
        assert (
            task_a.task_id == task_b.task_id
            and task_a.to_dict() == task_b.to_dict()
            and registry_a.to_dict() == registry_b.to_dict()
        )

    # Enqueue is idempotent: second load reuses all six jobs.
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path, monkeypatch=monkeypatch
    )
    keys = [coordinator.enqueue_gap_closure_spec(spec)["idempotency_key"] for spec in specs]
    keys2 = [
        coordinator.enqueue_gap_closure_spec(spec)["idempotency_key"]
        for spec in specs2
    ]
    assert keys == keys2
    assert len(coordinator.list_jobs(status=STATUS_QUEUED)) == 6
    coordinator.close()


@pytest.mark.skipif(
    not REPORT_PATH.is_file(),
    reason="historical v19 probe fixture is archived and is not a mainline dependency",
)
def test_v20_like_report_does_not_supersede_v19_identity(tmp_path) -> None:
    payload = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    v19_specs = v19_claim_evidence_gap_job_specs(payload)
    v20_payload = copy.deepcopy(payload)
    v20_payload["probe_timestamp"] = "2026-08-08T00:00:00Z"
    v20_specs = v19_claim_evidence_gap_job_specs(v20_payload)
    assert len(v20_specs) == 6
    for first, second in zip(v19_specs, v20_specs):
        assert first["source_record_id"] != second["source_record_id"]
        assert first["task_id"] != second["task_id"]
        task_a, registry_a = _task_registry(first)
        task_b, registry_b = _task_registry(second)
        assert task_a.task_id != task_b.task_id
        assert registry_a.fields["topic_scope"]["report_fingerprint"] != (
            registry_b.fields["topic_scope"]["report_fingerprint"]
        )


def test_schema_persists_one_wave_policy_and_wave_count(tmp_path) -> None:
    coordinator, _base, _snapshots = _coordinator(tmp_path)
    job = _enqueue_simple(coordinator, "wave-policy", "C1")
    assert job["retrieval_wave_count"] == 0
    assert job["max_retrieval_waves"] == MAX_RETRIEVAL_WAVES == 1
    assert job["progress_assessment"] is None
    assert job["next_action"] is None
    row = coordinator._conn.execute(
        "SELECT max_retrieval_waves, retrieval_wave_count "
        "FROM gap_closure_jobs"
    ).fetchone()
    assert (row[0], row[1]) == (1, 0)
    coordinator.close()


def test_worker_committed_closed_merges_revalidates_notifies_only_targets(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidation_calls: list = []

    def revalidator(*, job_key, affected_targets, snapshot_path, retrieval_wave_count):
        revalidation_calls.append(
            {
                "job_key": job_key,
                "targets": list(affected_targets),
                "snapshot": str(snapshot_path),
                "wave": retrieval_wave_count,
            }
        )
        return {
            "results": [
                _progress_result(target_id, target_type, "closed")
                for target_id, target_type in affected_targets
            ]
        }

    revision_calls: list = []
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, base, snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=lambda **kwargs: revision_calls.append(kwargs),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(
        coordinator,
        "job-committed",
        "C1",
        extra_targets=[{"target_id": "V1", "target_type": "visual"}],
    )
    job = coordinator.process_next()
    assert job["status"] == STATUS_CLOSED
    assert job["retrieval_wave_count"] == 1
    assert job["max_retrieval_waves"] == 1
    assert job["next_action"] == "closed"
    assert pipeline.generate_calls == 1
    assert pipeline.run_calls == 1
    assert len(merge_calls) == 1
    assert merge_calls[0]["base_units_path"] == str(
        base / "MATERIAL_UNITS_FINAL.json"
    )
    snapshot = Path(job["result"]["snapshot_path"])
    assert snapshot.is_dir()
    assert coordinator._conn.execute(
        "SELECT value FROM gap_closure_meta WHERE key='current_snapshot'"
    ).fetchone()[0] == str(snapshot)
    assert len(revalidation_calls) == 1
    assert [item[0] for item in revalidation_calls[0]["targets"]] == [
        "C1",
        "V1",
    ]
    assert revision_calls == []
    notifications = coordinator.list_notifications()
    assert {
        (item["target_id"], item["target_type"]) for item in notifications
    } == {("C1", "claim"), ("V1", "visual")}
    assert all(
        item["cache_version"] == snapshot.name for item in notifications
    )
    assert all(
        item["closure_result"]["progress"] == "closed"
        for item in notifications
    )
    assert coordinator.recover() == 0
    assert coordinator.process_next() is None
    coordinator.close()


def test_coordinator_merge_uses_supplementary_conflict_policy(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}, default="closed"),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "policy-merge", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_CLOSED
    assert len(merge_calls) == 1
    assert merge_calls[0]["supplementary_conflict_policy"] is True
    coordinator.close()


def test_improved_but_not_closed_stops_with_retained_comments(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    comments = ["residual spectral-bias caveat", "credibility wording still weak"]

    def revalidator(*, job_key, affected_targets, snapshot_path, retrieval_wave_count):
        return {
            "results": [
                _progress_result(
                    target_id,
                    target_type,
                    "improved",
                    residual_reviewer_comments=comments,
                )
                for target_id, target_type in affected_targets
            ]
        }

    revision_calls: list = []
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)

    def revision_callback(**kwargs):
        revision_calls.append(kwargs)
        return _make_revision(default="qualify")(**kwargs)

    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=revision_callback,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "improved", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["next_action"] == "stop_improved"
    assert job["retrieval_wave_count"] == 1
    assert len(merge_calls) == 1
    assert len(revision_calls) == 1
    assert ("C1", "claim") in revision_calls[0]["per_target_results"]
    per_target = job["result"]["per_target_results"][0]
    assert per_target["residual_reviewer_comments"] == comments
    assert per_target["revised_claim"] == "revised C1"
    assert per_target["next_action"] == "qualify"
    assert per_target["progress"] == "improved"
    assert job["result"]["revision"]["reason"] == (
        "improved_stop_local_revision"
    )
    notification = coordinator.list_notifications(target_id="C1")[0]
    assert notification["closure_result"]["progress"] == "improved"
    assert notification["closure_result"]["residual_reviewer_comments"] == (
        comments
    )
    assert coordinator.process_next() is None
    coordinator.close()


def test_improved_stop_missing_revision_callback_preserves_progress(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidator = _make_revalidator({}, default="improved")
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=None,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "improved-no-rev", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["next_action"] == "stop_improved"
    assert job["retrieval_wave_count"] == 1
    assert "missing_revision_callback_improved_stop" in str(
        job.get("error") or ""
    )
    assert job["result"]["per_target_results"][0]["progress"] == "improved"
    assert coordinator.process_next() is None
    coordinator.close()


def test_improved_stop_revision_failure_preserves_progress(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidator = _make_revalidator({}, default="improved")
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)

    def boom_revision(**kwargs):
        raise RuntimeError("revision exploded")

    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=boom_revision,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "improved-rev-fail", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["next_action"] == "stop_improved"
    assert "improved_stop_revision_failed" in str(job.get("error") or "")
    assert job["result"]["per_target_results"][0]["progress"] == "improved"
    coordinator.close()


def test_no_progress_non_delete_revision_becomes_improved_stop(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    def revalidator(
        *,
        job_key,
        affected_targets,
        snapshot_path,
        retrieval_wave_count,
    ):
        return {
            "results": [
                _progress_result(
                    target_id,
                    target_type,
                    "no_progress",
                    exact_quote_matches={"required phrase": ["u1"]},
                    locally_validated_quotes=[
                        {"unit_id": "u1", "quote": "validated quote"}
                    ],
                )
                for target_id, target_type in affected_targets
            ]
        }

    revision_calls: list = []

    def revision(
        *,
        job_key,
        affected_targets,
        snapshot_path,
        per_target_results=None,
    ):
        revision_calls.append(
            {
                "targets": list(affected_targets),
                "snapshot": snapshot_path,
                "per_target_results": per_target_results,
            }
        )
        return {
            "results": [
                {
                    "target_id": target_id,
                    "target_type": target_type,
                    "next_action": "qualify",
                    "revised_claim": f"qualified {target_id}",
                    "residual_reviewer_comments": [f"residual {target_id}"],
                    "reason": "no_outcome_level_progress",
                }
                for target_id, target_type in affected_targets
            ]
        }

    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=revision,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "no-progress-committed", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["next_action"] == "stop_improved"
    assert job["retrieval_wave_count"] == 1
    assert len(merge_calls) == 1
    assert len(revision_calls) == 1
    assert revision_calls[0]["snapshot"] == job["result"]["snapshot_path"]
    forwarded = revision_calls[0]["per_target_results"]
    assert forwarded is not None
    assert ("C1", "claim") in forwarded
    assert forwarded[("C1", "claim")]["progress"] == "no_progress"
    per_target = job["result"]["per_target_results"][0]
    assert per_target["progress"] == "no_progress"
    assert per_target["reason"] == "no_progress-reason-C1"
    assert per_target["next_action"] == "qualify"
    assert per_target["revised_claim"] == "qualified C1"
    assert per_target["exact_quote_matches"] == {"required phrase": ["u1"]}
    assert per_target["locally_validated_quotes"] == [
        {"unit_id": "u1", "quote": "validated quote"}
    ]
    assert job["result"]["progress_assessment"]["per_target"][0][
        "locally_validated_quotes"
    ] == [{"unit_id": "u1", "quote": "validated quote"}]
    assert job["result"]["revision"]["results"][0]["next_action"] == "qualify"
    assert job["result"]["progress_assessment"]["verdict"] == (
        STATUS_REVISION_REQUIRED
    )
    notification = coordinator.list_notifications(target_id="C1")[0]
    assert notification["closure_result"]["next_action"] == "qualify"
    assert notification["closure_result"]["progress"] == "no_progress"
    assert notification["closure_result"]["revised_claim"] == "qualified C1"
    assert coordinator.recover() == 0
    assert coordinator.process_next() is None
    coordinator.close()


def test_no_progress_pipeline_run_revision_without_snapshot_or_merge(
    tmp_path, monkeypatch
) -> None:
    pipeline = _FakePipeline([_no_progress_run()])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}),
        revision_callback=_make_revision(default="rewrite"),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "no-progress-run", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["next_action"] == "stop_improved"
    assert job["result"]["snapshot_path"] is None
    assert merge_calls == []
    assert not list(snapshots.glob("snapshot-*"))
    assert coordinator.list_notifications() != []
    assert coordinator.list_notifications()[0]["cache_version"] == "base"
    assert coordinator.recover() == 0
    assert coordinator.process_next() is None
    coordinator.close()


def test_no_progress_delete_revision_stays_revision_required(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidator = _make_revalidator({}, default="no_progress")

    def revision(
        *,
        job_key,
        affected_targets,
        snapshot_path,
        per_target_results=None,
    ):
        return {
            "results": [
                {
                    "target_id": target_id,
                    "target_type": target_type,
                    "next_action": "delete",
                    "revised_claim": "",
                    "residual_reviewer_comments": ["delete unsupported"],
                }
                for target_id, target_type in affected_targets
            ]
        }

    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=revision,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "no-progress-delete", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_REVISION_REQUIRED
    assert job["next_action"] == "revision:delete"
    per_target = job["result"]["per_target_results"][0]
    assert per_target["progress"] == "no_progress"
    assert per_target["next_action"] == "delete"
    coordinator.close()


def test_no_progress_malformed_revision_stays_revision_required(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidator = _make_revalidator({}, default="no_progress")

    def boom_revision(**kwargs):
        raise RuntimeError("revision exploded")

    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=boom_revision,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "no-progress-malformed", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_REVISION_REQUIRED
    assert job["next_action"] == "revision_required"
    assert "no_progress_revision_failed:RuntimeError" in str(
        job.get("error") or ""
    )
    assert job["result"]["per_target_results"][0]["progress"] == "no_progress"
    coordinator.close()


def test_no_progress_missing_revision_callback_stays_revision_required(
    tmp_path, monkeypatch
) -> None:
    pipeline = _FakePipeline([_no_progress_run()])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}),
        revision_callback=None,
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "no-progress-no-rev", "C1")
    job = coordinator.process_next()

    assert job["status"] == STATUS_REVISION_REQUIRED
    assert job["next_action"] == "revision_required"
    assert "missing_revision_callback" in str(job.get("error") or "")
    coordinator.close()


def test_exact_run_identity_is_required(tmp_path, monkeypatch) -> None:
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)

    def build_run(work_dir, *, task_id="task-fixed", key="key-task-fixed"):
        run = _committed_run(work_dir, task_id=task_id, idempotency_key=key)
        return run

    cases = [
        (
            "missing-identity",
            build_run(tmp_path / "inc-missing", task_id="", key=""),
            "no matching task identity",
        ),
        (
            "wrong-key",
            build_run(tmp_path / "inc-key", task_id="", key="other-key"),
            "idempotency_key",
        ),
        (
            "wrong-task-id",
            build_run(
                tmp_path / "inc-task",
                task_id="other-task",
                key="key-task",
            ),
            "task_id",
        ),
    ]
    for record_id, run, error_fragment in cases:
        pipeline = _FakePipeline([run])
        coordinator, _base, _snapshots = _coordinator(
            tmp_path / record_id,
            pipeline=pipeline,
            revalidator=_make_revalidator({("C1", "claim"): "closed"}),
            revision_callback=_make_revision(),
            monkeypatch=monkeypatch,
        )
        _enqueue_simple(coordinator, record_id, "C1")
        job = coordinator.process_next()
        assert job["status"] == STATUS_FAILED
        assert error_fragment in job["error"]
        assert merge_calls == []
        assert pipeline.run_calls == 1
        coordinator.close()

    run_ok = build_run(tmp_path / "inc-ok")
    pipeline_ok = _FakePipeline([run_ok])
    coordinator_ok, _base_ok, _snapshots_ok = _coordinator(
        tmp_path / "ok",
        pipeline=pipeline_ok,
        revalidator=_make_revalidator({("C1", "claim"): "closed"}),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator_ok, "ok", "C1")
    job_ok = coordinator_ok.process_next()
    assert job_ok["status"] == STATUS_CLOSED
    assert len(merge_calls) == 1
    coordinator_ok.close()


def test_per_target_recheck_results_not_aggregated(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidator = _make_revalidator(
        {
            ("C1", "claim"): "closed",
            ("S1", "section"): "improved",
        },
        default="improved",
    )
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=revalidator,
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(
        coordinator,
        "per-target",
        "C1",
        extra_targets=[{"target_id": "S1", "target_type": "section"}],
    )
    job = coordinator.process_next()
    assert job["status"] == STATUS_IMPROVED_STOP
    by_target = {
        (item["target_id"], item["target_type"]): item
        for item in job["result"]["per_target_results"]
    }
    assert by_target[("C1", "claim")]["progress"] == "closed"
    assert by_target[("S1", "section")]["progress"] == "improved"
    notifications = {
        (item["target_id"], item["target_type"]): item
        for item in coordinator.list_notifications()
    }
    assert notifications[("C1", "claim")]["closure_result"]["progress"] == (
        "closed"
    )
    assert notifications[("S1", "section")]["closure_result"]["progress"] == (
        "improved"
    )
    coordinator.close()


def test_failed_callback_auditable_and_terminal_not_requeued(
    tmp_path, monkeypatch
) -> None:
    class ExplodingPipeline(_FakePipeline):
        def run_pending(self, *, max_tasks=1):
            self.run_calls += 1
            raise RuntimeError("boom")

    pipeline = ExplodingPipeline()
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "fail", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_FAILED
    assert "RuntimeError: boom" in job["error"]
    events = [item.get("event") for item in job["attempts"]]
    assert "worker_error" in events
    assert coordinator.recover() == 0
    assert coordinator.process_next() is None
    coordinator.close()


def test_interrupted_wave_recovery_does_not_search_again(
    tmp_path, monkeypatch
) -> None:
    class ReplayActivePipeline(_FakePipeline):
        def generate_and_submit(self, task, registry):
            self.generate_calls += 1
            return SimpleNamespace(
                status="queued",
                reused=True,
                idempotency_key="key-task-fixed",
                task_id="task-fixed",
                result=None,
            )

        def run_pending(self, *, max_tasks=1):
            raise AssertionError("second retrieval wave must not run")

    pipeline = ReplayActivePipeline()
    revision_calls: list = []
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}),
        revision_callback=lambda **kwargs: revision_calls.append(kwargs)
        or {"results": []},
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "interrupt", "C1")
    coordinator._conn.execute(
        "UPDATE gap_closure_jobs SET status=?, retrieval_wave_count=? "
        "WHERE source_record_id=?",
        (STATUS_MERGING, 1, "interrupt"),
    )
    coordinator._conn.commit()
    assert coordinator.recover() == 1
    job = coordinator.process_next()
    assert job["status"] == STATUS_FAILED
    assert job["error"].startswith(
        "recovered job already spent its single retrieval wave"
    )
    assert job["next_action"] == "manual_resume"
    assert pipeline.run_calls == 0
    assert pipeline.generate_calls == 1
    assert revision_calls == []
    events = [item.get("event") for item in job["attempts"]]
    assert "retrieval_wave_interrupted" in events
    assert coordinator.recover() == 0
    assert coordinator.process_next() is None
    coordinator.close()


def test_two_sequential_commits_merge_against_previous_snapshot(
    tmp_path, monkeypatch
) -> None:
    first_work = tmp_path / "inc1"
    second_work = tmp_path / "inc2"
    pipeline = _FakePipeline(
        [
            _committed_run(first_work, task_id="task-a"),
            _committed_run(second_work, task_id="task-b"),
        ]
    )
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, base, snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}, default="improved"),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    for record_id, claim_id, task_id in (
        (
            "seq-a",
            "C1",
            "task-a",
        ),
        (
            "seq-b",
            "C2",
            "task-b",
        )
    ):
        spec = _spec(record_id, claim_id=claim_id, task_id=task_id)
        task, registry = _task_registry(spec)
        coordinator.enqueue_gap_closure(
            task=task,
            registry=registry,
            affected_targets=[
                {"target_id": claim_id, "target_type": "claim"}
            ],
            source_provenance={"producer": "test"},
            source_record_id=record_id,
        )
    first_job = coordinator.process_next()
    second_job = coordinator.process_next()
    assert first_job["status"] == STATUS_IMPROVED_STOP
    assert second_job["status"] == STATUS_IMPROVED_STOP
    assert len(merge_calls) == 2
    assert merge_calls[0]["base_units_path"] == str(
        base / "MATERIAL_UNITS_FINAL.json"
    )
    assert merge_calls[1]["base_units_path"] == str(
        snapshots / "snapshot-0001" / "MATERIAL_UNITS_FINAL.json"
    )
    assert coordinator._conn.execute(
        "SELECT value FROM gap_closure_meta WHERE key='current_snapshot'"
    ).fetchone()[0] == str(snapshots / "snapshot-0002")
    coordinator.close()


def test_idempotent_replay_does_not_remerge_or_revalidate(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    revalidation_calls: list = []
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=lambda **kwargs: revalidation_calls.append(kwargs)
        or _make_revalidator({}, default="closed")(
            job_key=kwargs["job_key"],
            affected_targets=kwargs["affected_targets"],
            snapshot_path=kwargs["snapshot_path"],
            retrieval_wave_count=kwargs["retrieval_wave_count"],
        ),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "replay", "C1")
    assert coordinator.process_next()["status"] == STATUS_CLOSED
    merge_calls_after_first = len(merge_calls)
    revalidation_after_first = len(revalidation_calls)
    assert coordinator.process_next() is None
    assert len(merge_calls) == merge_calls_after_first
    assert len(revalidation_calls) == revalidation_after_first
    assert pipeline.generate_calls == 1
    coordinator.close()


def test_missing_increment_paths_fail_closed(tmp_path, monkeypatch) -> None:
    run = SimpleNamespace(
        status="committed",
        reason="committed",
        error="",
        task_id="task-fixed",
        idempotency_key="key-task-fixed",
        result={"materialization": {"metadata": {}}},
    )
    pipeline = _FakePipeline([run])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "missing-paths", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_FAILED
    assert job["error"] == (
        "committed outcome metadata lacks valid increment paths"
    )
    assert merge_calls == []
    coordinator.close()


def test_notification_filtering_and_ack(tmp_path, monkeypatch) -> None:
    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}, default="closed"),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(
        coordinator,
        "notify",
        "C1",
        extra_targets=[{"target_id": "S1", "target_type": "section"}],
    )
    coordinator.process_next()
    assert {
        item["target_id"] for item in coordinator.list_notifications(target_id="C1")
    } == {"C1"}
    assert coordinator.list_notifications(target_id="missing") == []
    target = coordinator.list_notifications(target_id="S1")[0]
    assert coordinator.ack_notification(target["notification_id"]) is True
    acked = coordinator.list_notifications(target_id="S1", status="acked")
    assert len(acked) == 1 and acked[0]["acked_at"]
    assert coordinator.ack_notification(target["notification_id"]) is False
    coordinator.close()


def test_factory_generic_mapping_and_validation() -> None:
    spec = _spec("generic", claim_id="C7")
    task, registry = _task_registry(spec)
    assert task.gap_type == "claim_evidence_gap"
    assert registry.fields["missing_fact_units"] == [
        "measured near-field truncation error"
    ]
    assert registry.fields["reviewer_feedback"]["failure_reason"] == (
        "missing direct measurement evidence"
    )
    assert registry.fields["author_revision_history"][0]["note"] == (
        "narrow to measured near-field data"
    )
    assert registry.fields["required_material_strength"]["minimum"] == (
        "factual_support"
    )
    assert set(registry.fields) >= {
        "topic_scope",
        "user_question",
        "dynamic_axes",
        "target_claim_or_sentence",
        "bound_papers_and_quotes",
        "missing_fact_units",
        "required_material_strength",
        "retrieval_success_criteria",
        "existing_paper_identities",
        "materialization_policy",
    }

    bad = _spec("bad", claim_id="C8")
    bad["shared_context"] = {}
    with pytest.raises(GapClosureError, match="required projected context fields"):
        _task_and_registry_from_spec(bad)

    typed = typed_gap_job_spec(
        {
            **_record("typed", claim_id="C9"),
            "gap_type": "section_argument_gap",
        },
        shared_context=_shared_context(),
    )
    assert typed["gap_type"] == "section_argument_gap"
    with pytest.raises(GapClosureError, match="visual_material_gap"):
        typed_gap_job_spec(
            {
                **_record("visual", claim_id="C10"),
                "gap_type": "visual_material_gap",
            },
            shared_context=_shared_context(),
        )


def test_section_argument_gap_worker_uses_one_wave_improved_stop(
    tmp_path, monkeypatch
) -> None:
    record = {
        "gap_type": "section_argument_gap",
        "id": "section-gap-1",
        "section_task": {
            "task_id": "S1",
            "title": "Near-field evidence argument",
            "argument_task": "turn evidence into a distinct-role argument",
            "required_roles": ["mechanism", "validation"],
        },
        "argument_role": "mechanism_explanation",
        "failure_reason": "section lacks distinct-role claims",
        "author_revision_suggestion": "add a mechanism claim with exact quote",
        "missing_fact_units": ["near-field truncation evidence"],
        "bound_papers_and_quotes": [
            {"paper_id": "p1", "quote": "0.95 emissivity"}
        ],
        "required_material_strength": {
            "minimum": "factual_support",
            "abstract_ceiling": "background_only",
        },
        "success_criteria": ["has_distinct_role_claim"],
        "existing_paper_identities": ["doi:10.1/example"],
        "affected_targets": [
            {"target_id": "S1", "target_type": "section"}
        ],
    }
    spec = typed_gap_job_spec(record, shared_context=_shared_context())
    spec["task_id"] = "task-fixed"
    task, registry = _task_registry(spec)
    assert task.gap_type == "section_argument_gap"
    assert registry.fields["section_task"]["task_id"] == "S1"
    assert registry.fields["argument_role"] == "mechanism_explanation"

    work_dir = tmp_path / "task-increment"
    pipeline = _FakePipeline([_committed_run(work_dir)])
    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator(
            {("S1", "section"): "improved"}, default="improved"
        ),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    coordinator.enqueue_gap_closure(
        task=task,
        registry=registry,
        affected_targets=[{"target_id": "S1", "target_type": "section"}],
        source_provenance={"producer": "test"},
        source_record_id="section-gap-1",
    )
    job = coordinator.process_next()
    assert job["status"] == STATUS_IMPROVED_STOP
    assert job["retrieval_wave_count"] == 1
    assert len(merge_calls) == 1
    notification = coordinator.list_notifications(target_id="S1")[0]
    assert notification["closure_result"]["progress"] == "improved"
    coordinator.close()


def test_enqueue_gap_closure_spec_uses_factory_targets(tmp_path) -> None:
    coordinator, _base, _snapshots = _coordinator(tmp_path)
    spec = _spec("spec-enqueue", claim_id="C11")
    job = coordinator.enqueue_gap_closure_spec(spec)
    assert job["status"] == STATUS_QUEUED
    targets = {
        (row[0], row[1])
        for row in coordinator._conn.execute(
            "SELECT target_id, target_type FROM gap_closure_affected_targets"
        ).fetchall()
    }
    assert targets == {("C11", "claim")}
    coordinator.close()


def test_visual_gap_rejected_by_factory_and_direct_enqueue(tmp_path) -> None:
    coordinator, _base, _snapshots = _coordinator(tmp_path)
    spec = _spec("visual-direct", claim_id="C12")
    spec["gap_type"] = "visual_material_gap"
    with pytest.raises(GapClosureError, match="visual_material_gap"):
        _task_and_registry_from_spec(spec)
    task = SupplementaryRetrievalTask(
        task_id="visual-task",
        gap_type="visual_material_gap",
        context_refs=("topic_scope",),
        visual_route=True,
    )
    with pytest.raises(GapClosureError, match="visual_material_gap"):
        coordinator.enqueue_gap_closure(
            task=task,
            registry=ContextRegistry(),
            affected_targets=[{"target_id": "V1", "target_type": "visual"}],
            source_provenance={"producer": "test"},
            source_record_id="visual-direct",
        )
    coordinator.close()


def test_reused_submission_skips_run_pending_and_resumes(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "task-increment"
    committed_result = _committed_run(work_dir).result

    class ReusePipeline:
        def __init__(self, status, result):
            self.status = status
            self.result = result
            self.run_calls = 0

        def generate_and_submit(self, task, registry):
            return SimpleNamespace(
                status=self.status,
                reused=True,
                reuse_reason=f"{self.status}_replay",
                idempotency_key="key-task-fixed",
                task_id="task-fixed",
                result=self.result,
            )

        def run_pending(self, *, max_tasks=1):
            self.run_calls += 1
            return []

    merge_calls: list = []
    _install_fake_merge(monkeypatch, merge_calls)
    pipeline = ReusePipeline("committed", committed_result)
    coordinator, _base, _snapshots = _coordinator(
        tmp_path,
        pipeline=pipeline,
        revalidator=_make_revalidator({}, default="closed"),
        revision_callback=_make_revision(),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator, "reuse-committed", "C1")
    job = coordinator.process_next()
    assert job["status"] == STATUS_CLOSED
    assert pipeline.run_calls == 0
    assert len(merge_calls) == 1
    assert len(coordinator.list_notifications()) == 1
    merge_calls_after_committed = len(merge_calls)
    coordinator.close()

    pipeline_np = ReusePipeline("no_progress", None)
    revision_calls: list = []
    coordinator_np, _base_np, _snapshots_np = _coordinator(
        tmp_path / "np-reuse",
        pipeline=pipeline_np,
        revalidator=_make_revalidator({}),
        revision_callback=lambda **kwargs: revision_calls.append(kwargs)
        or _make_revision(default="qualify")(
            job_key=kwargs["job_key"],
            affected_targets=kwargs["affected_targets"],
            snapshot_path=kwargs["snapshot_path"],
        ),
        monkeypatch=monkeypatch,
    )
    _enqueue_simple(coordinator_np, "reuse-np", "C1")
    job_np = coordinator_np.process_next()
    assert job_np["status"] == STATUS_IMPROVED_STOP
    assert job_np["next_action"] == "stop_improved"
    assert pipeline_np.run_calls == 0
    assert len(merge_calls) == merge_calls_after_committed
    assert len(revision_calls) == 1
    assert coordinator_np.list_notifications() != []
    coordinator_np.close()


def test_version_agnostic_probe_spec_builder_selection_and_stability() -> None:
    probe = {
        "schema_version": "blueprint_quality_probe.v1",
        "probe_timestamp": "2026-08-10T00:00:00+00:00",
        "section_id": "S05",
        "section_title": "Simulation Credibility",
        "research_context": {
            "user_question": "How do simulations gain credibility?",
            "scope_definition": "optical simulation credibility",
            "key_questions": ["Which error sources dominate?"],
        },
        "final_claims": [
            {
                "claim_id": "c5.1",
                "statement": "Load bearing statement.",
                "role": "load_bearing",
            },
            {
                "claim_id": "c5.2",
                "statement": "Supporting statement.",
                "role": "supporting",
            },
        ],
        "evidence_gap_records": [
            {
                "claim_id": "c5.1",
                "component_id": "c5.1",
                "importance": "high",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit"],
                "current_evidence_summary": [],
            },
            {
                "claim_id": "c5.2",
                "component_id": "c5.2",
                "importance": "medium",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit"],
                "current_evidence_summary": [],
            },
        ],
    }

    blocking = build_claim_evidence_gap_specs_from_probe(probe)
    assert [spec["record"]["component_id"] for spec in blocking] == ["c5.1"]
    first = blocking[0]
    assert first["gap_type"] == "claim_evidence_gap"
    assert first["task_id"].startswith("gap-")
    assert first["source_record_id"].endswith(":S05:c5.1:c5.1")

    again = build_claim_evidence_gap_specs_from_probe(probe)
    assert [spec["task_id"] for spec in again] == [
        spec["task_id"] for spec in blocking
    ]

    wide = build_claim_evidence_gap_specs_from_probe(
        probe,
        include_nonblocking=True,
        include_medium=True,
    )
    assert {spec["record"]["component_id"] for spec in wide} == {
        "c5.1",
        "c5.2",
    }
    task, registry = _task_and_registry_from_spec(wide[0])
    assert task.gap_type == "claim_evidence_gap"
    assert registry.resolve(task.context_refs)


def test_probe_adapter_propagates_role_evidence_and_sibling_context() -> None:
    probe = {
        "schema_version": "blueprint_quality_probe.v1",
        "probe_timestamp": "2026-08-10T00:00:00+00:00",
        "section_id": "S07",
        "section_title": "Generic Section",
        "research_context": {"user_question": "generic question"},
        "final_claims": [
            {
                "claim_id": "c-fail",
                "component_id": "c-fail",
                "statement": "Failed statement.",
                "role": "load_bearing",
                "evidence_strength": "exact_quote",
                "parent_claim_id": "parent-1",
                "ready_for_write": False,
            },
            {
                "claim_id": "c-sib-ready",
                "statement": "Verified sibling statement.",
                "role": "supporting",
                "evidence_strength": "fulltext_quote",
                "parent_claim_id": "parent-1",
                "ready_for_write": True,
                "caveats": ["narrow scope"],
                "verified_quotes": [{
                    "quote": "verified quote text",
                    "paper_id": "paper-1",
                    "chunk_id": "chunk-1",
                    "title": "Source",
                }],
            },
            {
                "claim_id": "c-sib-quote",
                "statement": "Quote-only verified sibling.",
                "role": "supporting",
                "parent_claim_id": "parent-1",
                "ready_for_write": False,
                "bound_papers_and_quotes": [{
                    "evidence": "second quote",
                    "paper_id": "paper-2",
                    "chunk_id": "chunk-2",
                }],
            },
            {
                "claim_id": "c-sib-unverified",
                "statement": "Unverified sibling statement.",
                "parent_claim_id": "parent-1",
                "ready_for_write": True,
            },
            {
                "claim_id": "c-other",
                "statement": "Other parent verified.",
                "parent_claim_id": "parent-2",
                "ready_for_write": True,
                "verified_quotes": [{
                    "quote": "other quote",
                    "paper_id": "p9",
                    "chunk_id": "c9",
                }],
            },
        ],
        "evidence_gap_records": [
            {
                "claim_id": "c-fail",
                "component_id": "c-fail",
                "importance": "high",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit"],
            }
        ],
    }

    specs = build_claim_evidence_gap_specs_from_probe(probe)
    record = specs[0]["record"]

    assert record["claim_role"] == "load_bearing"
    assert record["evidence_strength"] == "exact_quote"
    assert record["parent_claim_id"] == "parent-1"
    context = record["sibling_verified_context"]
    assert [item["claim_id"] for item in context] == [
        "c-sib-ready",
        "c-sib-quote",
    ]
    first = context[0]
    assert first["statement"] == "Verified sibling statement."
    assert first["role"] == "supporting"
    assert first["evidence_strength"] == "fulltext_quote"
    assert first["caveats"] == ["narrow scope"]
    assert first["verified_quotes"] == [{
        "quote": "verified quote text",
        "paper_id": "paper-1",
        "chunk_id": "chunk-1",
        "title": "Source",
    }]
    assert context[1]["verified_quotes"][0]["quote"] == "second quote"
    assert all(
        item["claim_id"] != "c-sib-unverified" for item in context
    )
    assert all(item["claim_id"] != "c-other" for item in context)


def test_probe_adapter_sibling_context_empty_without_parent_fields() -> None:
    probe = {
        "schema_version": "blueprint_quality_probe.v1",
        "probe_timestamp": "2026-08-10T00:00:00+00:00",
        "section_id": "S05",
        "section_title": "Simulation Credibility",
        "research_context": {"user_question": "generic"},
        "final_claims": [{
            "claim_id": "c5.1",
            "statement": "Statement.",
            "role": "load_bearing",
        }],
        "evidence_gap_records": [
            {
                "claim_id": "c5.1",
                "component_id": "c5.1",
                "importance": "high",
                "disposition": "requires_new_evidence",
                "why_current_evidence_fails": "no exact quote",
                "missing_fact_units": ["unit"],
            }
        ],
    }

    specs = build_claim_evidence_gap_specs_from_probe(probe)
    record = specs[0]["record"]

    assert record["claim_role"] == "load_bearing"
    assert record["sibling_verified_context"] == []
