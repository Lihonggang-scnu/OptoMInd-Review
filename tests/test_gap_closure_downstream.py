"""Focused tests for the local S04 gap-closure downstream adapter."""

from __future__ import annotations

import copy
import json
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.gap_closure_downstream import (
    DISPOSITION_REVISE_BEFORE_WRITE,
    DISPOSITION_WRITE_READY,
    DISPOSITION_WRITE_WITH_LIMITS,
    SCHEMA_VERSION,
    STATUS_CLOSED,
    STATUS_FAILED,
    STATUS_IMPROVED_STOP,
    STATUS_NO_PROGRESS,
    STATUS_REVISION_REQUIRED,
    STATUS_STILL_OPEN,
    GapClosureDownstreamError,
    apply_gap_closure_to_report,
    build_gap_closure_writing_context,
    build_supplementary_evidence_packets,
    load_gap_closure_reports,
    merge_gap_closure_reports,
)


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (avoids pytest's restricted basetemp)."""
    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-gap-downstream"
    )
    root.mkdir(exist_ok=True)
    path = root / f"{request.node.name[:30]}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _per_target(
    target_id: str,
    *,
    progress: str | None = None,
    comments: list[str] | None = None,
    next_action: str | None = None,
    revised_claim: str | None = None,
) -> dict:
    item = {"target_id": target_id, "target_type": "claim"}
    if comments is not None:
        item["residual_reviewer_comments"] = list(comments)
    if progress is not None:
        item["progress"] = progress
        item["reason"] = f"test-{progress}"
    if next_action is not None:
        item["next_action"] = next_action
        item["reason"] = "test-revision"
        if revised_claim is not None:
            item["revised_claim"] = revised_claim
    return item


def _task(
    component_id: str,
    *,
    status: str = STATUS_IMPROVED_STOP,
    gap_type: str = "claim_evidence_gap",
    comments: list[str] | None = None,
    next_action: str | None = None,
    revised_claim: str | None = None,
    snapshot_path: str | None = None,
    task_id: str | None = None,
) -> dict:
    comments = list(comments or [])
    if status == STATUS_CLOSED:
        per_target = [_per_target(component_id, progress="closed", comments=[])]
    elif status == STATUS_IMPROVED_STOP:
        per_target = [
            _per_target(component_id, progress="improved", comments=comments)
        ]
    elif next_action:
        per_target = [
            _per_target(
                component_id,
                next_action=next_action,
                revised_claim=revised_claim,
                comments=comments,
            )
        ]
    else:
        per_target = [
            _per_target(component_id, progress="no_progress", comments=comments)
        ]
    return {
        "task_id": task_id or f"gap-{component_id}",
        "idempotency_key": f"gap:{component_id}",
        "component_id": component_id,
        "gap_type": gap_type,
        "status": status,
        "retrieval_wave_count": 1,
        "max_retrieval_waves": 1,
        "next_action": (
            next_action
            or (
                "closed"
                if status == STATUS_CLOSED
                else "stop_improved"
                if status == STATUS_IMPROVED_STOP
                else "revision_required"
            )
        ),
        "error": None,
        "per_target_results": per_target,
        "snapshot_path": snapshot_path,
    }


def _claim(claim_id: str, ready_for_write: bool = True) -> dict:
    return {
        "claim_id": claim_id,
        "statement": f"statement {claim_id}",
        "ready_for_write": ready_for_write,
    }


def _report(*tasks: dict, **extra: object) -> dict:
    payload = {
        "schema_version": "s04_claim_gap_closure_real.v1",
        "tasks": list(tasks),
    }
    payload.update(extra)
    return payload


def test_load_gap_closure_reports_path_and_mapping(tmp_path: Path) -> None:
    payload = _report(
        _task("c1.3", status=STATUS_IMPROVED_STOP, comments=["limit"])
    )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_gap_closure_reports([str(path), payload])

    assert len(loaded) == 2
    assert loaded[0]["source_report"] == str(path)
    assert loaded[0]["source_index"] == 0
    assert loaded[0]["payload"]["tasks"][0]["component_id"] == "c1.3"
    assert loaded[1]["source_report"] == "<mapping:2>"

    loaded[1]["payload"]["tasks"][0]["status"] = "mutated"
    assert payload["tasks"][0]["status"] == STATUS_IMPROVED_STOP


def test_load_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(GapClosureDownstreamError, match="cannot read"):
        load_gap_closure_reports(tmp_path / "missing.json")


def test_merge_later_reports_override_earlier_claims() -> None:
    earlier = _report(
        _task("c1.3", status=STATUS_IMPROVED_STOP, comments=["old limit"]),
        _task("c4.2", status=STATUS_FAILED),
    )
    later = _report(
        _task("c1.3", status=STATUS_CLOSED),
        _task(
            "c14.2",
            status=STATUS_REVISION_REQUIRED,
            next_action="delete",
            revised_claim="deleted c14.2",
            comments=["delete c14.2"],
        ),
    )

    merged = merge_gap_closure_reports([earlier, later])

    assert list(merged["claim_closures"]) == ["c1.3", "c14.2", "c4.2"]
    c1 = merged["claim_closures"]["c1.3"]
    assert c1["status"] == STATUS_CLOSED
    assert c1["disposition"] == DISPOSITION_WRITE_READY
    assert c1["source_report"] == "<mapping:2>"
    assert c1["reviewer_comments"] == []
    assert merged["claim_closures"]["c4.2"]["status"] == STATUS_FAILED
    assert merged["claim_closures"]["c4.2"]["disposition"] == (
        DISPOSITION_REVISE_BEFORE_WRITE
    )
    c14 = merged["claim_closures"]["c14.2"]
    assert c14["status"] == STATUS_REVISION_REQUIRED
    assert c14["revision_records"][0]["next_action"] == "delete"
    assert c14["revision_records"][0]["revised_claim"] == "deleted c14.2"
    assert merged["counts"]["claim_closure_count"] == 3


def test_within_report_later_task_overrides() -> None:
    report = _report(
        _task("c1.3", status=STATUS_IMPROVED_STOP, comments=["a"]),
        _task("c1.3", status=STATUS_CLOSED),
    )

    merged = merge_gap_closure_reports(report)

    assert merged["claim_closures"]["c1.3"]["status"] == STATUS_CLOSED
    assert merged["claim_closures"]["c1.3"]["writing_limits"] == []


def test_non_claim_gap_types_are_ignored_and_section_separated() -> None:
    report = _report(
        _task("c1.3", status=STATUS_CLOSED),
        _task(
            "s1",
            status=STATUS_IMPROVED_STOP,
            gap_type="section_argument_gap",
            comments=["section comment"],
        ),
        _task(
            "s1",
            status=STATUS_REVISION_REQUIRED,
            gap_type="review_structure_gap",
        ),
        _task(
            "s1",
            status=STATUS_REVISION_REQUIRED,
            gap_type="whole_review_gap",
        ),
        _task("s1", status=STATUS_REVISION_REQUIRED, gap_type="mystery_gap"),
        _task("s1", status=STATUS_REVISION_REQUIRED, gap_type=""),
    )

    merged = merge_gap_closure_reports(report)

    assert list(merged["claim_closures"]) == ["c1.3"]
    assert len(merged["section_gap_summaries"]) == 1
    section = merged["section_gap_summaries"][0]
    assert section["gap_type"] == "section_argument_gap"
    assert section["status"] == STATUS_IMPROVED_STOP
    assert len(merged["ignored_non_claim_records"]) == 4
    reasons = {item["reason"] for item in merged["ignored_non_claim_records"]}
    assert reasons == {
        "review_structure_gap_not_applied",
        "whole_review_gap_not_applied",
        "unsupported_gap_type",
        "missing_gap_type",
    }

    context = build_gap_closure_writing_context(report)
    assert context["eligible_claim_ids"] == ["c1.3"]
    assert context["counts"]["section_gap_count"] == 1
    assert context["counts"]["ignored_non_claim_count"] == 4
    assert "s1" not in context["claim_dispositions"]


@pytest.mark.parametrize(
    ("status", "disposition", "eligible"),
    [
        (STATUS_CLOSED, DISPOSITION_WRITE_READY, True),
        (STATUS_IMPROVED_STOP, DISPOSITION_WRITE_WITH_LIMITS, True),
        (STATUS_REVISION_REQUIRED, DISPOSITION_REVISE_BEFORE_WRITE, False),
        (STATUS_NO_PROGRESS, DISPOSITION_REVISE_BEFORE_WRITE, False),
        (STATUS_STILL_OPEN, DISPOSITION_REVISE_BEFORE_WRITE, False),
        (STATUS_FAILED, DISPOSITION_REVISE_BEFORE_WRITE, False),
        ("queued", DISPOSITION_REVISE_BEFORE_WRITE, False),
    ],
)
def test_status_dispositions(
    status: str, disposition: str, eligible: bool
) -> None:
    report = _report(
        _task("c1.3", status=status, comments=["keep"])
    )

    context = build_gap_closure_writing_context(report)
    record = context["claim_dispositions"]["c1.3"]

    assert record["status"] == status
    assert record["disposition"] == disposition
    assert record["eligible"] is eligible
    if status == STATUS_IMPROVED_STOP:
        assert record["writing_limits"] == ["keep"]
        assert record["reviewer_comments"] == ["keep"]
    else:
        assert record["writing_limits"] == []
    if eligible:
        assert "c1.3" in context["eligible_claim_ids"]
    else:
        assert "c1.3" in context["revise_before_write_claim_ids"]


@pytest.mark.parametrize("outer_status", [STATUS_CLOSED, STATUS_IMPROVED_STOP])
def test_task_level_delete_overrides_optimistic_outer_status(
    outer_status: str,
) -> None:
    report = _report(
        _task(
            "c1.3",
            status=outer_status,
            next_action="delete",
            comments=["delete claim"],
        )
    )

    context = build_gap_closure_writing_context(report)
    record = context["claim_dispositions"]["c1.3"]

    assert record["status"] == outer_status
    assert record["disposition"] == DISPOSITION_REVISE_BEFORE_WRITE
    assert record["eligible"] is False
    assert record["next_action"] == "delete"
    assert record["writing_limits"] == []
    assert "c1.3" in context["revise_before_write_claim_ids"]
    assert "c1.3" not in context["eligible_claim_ids"]


def test_per_target_delete_overrides_improved_stop_and_excludes_from_writing() -> None:
    task = _task(
        "c1.3",
        status=STATUS_IMPROVED_STOP,
        comments=["delete claim"],
    )
    task["per_target_results"][0]["next_action"] = "delete"

    context = build_gap_closure_writing_context(_report(task))
    record = context["claim_dispositions"]["c1.3"]

    assert record["status"] == STATUS_IMPROVED_STOP
    assert record["disposition"] == DISPOSITION_REVISE_BEFORE_WRITE
    assert record["eligible"] is False
    assert record["next_action"] == "delete"
    assert record["revision_records"][0]["next_action"] == "delete"
    assert record["writing_limits"] == []
    assert "c1.3" in context["revise_before_write_claim_ids"]

    applied = apply_gap_closure_to_report(
        {"final_claims": [_claim("c1.3", ready_for_write=True)]},
        context,
    )
    claim = applied["final_claims"][0]
    assert claim["ready_for_write"] is False
    assert claim["gap_closure"]["eligible"] is False
    assert claim["gap_closure"]["disposition"] == DISPOSITION_REVISE_BEFORE_WRITE


def test_revision_results_delete_overrides_improved_stop() -> None:
    task = _task("c1.3", status=STATUS_IMPROVED_STOP)
    task["revision"] = {
        "reason": "improved_stop_local_revision",
        "results": [{
            "target_id": "c1.3",
            "next_action": "delete",
        }],
    }

    record = build_gap_closure_writing_context(_report(task))[
        "claim_dispositions"
    ]["c1.3"]

    assert record["disposition"] == DISPOSITION_REVISE_BEFORE_WRITE
    assert record["eligible"] is False
    assert record["next_action"] == "delete"
    assert record["revision_records"][0]["next_action"] == "delete"


def test_delete_wins_over_non_delete_per_target_action() -> None:
    task = _task(
        "c1.3",
        status=STATUS_IMPROVED_STOP,
        next_action="delete",
    )
    task["per_target_results"][0]["next_action"] = "narrow"

    record = build_gap_closure_writing_context(_report(task))[
        "claim_dispositions"
    ]["c1.3"]

    assert record["next_action"] == "delete"
    assert record["disposition"] == DISPOSITION_REVISE_BEFORE_WRITE
    assert record["eligible"] is False


def test_improved_stop_with_non_delete_action_stays_write_with_limits() -> None:
    task = _task(
        "c1.3",
        status=STATUS_IMPROVED_STOP,
        comments=["keep limit"],
    )
    task["per_target_results"][0]["next_action"] = "narrow"

    context = build_gap_closure_writing_context(_report(task))
    record = context["claim_dispositions"]["c1.3"]

    assert record["disposition"] == DISPOSITION_WRITE_WITH_LIMITS
    assert record["eligible"] is True
    assert record["next_action"] == "stop_improved"
    assert "keep limit" in record["writing_limits"]
    assert "revision:narrow" in record["writing_limits"]
    assert "c1.3" in context["eligible_claim_ids"]
    assert "c1.3" in context["write_with_limits_claim_ids"]


def test_apply_gap_closure_to_report_annotates_without_mutation() -> None:
    input_report = {
        "schema_version": "probe.v19",
        "final_claims": [
            _claim("c1.3", ready_for_write=True),
            _claim("c2.2", ready_for_write=True),
            _claim("c3.1", ready_for_write=True),
        ],
    }
    snapshot = "F:/cache/snapshot-0001"
    closures = merge_gap_closure_reports(
        [
            _report(
                _task(
                    "c1.3",
                    status=STATUS_IMPROVED_STOP,
                    comments=["limit one"],
                    snapshot_path=snapshot,
                ),
                _task(
                    "c2.2",
                    status=STATUS_REVISION_REQUIRED,
                    next_action="delete",
                    revised_claim="delete c2.2",
                    comments=["revise c2.2"],
                ),
            )
        ]
    )
    before = copy.deepcopy(input_report)

    applied = apply_gap_closure_to_report(input_report, closures)

    assert input_report == before
    by_id = {claim["claim_id"]: claim for claim in applied["final_claims"]}
    assert by_id["c1.3"]["ready_for_write"] is True
    assert by_id["c1.3"]["gap_closure"]["disposition"] == (
        DISPOSITION_WRITE_WITH_LIMITS
    )
    assert by_id["c1.3"]["gap_closure"]["writing_limits"] == ["limit one"]
    assert by_id["c1.3"]["gap_closure"]["snapshot_path"] == snapshot
    assert by_id["c2.2"]["ready_for_write"] is False
    assert by_id["c2.2"]["gap_closure"]["disposition"] == (
        DISPOSITION_REVISE_BEFORE_WRITE
    )
    assert by_id["c2.2"]["gap_closure"]["revision_records"][0][
        "next_action"
    ] == "delete"
    assert by_id["c3.1"]["ready_for_write"] is True
    assert "gap_closure" not in by_id["c3.1"]
    json.dumps(applied, ensure_ascii=False, sort_keys=True)


def test_apply_accepts_merged_list_and_context() -> None:
    input_report = {"final_claims": [_claim("c1.3"), _claim("c2.2")]}
    records = [
        {
            "claim_id": "c1.3",
            "component_id": "c1.3",
            "status": STATUS_CLOSED,
            "disposition": DISPOSITION_WRITE_READY,
            "eligible": True,
            "writing_limits": [],
            "reviewer_comments": [],
            "revision_records": [],
            "author_revision_records": [],
        },
        {
            "claim_id": "c2.2",
            "component_id": "c2.2",
            "status": STATUS_REVISION_REQUIRED,
            "disposition": DISPOSITION_REVISE_BEFORE_WRITE,
            "eligible": False,
            "writing_limits": [],
            "reviewer_comments": ["revise"],
            "revision_records": [{"next_action": "qualify"}],
            "author_revision_records": [],
        },
    ]
    merged = {
        "schema_version": SCHEMA_VERSION,
        "claim_closures": {record["claim_id"]: record for record in records},
    }
    context = build_gap_closure_writing_context(merged)

    for closures in (merged, records, context):
        applied = apply_gap_closure_to_report(input_report, closures)
        assert applied["final_claims"][0]["gap_closure"]["claim_id"] == "c1.3"
        assert applied["final_claims"][0]["ready_for_write"] is True
        assert applied["final_claims"][1]["ready_for_write"] is False


def test_deterministic_output_and_comment_dedupe() -> None:
    report = _report(
        _task(
            "c1.3",
            status=STATUS_IMPROVED_STOP,
            comments=["  limit   one ", "LIMIT ONE", "second"],
        ),
        _task(
            "c2.2",
            status=STATUS_REVISION_REQUIRED,
            next_action="qualify",
            revised_claim="qualify c2.2",
            comments=["revise", " revise "],
        ),
    )

    first = build_gap_closure_writing_context(report)
    second = build_gap_closure_writing_context(report)

    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    record = first["claim_dispositions"]["c1.3"]
    assert record["reviewer_comments"] == ["  limit   one ", "second"]
    assert record["writing_limits"] == ["  limit   one ", "second"]
    assert first["claim_dispositions"]["c2.2"]["reviewer_comments"] == ["revise"]
    assert first["revision_records"]["c2.2"][0]["revised_claim"] == "qualify c2.2"


def test_empty_and_no_report_behavior() -> None:
    assert load_gap_closure_reports(None) == []

    merged = merge_gap_closure_reports([])
    assert merged["claim_closures"] == {}
    assert merged["counts"]["claim_closure_count"] == 0

    context = build_gap_closure_writing_context(None)
    assert context["claim_dispositions"] == {}
    assert context["eligible_claim_ids"] == []
    assert context["revise_before_write_claim_ids"] == []
    assert context["counts"]["report_count"] == 0
    assert context["counts"]["claim_closure_count"] == 0

    no_tasks = build_gap_closure_writing_context(
        {"schema_version": "x", "output_snapshot": "snap"}
    )
    assert no_tasks["counts"]["claim_closure_count"] == 0

    applied = apply_gap_closure_to_report({"final_claims": []}, None)
    assert applied == {"final_claims": []}


def test_section_report_without_tasks_creates_summary() -> None:
    section_report = {
        "schema_version": "s04_section_argument_gap_real.v1",
        "outcome": "no_actionable_section_gap",
        "actionable_section_gap": False,
        "unresolved_evidence_gap_count": 6,
        "missing_roles": [],
        "candidate_claims": [],
        "candidate_claim_audit": {"proposed_candidate_count": 0},
        "output_snapshot": None,
        "never_invoked": ["review_structure_gap", "whole_review_gap"],
    }

    context = build_gap_closure_writing_context([section_report])

    assert len(context["section_gap_summary"]) == 1
    summary = context["section_gap_summary"][0]
    assert summary["outcome"] == "no_actionable_section_gap"
    assert summary["actionable_section_gap"] is False
    assert summary["never_invoked"] == ["review_structure_gap", "whole_review_gap"]
    assert context["claim_dispositions"] == {}
    assert context["counts"]["section_gap_count"] == 1


def test_whole_report_review_structure_gap_is_ignored() -> None:
    report = {
        "schema_version": "x",
        "gap_type": "whole_review_gap",
        "status": STATUS_REVISION_REQUIRED,
    }

    merged = merge_gap_closure_reports(report)

    assert merged["claim_closures"] == {}
    assert len(merged["ignored_non_claim_records"]) == 1
    assert (
        merged["ignored_non_claim_records"][0]["reason"]
        == "whole_review_gap_not_applied"
    )


def test_claim_record_without_target_id_is_audited() -> None:
    task = {
        "task_id": "gap-x",
        "gap_type": "claim_evidence_gap",
        "status": STATUS_CLOSED,
        "per_target_results": [],
    }

    merged = merge_gap_closure_reports(_report(task))

    assert merged["claim_closures"] == {}
    assert (
        merged["ignored_non_claim_records"][0]["reason"]
        == "claim_record_missing_target_id"
    )


def test_non_list_tasks_raises() -> None:
    with pytest.raises(GapClosureDownstreamError, match="non-list"):
        merge_gap_closure_reports({"tasks": {"bad": True}})


def test_author_revision_records_preserved() -> None:
    task = _task(
        "c1.3",
        status=STATUS_IMPROVED_STOP,
        comments=["c"],
    )
    task["record"] = {
        "author_revision_suggestion": "narrow to forward modelling",
        "reviewer_feedback": {
            "residual_reviewer_comments": ["c", "another"]
        },
    }

    context = build_gap_closure_writing_context(_report(task))
    record = context["claim_dispositions"]["c1.3"]

    assert record["author_revision_records"] == [
        {"author_revision_suggestion": "narrow to forward modelling"}
    ]
    assert record["reviewer_comments"] == ["c", "another"]
    assert "author_revision_suggestion:narrow to forward modelling" in (
        record["writing_limits"]
    )


def test_apply_gap_closure_applies_revised_claim_only_when_eligible() -> None:
    report = {
        "final_claims": [
            {"claim_id": "c1", "statement": "Original statement."},
            {"claim_id": "c2", "statement": "Keep statement."},
        ]
    }
    tasks = [
        {
            "component_id": "c1",
            "claim_id": "c1",
            "gap_type": "claim_evidence_gap",
            "status": STATUS_IMPROVED_STOP,
            "retrieval_wave_count": 1,
            "max_retrieval_waves": 1,
            "task_id": "task-c1",
            "snapshot_path": "snapshot-v1",
            "per_target_results": [{
                "target_id": "c1",
                "revised_claim": "Narrowed statement.",
                "next_action": "narrow",
            }],
        },
        {
            "component_id": "c2",
            "claim_id": "c2",
            "gap_type": "claim_evidence_gap",
            "status": STATUS_REVISION_REQUIRED,
            "retrieval_wave_count": 1,
            "max_retrieval_waves": 1,
            "task_id": "task-c2",
            "snapshot_path": "snapshot-v1",
            "per_target_results": [{
                "target_id": "c2",
                "revised_claim": "Must not apply.",
                "next_action": "narrow",
            }],
        },
    ]
    context = build_gap_closure_writing_context({"tasks": tasks})

    revised = apply_gap_closure_to_report(report, context)

    assert revised["final_claims"][0]["statement"] == "Narrowed statement."
    assert revised["final_claims"][0]["original_statement"] == (
        "Original statement."
    )
    assert revised["final_claims"][0]["statement_revision_source"][
        "task_id"
    ] == "task-c1"
    assert revised["final_claims"][1]["statement"] == "Keep statement."


def _supplementary_unit(
    unit_id: str,
    raw_text: str,
    *,
    task_id: str = "task-c1",
    source_kind: str = "s2_body",
    content_depth: str = "fulltext",
    paper_id: str = "paper-1",
    chunk_id: str = "",
) -> dict:
    return {
        "unit_id": unit_id,
        "identity": {
            "paper_id": paper_id,
            "chunk_id": chunk_id or f"chunk-{unit_id}",
            "title": "Source title",
        },
        "durable_content": {
            "raw_text": raw_text,
            "content_depth": content_depth,
        },
        "durable_content_card": {
            "content_quality": {"source_kind": source_kind}
        },
        "query_annotations": [
            {"supplementary_task_references": [{"task_id": task_id}]}
        ],
    }


def test_build_supplementary_evidence_packets_exact_and_task_referenced() -> None:
    closure = {
        "eligible": True,
        "task_id": "task-c1",
        "component_id": "c1",
        "per_target_results": [
            {
                "target_id": "c1",
                "locally_validated_quotes": [
                    {"unit_id": "u1", "quote": "exact phrase"},
                    {"unit_id": "u3", "quote": "missing quote"},
                ],
                "exact_quote_matches": {
                    "phrase two": ["u2"],
                    "phrase four": ["u4"],
                    "abstract phrase": ["u-abstract"],
                    "missing phrase": ["u-missing"],
                },
            },
            {
                "target_id": "other-claim",
                "locally_validated_quotes": [
                    {"unit_id": "u1", "quote": "cross-claim quote"}
                ],
            },
        ],
    }
    units = [
        _supplementary_unit("u1", "prefix exact phrase suffix"),
        _supplementary_unit(
            "u2",
            "phrase two here",
            source_kind="public_oa_fulltext",
        ),
        _supplementary_unit("u3", "wrong text"),
        _supplementary_unit(
            "u4",
            "phrase four here",
            task_id="other-task",
        ),
        _supplementary_unit(
            "u-abstract",
            "abstract phrase content",
            source_kind="abstract",
            content_depth="abstract",
        ),
    ]

    packets, audit = build_supplementary_evidence_packets(
        closure,
        units,
        claim_id="c1",
    )

    by_unit = {packet["chunk_id"]: packet for packet in packets}
    assert by_unit["chunk-u1"]["exact_spans"] == ["exact phrase"]
    assert by_unit["chunk-u1"]["evidence_level"] == "fulltext"
    assert by_unit["chunk-u2"]["evidence_level"] == "fulltext"
    abstract = [p for p in packets if p["source_kind"] == "abstract"]
    assert len(abstract) == 1
    assert abstract[0]["evidence_level"] == "abstract"
    assert abstract[0]["retrieval_role"] == "supplementary_background_only"
    rejected_reasons = {item["reason"] for item in audit["rejected"]}
    assert "quote_not_found" in rejected_reasons
    assert "task_reference_mismatch" in rejected_reasons
    assert "missing_unit" in rejected_reasons
    assert audit["admitted_packet_count"] == 3
    assert audit["candidate_count"] == 6


def test_build_supplementary_evidence_packets_summary_and_metadata_weak() -> None:
    closure = {
        "eligible": True,
        "task_id": "task-c1",
        "component_id": "c1",
        "per_target_results": [
            {
                "target_id": "c1",
                "exact_quote_matches": {
                    "tldr phrase": ["u-tldr"],
                    "summary phrase": ["u-summary"],
                    "metadata phrase": ["u-metadata"],
                    "s2 body phrase": ["u-body"],
                    "oa phrase": ["u-oa"],
                },
            }
        ],
    }
    units = [
        _supplementary_unit(
            "u-tldr",
            "tldr phrase content",
            source_kind="s2_tldr",
            content_depth="tldr",
        ),
        _supplementary_unit(
            "u-summary",
            "summary phrase content",
            source_kind="summary",
            content_depth="summary",
        ),
        _supplementary_unit(
            "u-metadata",
            "metadata phrase content",
            source_kind="metadata",
            content_depth="metadata",
        ),
        _supplementary_unit(
            "u-body",
            "s2 body phrase content",
            source_kind="s2_body_snippet",
            content_depth="structured_snippet",
        ),
        _supplementary_unit(
            "u-oa",
            "oa phrase content",
            source_kind="public_oa_fulltext",
            content_depth="fulltext",
        ),
    ]

    packets, audit = build_supplementary_evidence_packets(
        closure,
        units,
        claim_id="c1",
    )

    by_chunk = {packet["chunk_id"]: packet for packet in packets}
    for chunk_id in ("chunk-u-tldr", "chunk-u-summary", "chunk-u-metadata"):
        packet = by_chunk[chunk_id]
        assert packet["evidence_level"] == "abstract"
        assert packet["retrieval_role"] == "supplementary_background_only"
        assert packet["support_relation"] == "background_support"
        assert packet["limitations"]
    assert by_chunk["chunk-u-body"]["evidence_level"] == "fulltext"
    assert by_chunk["chunk-u-body"][
        "retrieval_role"
    ] == "supplementary_evidence_candidate"
    assert by_chunk["chunk-u-oa"]["evidence_level"] == "fulltext"
    assert by_chunk["chunk-u-oa"][
        "retrieval_role"
    ] == "supplementary_evidence_candidate"
    assert audit["admitted_packet_count"] == 5


def test_build_supplementary_evidence_packets_not_eligible() -> None:
    packets, audit = build_supplementary_evidence_packets(
        {"eligible": False},
        [_supplementary_unit("u1", "text")],
        claim_id="c1",
    )
    assert packets == []
    assert audit["status"] == "not_eligible"
