"""Adversarial, offline checks for section-coverage decision contracts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    PROJECT_ROOT
    / "optomind_research"
    / "runtime"
    / "coverage_decision_contract.py"
)


def _load_contract():
    spec = importlib.util.spec_from_file_location(
        "coverage_decision_contract_under_test",
        CONTRACT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT = _load_contract()


def _section_data() -> dict[str, Any]:
    return {
        "section_id": "S-DECISION",
        "title": "Integrated nonlinear optical waveguides",
        "scope_description": "section-specific nonlinear optical waveguide physics",
        "chapter_argument": "Mechanism and method determine the usable design space.",
        "required_roles": ["foundation", "mechanism", "method"],
        "topic_identity": {
            "scientific_object": "integrated nonlinear optical waveguides",
            "core_anchor_tokens": ["integrated", "nonlinear", "optical", "waveguides"],
        },
    }


def test_candidate_state_machine_rejects_requested_promotions() -> None:
    """A stale/model-supplied action cannot promote an unapproved candidate."""

    enum_like = SimpleNamespace(value="approved")
    cases = [
        ({"decision": "deferred", "scope_fit": "direct"}, "reject", False),
        ({"decision": "rejected", "scope_fit": "direct"}, "reject", False),
        ({"decision": "approved", "scope_fit": "contextual"}, "discovery_lead", False),
        ({"decision": "approved", "scope_fit": "out_of_scope"}, "reject", False),
        # Scientific approval alone is not an acquisition route.
        ({"decision": "approved", "scope_fit": "direct"}, "discovery_lead", False),
        # Each typed legal route remains executable after approval.
        (
            {
                "decision": "approved",
                "scope_fit": "direct",
                "is_oa": True,
                "oa_url": "https://oa.example/paper.pdf",
            },
            "materialize_now",
            True,
        ),
        (
            {
                "decision": enum_like,
                "scope_fit": "adjacent",
                "semantic_scholar_id": "S2:1",
                "content_depth": "structured_body",
            },
            "materialize_now",
            True,
        ),
        (
            {
                "decision": "approved",
                "scope_fit": "direct",
                "local_fulltext_path": "C:/fulltext/paper.txt",
            },
            "materialize_now",
            True,
        ),
    ]

    for candidate, expected_action, expected_materializable in cases:
        contract = CONTRACT.canonical_candidate_decision(
            candidate,
            requested_action="materialize_now",
        )
        assert contract.action == expected_action
        assert CONTRACT.candidate_is_materializable(candidate) is expected_materializable

    clamped = CONTRACT.canonical_candidate_decision(
        {"decision": "approved", "scope_fit": "direct"},
        requested_action="materialize_now",
    )
    assert clamped.state == "approved_discovery_lead"
    assert clamped.route_available is False
    assert "materialize_now_clamped_to_discovery_lead" in clamped.reason


def test_json_recovery_accepts_transport_noise_but_not_malformed_decisions() -> None:
    recovered = CONTRACT.decode_json_payload(
        "```json\n[{\"candidate_id\": \"c1\",},]\n```",
        expected="list",
    )
    assert recovered.error == ""
    assert recovered.recovered is True
    assert recovered.value == [{"candidate_id": "c1"}]

    prose = CONTRACT.decode_json_payload(
        "The decision records are:\n{\"decision\": \"approved\"}",
        expected="object",
    )
    assert prose.error == ""
    assert prose.value["decision"] == "approved"

    for malformed in (
        "[{'candidate_id': 'c1'}]",  # Python literal, not JSON.
        "[{\"candidate_id\": \"c1\"}",  # Unclosed array.
        "{\"decision\": \"approved\"} and then another decision",
    ):
        result = CONTRACT.decode_json_payload(malformed, expected="list")
        assert result.error
        assert result.value is None


def test_uncovered_roles_and_components_generate_stable_compact_queries() -> None:
    section = _section_data()
    audit = {
        "role_audits": {
            "foundation": {
                "coverage_verdict": "sufficient",
                "gap_severity": "minor",
            },
            "mechanism": {
                "coverage_verdict": "none",
                "gap_severity": "blocking",
            },
        }
    }
    source_ledger = {
        "sources": [
            {
                "literature_role": "foundation",
                "canonical_chunk_ids": ["chunk:foundation"],
            }
        ]
    }
    roles = CONTRACT.derive_uncovered_roles(
        section,
        audit=audit,
        source_ledger=source_ledger,
    )
    assert "foundation" not in roles
    assert "mechanism" in roles
    assert "method" in roles

    targets = CONTRACT.build_uncovered_query_targets(
        section,
        roles=roles,
        components=["phase matching", {"component": "coupling regime"}],
        max_targets=8,
    )
    assert targets == CONTRACT.build_uncovered_query_targets(
        section,
        roles=roles,
        components=["phase matching", {"component": "coupling regime"}],
        max_targets=8,
    )
    assert targets
    assert len(targets) <= 8
    assert all(len(item["query"]) <= 220 for item in targets)
    assert {item.get("role") for item in targets} >= {"mechanism", "method"}
    assert all("integrated nonlinear optical waveguides" in item["query"] for item in targets)

    closed = CONTRACT.closed_scientific_components(
        targets,
        [{
            "literature_role": "mechanism",
            "canonical_chunk_ids": ["chunk:mechanism"],
        }],
    )
    assert any("phase matching" in item for item in closed)


def test_readiness_and_context_admission_are_truthful_before_a_call() -> None:
    open_package = {
        "coverage_status": "completed_with_open_gaps",
        "breadth_target_met": False,
        "blocking_gaps_remain": False,
    }
    readiness = CONTRACT.evaluate_coverage_readiness(
        required_artifacts=("SECTION_CONTEXT.json",),
        work_dir_exists=True,
        package=open_package,
    )
    assert readiness.structural_task_complete is True
    assert readiness.scientific_coverage_ready is False
    assert readiness.outcome == "needs_more_literature"

    sufficient = CONTRACT.evaluate_coverage_readiness(
        required_artifacts=(),
        work_dir_exists=True,
        package={
            "coverage_status": "coverage_sufficient",
            "breadth_target_met": True,
            "blocking_gaps_remain": False,
        },
    )
    assert sufficient.scientific_coverage_ready is True
    assert sufficient.outcome == "completed"

    admitted = CONTRACT.admit_context_call(
        predicted_input_tokens=900,
        output_reserve_tokens=100,
        cumulative_input_tokens=0,
        cumulative_budget_tokens=2000,
        per_call_budget_tokens=1000,
    )
    assert admitted.admitted is True
    per_call_rejected = CONTRACT.admit_context_call(
        predicted_input_tokens=901,
        output_reserve_tokens=100,
        cumulative_input_tokens=0,
        cumulative_budget_tokens=2000,
        per_call_budget_tokens=1000,
    )
    assert per_call_rejected.admitted is False
    assert per_call_rejected.reason == "per_call_context_budget_exceeded"
    cumulative_rejected = CONTRACT.admit_context_call(
        predicted_input_tokens=500,
        output_reserve_tokens=100,
        cumulative_input_tokens=1450,
        cumulative_budget_tokens=2000,
        per_call_budget_tokens=1000,
    )
    assert cumulative_rejected.admitted is False
    assert cumulative_rejected.reason == "cumulative_context_budget_exceeded"


def _registry_context(tmp_path: Path):
    _registry_module()
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    return SectionCoverageContext(
        section_id="S-DECISION",
        section_data=_section_data(),
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "temp.sqlite",
        work_dir=tmp_path,
    )


def _registry_module():
    pytest.importorskip("agentscope")
    try:
        from optomind_research.runtime import section_coverage_tool_registry
    except ModuleNotFoundError as exc:
        pytest.skip(f"registry runtime dependency unavailable: {exc}")
    return section_coverage_tool_registry


def test_registry_repairs_legacy_actions_and_rejects_invalid_audit_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry_module()

    candidate = {
        "candidate_id": "cand_legacy",
        "section_id": "S-DECISION",
        "role": "mechanism",
        "title": "Mechanism paper",
        "abstract": "A compact abstract.",
        "decision": "deferred",
        "scope_fit": "direct",
        "candidate_action": "materialize_now",
    }
    registry._append_candidates_to_ledger(tmp_path, "S-DECISION", [candidate])
    stored = json.loads((tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))
    assert stored["candidates"][0]["candidate_action"] == "reject"

    ctx = _registry_context(tmp_path)
    registry._restore_candidates_from_ledger(ctx)
    assert ctx.get_candidate("cand_legacy")["candidate_action"] == "reject"

    monkeypatch.setattr(
        registry,
        "_deterministic_post_audit_transition",
        lambda *_args, **_kwargs: {"status": "stubbed"},
    )
    invalid = registry._make_submit_candidate_audit(ctx)(
        "[{'candidate_id': 'cand_legacy', 'scope_fit': 'direct', "
        "'decision': 'approved', 'audit_reason': 'bad transport'}]"
    )
    assert json.loads(invalid)["status"] == "error"
    unchanged = json.loads((tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8"))
    assert unchanged["candidates"][0]["decision"] == "deferred"

    valid = registry._make_submit_candidate_audit(ctx)(
        "```json\n[{\"candidate_id\": \"cand_legacy\", \"scope_fit\": "
        "\"direct\", \"role_fit\": [\"mechanism\"], \"decision\": "
        "\"approved\", \"audit_reason\": \"direct mechanism evidence\", "
        "\"not_usable_for\": [],}]\n```"
    )
    valid_data = json.loads(valid)
    assert valid_data["status"] == "ok"
    assert valid_data["json_recovered"] is True
    # Approval remains scientifically valid, but a route-less candidate is
    # clamped out of executable materialization.
    assert valid_data["approved_ids"] == ["cand_legacy"]
    assert valid_data["materialize_now_ids"] == []
    assert valid_data["candidate_actions"]["discovery_lead"] == 1
    provenance = valid_data["candidate_action_provenance"]["cand_legacy"]
    assert provenance["action"] == "discovery_lead"
    assert provenance["route_available"] is False
    assert "materialize_now_clamped_to_discovery_lead" in provenance["reason"]
    persisted = json.loads(
        (tmp_path / "OA_CANDIDATE_LEDGER.json").read_text(encoding="utf-8")
    )
    assert persisted["candidates"][0]["candidate_action"] == "discovery_lead"
    assert "candidate_action_provenance:" in persisted["candidates"][0]["audit_reason"]


def test_repeated_candidate_inspection_is_delta_only_and_measurably_smaller(tmp_path: Path) -> None:
    registry = _registry_module()

    ctx = _registry_context(tmp_path)
    ids = ctx.register_candidates([
        {
            "candidate_id": f"cand_{index}",
            "section_id": "S-DECISION",
            "role": "mechanism",
            "title": f"Mechanism paper {index}",
            "doi": f"10.1234/{index}",
            "abstract": "long scientific abstract " * 300,
            "authors": ["Author A", "Author B"],
            "backends": ["offline_fixture"],
            "decision": "deferred",
            "scope_fit": "unreviewed",
        }
        for index in range(6)
    ])
    inspect = registry._make_inspect_candidate_batch(ctx)
    first = inspect(json.dumps(ids))
    second = inspect(json.dumps(ids))
    first_size = len(first.encode("utf-8"))
    second_size = len(second.encode("utf-8"))
    second_data = json.loads(second)
    assert first_size > second_size
    assert second_data["candidates"] == []
    assert second_data["unchanged_ids"] == ids
    assert first_size - second_size > 1000


def test_visual_extraction_gate_requires_textual_closure_or_explicit_visual_first(tmp_path: Path) -> None:
    registry = _registry_module()

    assert registry._visual_first_evidence_required(_section_data()) is False
    visual_section = {**_section_data(), "visual_evidence_mode": "visual_first"}
    assert registry._visual_first_evidence_required(visual_section) is True
    assert registry._textual_evidence_closure_exists(
        tmp_path,
        candidate_id="cand_visual",
        new_chunk_ids=["chunk:new"],
    ) is False
    (tmp_path / "SECTION_MATERIAL_PACKAGE.json").write_text(
        json.dumps({"coverage_status": "coverage_sufficient"}),
        encoding="utf-8",
    )
    assert registry._textual_evidence_closure_exists(
        tmp_path,
        candidate_id="cand_visual",
        new_chunk_ids=["chunk:new"],
    ) is True


def test_result_reconciliation_does_not_leave_completed_all_gates_passed(tmp_path: Path) -> None:
    _registry_module()
    try:
        from optomind_research.runtime.section_coverage_orchestrator import (
            SectionCoverageOrchestrator,
        )
    except ModuleNotFoundError as exc:
        pytest.skip(f"orchestrator runtime dependency unavailable: {exc}")

    (tmp_path / "RESULT.json").write_text(
        json.dumps({"status": "completed", "all_gates_passed": True}),
        encoding="utf-8",
    )
    (tmp_path / "RESULT.md").write_text("result\n", encoding="utf-8")
    package = {
        "coverage_status": "completed_with_open_gaps",
        "breadth_target_met": False,
        "blocking_gaps_remain": False,
    }
    SectionCoverageOrchestrator._reconcile_result_artifact(
        tmp_path,
        package,
        structural_task_complete=True,
    )
    SectionCoverageOrchestrator._reconcile_result_artifact(
        tmp_path,
        package,
        structural_task_complete=True,
    )
    result = json.loads((tmp_path / "RESULT.json").read_text(encoding="utf-8"))
    result_md = (tmp_path / "RESULT.md").read_text(encoding="utf-8")
    assert result["status"] == "validation_failed"
    assert result["coverage_outcome"] == "needs_more_literature"
    assert result["structural_task_complete"] is True
    assert result["scientific_coverage_ready"] is False
    assert result["all_gates_passed"] is False
    assert result_md.count("Scientific coverage readiness") == 1
