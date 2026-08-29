"""Focused tests for the supplementary claim revision pipeline."""

from __future__ import annotations

import copy
import json
from typing import Any, Callable, Mapping

from optomind_research.runtime.gap_closure_downstream import (
    merge_gap_closure_reports,
)
from optomind_research.runtime.supplementary_claim_revision import (
    DEFAULT_MODEL_TIER,
    MAX_PASSES,
    SCHEMA_VERSION,
    apply_revision_outcomes_to_probe,
    build_claim_revision_dossiers,
    run_supplementary_claim_revision,
    resolve_blueprint_placement,
    validate_evidence_selections,
)
from optomind_research.runtime.supplementary_gap_closure import (
    v19_claim_evidence_gap_job_specs,
)


TARGETS = (
    ("c1.3", "PINNs enable dataset-free inverse design.", "load_bearing", "c1"),
    ("c2.2", "This approach generalizes the adjoint method.", "load_bearing", "c2"),
    ("c4.2", "Claim c4.2 statement.", "counterevidence", "c4"),
    ("c5.3", "Claim c5.3 statement.", "supporting", "c5"),
    ("c10.2", "Claim c10.2 statement.", "supporting", "c10"),
    ("c14.2", "Claim c14.2 statement.", "supporting", "c14"),
)


def _claim(
    claim_id: str,
    statement: str,
    role: str,
    parent: str,
    *,
    ready: bool = False,
) -> dict[str, Any]:
    components = [
        {
            "component_id": claim_id,
            "statement": statement,
            "support_assessment": "partial",
            "reason": "original verification",
            "bindings": [],
        }
    ]
    return {
        "claim_id": claim_id,
        "statement": statement,
        "role": role,
        "parent_claim_id": parent,
        "candidate_chunk_ids": [],
        "ready_for_write": ready,
        "quote_verified": False,
        "verified_quote": None,
        "verified_quotes": [],
        "permission": "qualified_only",
        "qualified_support_only": True,
        "qualified_wording_present": True,
        "caveats": [],
        "_rejected_chunk_ids": [],
        "claim_components": copy.deepcopy(components),
        "component_verification": copy.deepcopy(components),
    }


def _gap_record(claim_id: str, statement: str) -> dict[str, Any]:
    original_raw = ""
    if claim_id == "c1.3":
        original_raw = (
            "PINNs eliminate the need for mesh generation and labelled data "
            "for forward modelling."
        )
    if claim_id == "c2.2":
        original_raw = (
            "The approach generalizes the adjoint method to "
            "adjoint-enabled topology optimization."
        )
    summary = []
    if original_raw:
        summary.append(
            {
                "chunk_id": f"chunk-orig-{claim_id}",
                "paper_id": f"paper-orig-{claim_id}",
                "title": f"Original paper {claim_id}",
                "permission": "contextual_or_qualified_support",
                "source_kind": "fulltext",
                "raw_text": original_raw,
            }
        )
    return {
        "claim_id": claim_id,
        "component_id": claim_id,
        "disposition": "requires_new_evidence",
        "missing_fact_units": [statement],
        "why_current_evidence_fails": f"Evidence is incomplete for {claim_id}.",
        "required_revision_or_qualification": (
            "Narrow the claim to what the supplied evidence supports."
        ),
        "current_evidence_summary": summary,
    }


def _probe() -> dict[str, Any]:
    claims = [
        _claim("c1.1", "PINNs embed physics into loss functions.", "load_bearing", "c1", ready=True),
        _claim("c1.2", "PINNs avoid mesh generation.", "load_bearing", "c1", ready=True),
        _claim("c2.1", "Differentiable solvers give exact gradients.", "load_bearing", "c2", ready=True),
        _claim("c2.3", "Sibling adjoint claim.", "load_bearing", "c2", ready=True),
    ]
    for claim_id, statement, role, parent in TARGETS:
        claims.append(_claim(claim_id, statement, role, parent))
    records = [
        _gap_record(claim_id, statement)
        for claim_id, statement, _role, _parent in TARGETS
    ]
    blueprint = {
        "section_title": "The Credibility Gap",
        "argument_arc": [
            {
                "step": "Establish the fundamental trade-off.",
                "claim_ids": ["c1.1", "c1.2"],
            },
            {
                "step": "Analyze adjoint-based optimization.",
                "claim_ids": ["c2.1"],
            },
        ],
        "subsection_blueprint": [
            {
                "title": "Mechanisms",
                "purpose": "Contrast mechanisms.",
                "claim_ids": ["c1.1", "c1.2"],
                "transition_logic": "",
            }
        ],
        "claim_usage": {
            "adopted_claim_ids": ["c1.1", "c1.2", "c2.1", "c2.3"],
            "reserved_claims": [],
        },
        "section_mission": {
            "statement": "Compare credibility.",
            "claim_ids": ["c1.1"],
        },
        "central_judgement": {"statement": "Bounded.", "claim_ids": []},
        "title_rationale": {"statement": "Title.", "claim_ids": []},
        "user_axis_coverage": [
            {"axis": "A", "answer": "B", "claim_ids": []}
        ],
        "remaining_uncertainties": [],
    }
    return {
        "schema_version": "probe.v19",
        "probe_timestamp": "2026-08-09T10:03:07",
        "section_id": "S04",
        "section_title": "The Credibility Gap",
        "research_context": {"user_question": "How credible are the methods?"},
        "final_claims": claims,
        "evidence_gap_records": records,
        "verified_blueprint": blueprint,
        "write_gate": {
            "allowed_to_write": True,
            "ready_claim_count": 4,
            "all_load_bearing_claims_ready": True,
            "blueprint_claim_audit": {"ready": True},
            "all_materials_actually_read": True,
            "final_source_grounded_blueprint_audit_ready": True,
            "final_source_grounded_blueprint_hard_blocks_absent": True,
        },
        "claim_scope_contracts": [],
    }


def _closure_task(
    claim_id: str,
    *,
    status: str = "improved_stop",
    comments: list[str] | None = None,
) -> dict[str, Any]:
    per_target: dict[str, Any] = {
        "target_id": claim_id,
        "target_type": "claim",
        "residual_reviewer_comments": list(comments or []),
    }
    if status == "closed":
        per_target["progress"] = "closed"
    elif status == "improved_stop":
        per_target["progress"] = "improved"
    else:
        per_target["progress"] = "no_progress"
    return {
        "task_id": f"gap-{claim_id}",
        "component_id": claim_id,
        "gap_type": "claim_evidence_gap",
        "status": status,
        "next_action": "",
        "per_target_results": [per_target],
        "snapshot_path": f"cache/snapshot-{claim_id}",
    }


def _closure_report(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "s04_claim_gap_closure_real.v1",
        "tasks": list(tasks),
    }


def _unit(
    unit_id: str,
    chunk_id: str,
    paper_id: str,
    raw_text: str,
    *,
    depth: str = "fulltext",
    kind: str = "fulltext",
    permission: str = "factual_support",
    task_id: str = "",
) -> dict[str, Any]:
    unit: dict[str, Any] = {
        "unit_id": unit_id,
        "unit_kind": "text_chunk",
        "identity": {
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "doi": f"doi-{chunk_id}",
            "title": f"Title {chunk_id}",
        },
        "durable_content": {
            "raw_text": raw_text,
            "content_depth": depth,
        },
        "durable_content_card": {
            "content_quality": {
                "source_kind": kind,
                "evidence_ceiling": permission,
            }
        },
        "query_annotations": [],
    }
    if task_id:
        unit["query_annotations"] = [
            {"supplementary_task_references": [{"task_id": task_id}]}
        ]
    return unit


def _specs(probe: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        spec["claim_id"]: spec
        for spec in v19_claim_evidence_gap_job_specs(probe)
    }


def _delete_author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": "delete",
        "revised_claim": "",
        "evidence_selections": [],
        "rationale": "No supported revision from supplied evidence.",
    }


def _pass_reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "pass",
        "reason": "Supported by supplied evidence.",
        "corrected_claim": "",
        "evidence_selections": [],
    }


def _callbacks(
    *,
    c13_author: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    c13_reviewer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    default_author: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> tuple[
    Callable[[Mapping[str, Any]], Mapping[str, Any]],
    Callable[[Mapping[str, Any]], Mapping[str, Any]],
    dict[str, list[Mapping[str, Any]]],
]:
    calls: dict[str, list[Mapping[str, Any]]] = {"author": [], "reviewer": []}

    def author(author_input: Mapping[str, Any]) -> Mapping[str, Any]:
        calls["author"].append(author_input)
        claim_id = author_input["dossier"]["claim_id"]
        if claim_id == "c1.3" and c13_author is not None:
            output = dict(c13_author(author_input))
        else:
            output = dict((default_author or _delete_author)(author_input))
        if usage:
            output["_llm_usage"] = dict(usage)
        return output

    def reviewer(reviewer_input: Mapping[str, Any]) -> Mapping[str, Any]:
        calls["reviewer"].append(reviewer_input)
        claim_id = reviewer_input["dossier"]["claim_id"]
        if claim_id == "c1.3" and c13_reviewer is not None:
            output = dict(c13_reviewer(reviewer_input))
        else:
            output = dict(_pass_reviewer(reviewer_input))
        if usage:
            output["_llm_usage"] = dict(usage)
        return output

    return author, reviewer, calls


def _run(
    *,
    probe: Mapping[str, Any],
    closure_reports: Any = None,
    baseline_units: list[dict[str, Any]] | None = None,
    snapshot_units: list[dict[str, Any]] | None = None,
    author_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    reviewer_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    c13_author: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    c13_reviewer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    default_author: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, list[Mapping[str, Any]]]]:
    calls: dict[str, list[Mapping[str, Any]]] = {
        "author": [],
        "reviewer": [],
    }
    if author_callback is None or reviewer_callback is None:
        default_author, default_reviewer, default_calls = _callbacks(
            c13_author=c13_author,
            c13_reviewer=c13_reviewer,
            default_author=default_author,
            usage=usage,
        )
        author_callback = author_callback or default_author
        reviewer_callback = reviewer_callback or default_reviewer
        calls = default_calls
    result = run_supplementary_claim_revision(
        probe_report=probe,
        closure_reports=closure_reports,
        baseline_units=baseline_units or [],
        snapshot_units=snapshot_units or [],
        author_callback=author_callback,
        reviewer_callback=reviewer_callback,
    )
    return result, calls


def _new_unit_for(
    probe: Mapping[str, Any],
    claim_id: str,
    *,
    raw_text: str,
    unit_id: str = "unit:new",
    chunk_id: str = "chunk:new",
    paper_id: str = "paper:new",
    depth: str = "fulltext",
    kind: str = "fulltext",
    permission: str = "factual_support",
) -> dict[str, Any]:
    task_id = _specs(probe)[claim_id]["task_id"]
    return _unit(
        unit_id,
        chunk_id,
        paper_id,
        raw_text,
        depth=depth,
        kind=kind,
        permission=permission,
        task_id=task_id,
    )


def test_improved_stop_is_not_auto_passing() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task(
            "c1.3",
            status="improved_stop",
            comments=["Candidate progress but claim is not fully supported."],
        )
    )

    def revise_reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "revise",
            "reason": "Keep revising.",
            "corrected_claim": "",
            "evidence_selections": [],
        }

    def small_author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        dossier = author_input["dossier"]
        unit = next(
            item
            for item in dossier["evidence_units"]
            if item["origin"] == "incremental"
        )
        quote = "PINN inverse design is competitive"
        return {
            "action": "small_revision",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {"unit_id": unit["unit_id"], "chunk_id": unit["chunk_id"], "quote": quote}
            ],
            "rationale": "Small revision bounded to the selected quote.",
        }

    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]
    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=small_author,
        c13_reviewer=revise_reviewer,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "no_progress"
    assert outcome["ready_for_write"] is False
    assert len(calls["author"]) >= 1
    assert len(calls["reviewer"]) == 1
    claim = result["revised_probe_report"]["final_claims"]
    by_id = {item["claim_id"]: item for item in claim}
    assert by_id["c1.3"]["ready_for_write"] is False
    assert by_id["c1.3"]["supplementary_revision"]["final_status"] == "no_progress"
    dossier = result["dossiers"][0]
    assert dossier["claim_id"] == "c1.3"
    assert dossier["revision_mode"] == "small_revision"


def test_successful_small_revision_with_exact_new_quote() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task(
            "c1.3",
            status="improved_stop",
            comments=["Candidate progress."],
        )
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]
    selection = {
        "unit_id": snapshot[0]["unit_id"],
        "chunk_id": snapshot[0]["identity"]["chunk_id"],
        "quote": quote,
    }

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "action": "small_revision",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [selection],
            "rationale": "Exact quote supports competitive inverse design.",
        }

    def reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "pass",
            "reason": "The revised claim is supported by the selected quote.",
            "corrected_claim": (
                "The reviewed study reports that PINN inverse design is competitive."
            ),
            "evidence_selections": [selection],
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
        c13_reviewer=reviewer,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "passed"
    assert outcome["ready_for_write"] is True
    assert outcome["final_claim_text"] == (
        "The reviewed study reports that PINN inverse design is competitive."
    )
    assert outcome["permission"] == "factual_support"
    assert len(calls["author"]) == 6  # five deletes + one small revision
    assert len(calls["reviewer"]) == 1

    revised = result["revised_probe_report"]
    claim = next(
        item for item in revised["final_claims"] if item["claim_id"] == "c1.3"
    )
    assert claim["ready_for_write"] is True
    assert claim["statement"] == outcome["final_claim_text"]
    assert claim["candidate_chunk_ids"] == [snapshot[0]["identity"]["chunk_id"]]
    assert claim["verified_quotes"] == [
        {"chunk_id": snapshot[0]["identity"]["chunk_id"], "quote": quote}
    ]
    assert claim["quote_verified"] is True
    assert claim["permission"] == "factual_support"
    assert any("supplementary claim revision" in caveat for caveat in claim["caveats"])
    component = claim["claim_components"][0]
    assert component["statement"] == outcome["final_claim_text"]
    assert component["bindings"][0]["verbatim_quote"] == quote
    verification = claim["component_verification"][0]
    assert verification["ready"] is True
    assert verification["quote_exact"] is True

    contract = next(
        item
        for item in revised["claim_scope_contracts"]
        if item["claim_id"] == "c1.3"
    )
    assert contract["verified_statement"] == outcome["final_claim_text"]
    source = contract["source_envelope"]["sources"][0]
    assert source["chunk_id"] == snapshot[0]["identity"]["chunk_id"]
    assert source["paper_id"] == snapshot[0]["identity"]["paper_id"]
    assert source["use_permission"] == "factual_support"
    assert contract["source_envelope"]["paper_ids"] == [
        snapshot[0]["identity"]["paper_id"]
    ]

    arc = revised["verified_blueprint"]["argument_arc"][0]
    assert arc["step"] == "Establish the fundamental trade-off."
    assert "c1.3" in arc["claim_ids"]
    assert "c1.3" in revised["verified_blueprint"]["subsection_blueprint"][0]["claim_ids"]
    assert "c1.3" in revised["verified_blueprint"]["claim_usage"]["adopted_claim_ids"]
    mission = revised["verified_blueprint"]["section_mission"]["claim_ids"]
    judgement = revised["verified_blueprint"]["central_judgement"]["claim_ids"]
    rationale = revised["verified_blueprint"]["title_rationale"]["claim_ids"]
    axes = [
        str(value)
        for row in revised["verified_blueprint"]["user_axis_coverage"]
        for value in row.get("claim_ids") or []
    ]
    assert "c1.3" not in mission
    assert "c1.3" not in judgement
    assert "c1.3" not in rationale
    assert "c1.3" not in axes


def test_no_progress_narrowing_with_original_evidence() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c2.2", status="revision_required")
    )
    quote = "generalizes the adjoint method to adjoint-enabled topology optimization"
    dossier_by_id = {
        dossier["claim_id"]: dossier
        for dossier in build_claim_revision_dossiers(probe, closure_reports=closure)
    }
    original_unit = next(
        unit
        for unit in dossier_by_id["c2.2"]["evidence_units"]
        if unit["origin"] == "original_evidence"
    )

    def author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        if author_input["dossier"]["claim_id"] != "c2.2":
            return _delete_author(author_input)
        return {
            "action": "narrow",
            "revised_claim": (
                "The approach generalizes the adjoint method to "
                "adjoint-enabled topology optimization."
            ),
            "evidence_selections": [
                {
                    "unit_id": original_unit["unit_id"],
                    "chunk_id": original_unit["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Narrowed to the original supplied evidence.",
            "target_blueprint_placement": {
                "argument_arc": "argument_arc[1].claim_ids",
                "subsection_blueprint": "subsection_blueprint[0].claim_ids",
            },
        }

    def reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "pass",
            "reason": "Supported by the supplied original evidence.",
            "corrected_claim": "",
            "evidence_selections": [],
        }

    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        author_callback=author,
        reviewer_callback=reviewer,
    )

    outcome = result["outcomes"]["c2.2"]
    assert outcome["final_status"] == "passed"
    assert outcome["ready_for_write"] is True
    assert outcome["permission"] == "qualified_only"
    assert outcome["final_selections"][0]["unit_id"] == original_unit["unit_id"]


def test_delete_produces_no_ready_text() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    before_blueprint = copy.deepcopy(probe["verified_blueprint"])

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "action": "delete",
            "revised_claim": "",
            "evidence_selections": [],
            "rationale": "No supplied evidence supports a useful statement.",
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        c13_author=author,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "deleted"
    assert outcome["ready_for_write"] is False
    assert calls["reviewer"] == []
    revised = result["revised_probe_report"]
    claim = next(
        item for item in revised["final_claims"] if item["claim_id"] == "c1.3"
    )
    assert claim["ready_for_write"] is False
    assert claim["verified_quotes"] == []
    assert claim["candidate_chunk_ids"] == []
    assert revised["verified_blueprint"] == before_blueprint
    assert all(
        contract["claim_id"] != "c1.3"
        for contract in revised.get("claim_scope_contracts") or []
    )


def test_abstract_rejection() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The abstract says PINN inverse design is competitive.",
            depth="abstract_claim",
            kind="abstract",
            permission="contextual_or_qualified_support",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "small_revision",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Uses the abstract quote.",
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "no_progress"
    assert outcome["ready_for_write"] is False
    assert outcome["final_reason"] == "repeated_invalid_author_output"
    assert outcome["round_count"] == 2
    assert calls["reviewer"] == []
    assert any(
        "abstract" in str(feedback)
        for feedback in calls["author"][1]["local_feedback"]
    )


def test_invalid_non_contiguous_quote_rejection() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="PINN inverse design is competitive.",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "small_revision",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": "competitive inverse design PINN",
                }
            ],
            "rationale": "Broken quote.",
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "no_progress"
    assert outcome["final_reason"] == "repeated_invalid_author_output"
    assert outcome["round_count"] == 2
    assert calls["reviewer"] == []
    assert any(
        "contiguous substring" in str(feedback)
        for feedback in calls["author"][1]["local_feedback"]
    )

    # Direct validator also rejects units without paper identity.
    no_paper = dict(snapshot[0])
    no_paper["identity"] = {"chunk_id": "chunk:nopaper"}
    validation = validate_evidence_selections(
        [
            {
                "unit_id": no_paper["unit_id"],
                "chunk_id": "chunk:nopaper",
                "quote": "PINN inverse design is competitive",
            }
        ],
        [no_paper],
    )
    assert validation["valid"] is False
    assert any("paper identity" in error for error in validation["errors"])


def test_max_three_and_no_progress_stopping() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="revision_required")
    )
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The approach generalizes the adjoint method.",
        )
    ]
    quote = "generalizes the adjoint method"

    def changing_author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "rewrite",
            "revised_claim": f"Round {author_input['round']} revised wording.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Iterative rewrite.",
        }

    def revise_reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "revise",
            "reason": "Keep revising.",
            "corrected_claim": "",
            "evidence_selections": [],
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=changing_author,
        c13_reviewer=revise_reviewer,
    )
    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "no_progress"
    assert outcome["final_reason"] == "max_passes_exceeded"
    assert outcome["round_count"] == MAX_PASSES
    assert len(calls["author"]) == 5 + MAX_PASSES
    assert len(calls["reviewer"]) == MAX_PASSES

    def repeating_author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "rewrite",
            "revised_claim": "Same wording every round.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "No change.",
        }

    result2, calls2 = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=repeating_author,
        c13_reviewer=revise_reviewer,
    )
    outcome2 = result2["outcomes"]["c1.3"]
    assert outcome2["final_status"] == "no_progress"
    assert "author_repeated_previous_revision" in outcome2["final_reason"]
    assert outcome2["round_count"] == 2
    assert len(calls2["author"]) == 5 + 2
    assert len(calls2["reviewer"]) == 1


def test_source_envelope_construction() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Narrowed.",
        }

    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )
    revised = result["revised_probe_report"]
    contract = next(
        item
        for item in revised["claim_scope_contracts"]
        if item["claim_id"] == "c1.3"
    )
    envelope = contract["source_envelope"]
    assert envelope["independent_source_count"] == 1
    assert envelope["chunk_ids"] == [snapshot[0]["identity"]["chunk_id"]]
    assert envelope["paper_ids"] == [snapshot[0]["identity"]["paper_id"]]
    assert envelope["permissions"] == ["factual_support"]
    assert envelope["attribution_required"] is True
    assert contract["revised_by"] == "supplementary_claim_revision"


def test_report_immutability_and_determinism() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Narrowed.",
        }

    before = copy.deepcopy(dict(probe))
    before_closure = copy.deepcopy(dict(closure))
    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )
    assert probe == before
    assert closure == before_closure
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["model_tier"] == DEFAULT_MODEL_TIER

    result2, _calls2 = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )

    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    assert canonical(result["dossiers"]) == canonical(result2["dossiers"])
    assert canonical(result["outcomes"]) == canonical(result2["outcomes"])
    assert canonical(result["revised_probe_report"]) == canonical(
        result2["revised_probe_report"]
    )


def test_provider_usage_recorded_and_summarized() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Narrowed.",
        }

    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    usage = result["provider_usage"]
    assert usage["author"]["call_count"] == 6
    assert usage["author"]["input_tokens"] == 60
    assert usage["author"]["output_tokens"] == 30
    assert usage["reviewer"]["call_count"] == 1
    assert usage["total"]["call_count"] == 7
    assert usage["total"]["input_tokens"] == 70
    assert usage["total"]["output_tokens"] == 35
    assert len(usage["calls"]) == 7


def test_missing_closure_uses_medium_revision_mode() -> None:
    probe = _probe()
    dossiers = build_claim_revision_dossiers(
        probe,
        closure_reports=_closure_report(
            _closure_task("c1.3", status="improved_stop")
        ),
    )
    by_id = {dossier["claim_id"]: dossier for dossier in dossiers}
    assert by_id["c1.3"]["revision_mode"] == "small_revision"
    assert by_id["c2.2"]["revision_mode"] == "medium_revision"
    assert by_id["c2.2"]["retrieval_status"] == ""
    assert by_id["c2.2"]["closure"] == {}


def test_apply_revision_outcomes_is_additive() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(_author_input: Mapping[str, Any]) -> dict[str, Any]:
        unit = snapshot[0]
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Narrowed.",
        }

    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )
    revised = result["revised_probe_report"]
    marker = revised["supplementary_revision"]
    assert marker["model_tier"] == DEFAULT_MODEL_TIER
    assert marker["next_command"]["module"] == (
        "experiments.blueprint_writing_acceptance"
    )
    assert marker["next_command"]["arguments"][-2:] == [
        "--live",
        "--policy-recheck",
    ]
    assert "c1.3" in marker["targets"]
    assert marker["outcomes"]["c1.3"]["ready_for_write"] is True
    assert marker["counts"]["passed"] == 1
    assert revised["write_gate"]["allowed_to_write"] is True


def test_malformed_first_author_response_repaired_on_round_two() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        if author_input["dossier"]["claim_id"] != "c1.3":
            return _delete_author(author_input)
        if author_input["round"] == 1:
            return {
                "action": "narrow",
                "revised_claim": "",
                "evidence_selections": [],
                "rationale": "",
            }
        unit = next(
            item
            for item in author_input["dossier"]["evidence_units"]
            if item["origin"] == "incremental"
        )
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Fixed after local contract feedback.",
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "passed"
    assert outcome["round_count"] == 2
    assert len(calls["author"]) == 5 + 2
    assert len(calls["reviewer"]) == 1
    assert any(
        "author_output_invalid" in str(feedback)
        for feedback in calls["author"][1]["local_feedback"]
    )


def test_malformed_reviewer_retried_against_same_author_proposal() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="improved_stop")
    )
    quote = "PINN inverse design is competitive"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The hPINN framework shows that PINN inverse design is competitive.",
        )
    ]

    def author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        if author_input["dossier"]["claim_id"] != "c1.3":
            return _delete_author(author_input)
        unit = next(
            item
            for item in author_input["dossier"]["evidence_units"]
            if item["origin"] == "incremental"
        )
        return {
            "action": "narrow",
            "revised_claim": "PINN inverse design is competitive.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Identical scientific wording every attempt.",
        }

    def reviewer(reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        if reviewer_input["reviewer_attempt"] == 1:
            return {"verdict": "pass"}
        return {
            "verdict": "pass",
            "reason": "Supported by the supplied quote.",
            "corrected_claim": "",
            "evidence_selections": [],
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
        c13_reviewer=reviewer,
        usage={"input_tokens": 3, "output_tokens": 1},
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "passed"
    assert outcome["round_count"] == 1
    assert len(calls["author"]) == 5 + 1
    assert len(calls["reviewer"]) == 2
    round_record = outcome["rounds"][0]
    assert len(round_record["reviewer_attempts"]) == 2
    assert round_record["reviewer_attempts"][0]["local_feedback"]
    assert "reviewer_output_invalid" in round_record["reviewer_attempts"][0][
        "local_feedback"
    ]
    assert (
        round_record["reviewer_attempts"][1]["reviewer_validation"]["verdict"]
        == "pass"
    )
    assert calls["reviewer"][1]["reviewer_attempt"] == 2
    usage = result["provider_usage"]
    assert usage["reviewer"]["call_count"] == 2
    assert usage["reviewer"]["input_tokens"] == 6
    assert usage["total"]["call_count"] == 6 + 2
    assert any(
        call.get("attempt") == 1 and call.get("agent") == "reviewer"
        for call in usage["calls"]
    )
    assert any(
        call.get("attempt") == 2 and call.get("agent") == "reviewer"
        for call in usage["calls"]
    )


def test_max_reviewer_attempts_enforced_with_invalid_reviewer() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c1.3", status="revision_required")
    )
    quote = "generalizes the adjoint method"
    snapshot = [
        _new_unit_for(
            probe,
            "c1.3",
            raw_text="The approach generalizes the adjoint method.",
        )
    ]

    def author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        if author_input["dossier"]["claim_id"] != "c1.3":
            return _delete_author(author_input)
        unit = snapshot[0]
        return {
            "action": "rewrite",
            "revised_claim": "The approach generalizes the adjoint method.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["identity"]["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Same valid proposal on every call.",
        }

    def reviewer(reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        # Invalid (missing reason) but distinct per attempt so the bounded
        # reviewer retry is exercised rather than the repeat short circuit.
        return {
            "verdict": "revise",
            "note": f"attempt {reviewer_input['reviewer_attempt']}",
        }

    result, calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        c13_author=author,
        c13_reviewer=reviewer,
    )

    outcome = result["outcomes"]["c1.3"]
    assert outcome["final_status"] == "no_progress"
    assert outcome["final_reason"] == "max_reviewer_attempts_exceeded"
    assert outcome["round_count"] == 1
    assert len(calls["author"]) == 5 + 1
    assert len(calls["reviewer"]) == 3
    assert [
        call["reviewer_attempt"] for call in calls["reviewer"]
    ] == [1, 2, 3]
    assert any(
        "reviewer_output_invalid" in str(feedback)
        for feedback in calls["reviewer"][1]["local_feedback"]
    )


def test_c10_2_explicit_placement_without_sibling() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c10.2", status="improved_stop")
    )
    quote = "generative surrogate claims remain bounded"
    snapshot = [
        _new_unit_for(
            probe,
            "c10.2",
            raw_text="The study shows generative surrogate claims remain bounded.",
            unit_id="unit:new:c102",
            chunk_id="chunk:new:c102",
            paper_id="paper:new:c102",
        )
    ]
    dossier = next(
        item
        for item in build_claim_revision_dossiers(
            probe, closure_reports=closure
        )
        if item["claim_id"] == "c10.2"
    )
    assert dossier["sibling_claim_ids"] == []
    missing = resolve_blueprint_placement(None, dossier)
    assert missing["valid"] is False
    assert any("explicit" in error for error in missing["errors"])

    def author(author_input: Mapping[str, Any]) -> dict[str, Any]:
        if author_input["dossier"]["claim_id"] != "c10.2":
            return _delete_author(author_input)
        unit = next(
            item
            for item in author_input["dossier"]["evidence_units"]
            if item["origin"] == "incremental"
        )
        return {
            "action": "narrow",
            "revised_claim": "Generative surrogate claims remain bounded.",
            "evidence_selections": [
                {
                    "unit_id": unit["unit_id"],
                    "chunk_id": unit["chunk_id"],
                    "quote": quote,
                }
            ],
            "rationale": "Explicit placement even without siblings.",
            "target_blueprint_placement": {
                "argument_arc": "argument_arc[0].claim_ids",
                "subsection_blueprint": "subsection_blueprint[0].claim_ids",
            },
        }

    def reviewer(_reviewer_input: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "pass",
            "reason": "Supported by the supplied quote.",
            "corrected_claim": "",
            "evidence_selections": [],
        }

    result, _calls = _run(
        probe=probe,
        closure_reports=closure,
        snapshot_units=snapshot,
        author_callback=author,
        reviewer_callback=reviewer,
    )

    outcome = result["outcomes"]["c10.2"]
    assert outcome["final_status"] == "passed"
    assert outcome["final_placement"]["argument_arc"]["index"] == 0
    assert outcome["final_placement"]["subsection_blueprint"]["index"] == 0
    revised = result["revised_probe_report"]
    arc_ids = revised["verified_blueprint"]["argument_arc"][0]["claim_ids"]
    subsection_ids = revised["verified_blueprint"]["subsection_blueprint"][0][
        "claim_ids"
    ]
    adopted = revised["verified_blueprint"]["claim_usage"]["adopted_claim_ids"]
    assert "c10.2" in arc_ids
    assert "c10.2" in subsection_ids
    assert "c10.2" in adopted
    assert "c10.2" not in revised["verified_blueprint"]["section_mission"][
        "claim_ids"
    ]


def test_placement_bare_numbers_rejected_and_exact_paths_resolve() -> None:
    probe = _probe()
    closure = _closure_report(
        _closure_task("c10.2", status="improved_stop")
    )
    dossier = next(
        item
        for item in build_claim_revision_dossiers(
            probe, closure_reports=closure
        )
        if item["claim_id"] == "c10.2"
    )

    bare = resolve_blueprint_placement(
        {"argument_arc": 1, "subsection_blueprint": "1"}, dossier
    )
    assert bare["valid"] is False
    assert any("ambiguous" in error for error in bare["errors"])

    exact = resolve_blueprint_placement(
        {
            "argument_arc": "argument_arc[0].claim_ids",
            "subsection_blueprint": "subsection_blueprint[0].claim_ids",
        },
        dossier,
    )
    assert exact["valid"] is True
    assert exact["argument_arc"]["index"] == 0
    assert exact["subsection_blueprint"]["index"] == 0

    mapping = resolve_blueprint_placement(
        {
            "argument_arc": {"index": 1},
            "subsection_blueprint": {"index": 0},
        },
        dossier,
    )
    assert mapping["valid"] is True
    assert mapping["argument_arc"]["index"] == 1
    assert mapping["subsection_blueprint"]["index"] == 0
