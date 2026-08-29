"""Domain-neutral R5 focus-gate and traceability tests."""

from __future__ import annotations

from copy import deepcopy

from optomind_research.runtime.program_focus_gate import ProgramFocusGate


def _package_fixture() -> tuple[dict, list[dict], list[dict], dict, dict, dict]:
    opportunities = [
        {
            "opportunity_id": "OP01",
            "evidence_status": "partially_supported",
            "supporting_paper_ids": ["paper_1"],
            "supporting_chunk_ids": ["chunk_1"],
            "author_inference": "A controlled perturbation may separate the coupled pathways.",
            "uncertainty": "The separation may depend on calibration quality.",
        },
        {
            "opportunity_id": "OP02",
            "evidence_status": "partially_supported",
            "supporting_paper_ids": ["paper_2"],
            "supporting_chunk_ids": ["chunk_2"],
            "author_inference": "A shared comparison may expose a transferable ordering.",
            "uncertainty": "Transferability across platforms remains uncertain.",
        },
        {
            "opportunity_id": "OP03",
            "evidence_status": "open_gap",
            "supporting_paper_ids": [],
            "supporting_chunk_ids": [],
            "author_inference": "The unexplored regime may reveal a distinct response.",
            "uncertainty": "The effect may remain below the available sensitivity.",
        },
    ]
    hypotheses = [
        {
            "hypothesis_id": "H01",
            "statement": "A controlled perturbation separates the two coupled response pathways.",
            "readiness": "needs_more_literature",
            "supporting_chunk_ids": ["chunk_1"],
        },
        {
            "hypothesis_id": "H02",
            "statement": "A shared evaluation protocol reveals a different ordering of candidate methods.",
            "readiness": "needs_more_literature",
            "supporting_chunk_ids": ["chunk_2"],
        },
    ]
    gate = {
        "schema_version": "research_harness.program_focus_gate.v1",
        "gate_id": "PFG01",
        "main_problem": {
            "problem_id": "P01",
            "statement": "Coupled response pathways prevent reliable comparison.",
            "scope": "One controlled measurement platform.",
            "boundary": "Do not expand to unrelated platforms.",
        },
        "project_type": "experiment",
        "shared_platform": {
            "platform_id": "PLAT01",
            "name": "Controlled measurement platform",
            "description": "One sample preparation, acquisition, and analysis route.",
            "compatibility_key": "controlled_measurement",
        },
        "boundaries": {
            "personnel": ["One research team"],
            "equipment": ["One calibrated instrument"],
            "data": ["Reference and perturbation measurements"],
            "timeline": ["Twelve months"],
            "budget": ["Fixed equipment budget"],
        },
        "unified_evaluation": {
            "metrics": [{"metric_id": "M01", "name": "response separation"}],
            "baselines": [{"baseline_id": "B01", "name": "reference condition"}],
            "comparison_protocol": "Use the same reference and uncertainty procedure for every condition.",
        },
        "selected_opportunity_ids": ["OP01", "OP02"],
        "main_hypothesis_ids": ["H01", "H02"],
        "future_hypothesis_ids": [],
        "hypothesis_dependencies": [
            {
                "upstream_hypothesis_id": "H01",
                "downstream_hypothesis_id": "H02",
                "reason": "The comparison depends on first separating the pathways.",
            }
        ],
        "future_branches": [
            {
                "opportunity_id": "OP03",
                "reason": "The unexplored regime needs a later feasibility study.",
                "excluded_from_current_work_packages": True,
            }
        ],
        "traceability_matrix": [
            {
                "problem_id": "P01",
                "opportunity_id": "OP01",
                "hypothesis_id": "H01",
                "work_package_id": "WP01",
                "proposed_tests": ["Run the perturbation test"],
                "metrics": ["M01"],
                "baselines": ["B01"],
                "falsification_conditions": ["The pathways remain inseparable"],
                "stop_or_pivot_decisions": ["Pivot to a simpler perturbation"],
            },
            {
                "problem_id": "P01",
                "opportunity_id": "OP02",
                "hypothesis_id": "H02",
                "work_package_id": "WP02",
                "proposed_tests": ["Run the common benchmark"],
                "metrics": ["M01"],
                "baselines": ["B01"],
                "falsification_conditions": ["Method ordering is unchanged"],
                "stop_or_pivot_decisions": ["Stop if the benchmark is not transferable"],
            },
        ],
    }
    plan = {
        "results_status": "verification_deferred",
        "main_hypothesis_statements": [
            {"hypothesis_id": item["hypothesis_id"], "statement": item["statement"]}
            for item in hypotheses
        ],
        "narrative_markdown": (
            "The program is a planned validation route. "
            + hypotheses[0]["statement"]
            + " "
            + hypotheses[1]["statement"]
        ),
        "work_packages": [
            {
                "work_package_id": "WP01",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "platform_id": "PLAT01",
                "platform_compatibility_key": "controlled_measurement",
                "metric_ids": ["M01"],
                "baseline_ids": ["B01"],
                "verification_status": "verification_deferred",
            },
            {
                "work_package_id": "WP02",
                "hypothesis_ids": ["H02"],
                "opportunity_ids": ["OP02"],
                "platform_id": "PLAT01",
                "platform_compatibility_key": "controlled_measurement",
                "metric_ids": ["M01"],
                "baseline_ids": ["B01"],
                "verification_status": "verification_deferred",
            },
        ],
    }
    context = {
        "review_scope_map": {"schema_version": "review_scope_map.v1"},
        "literature_relation_graph": {"edge_count": 0},
        "technical_audit": {"status": "passed"},
        "source_permissions": {"direct_fact_permission": "factual_support"},
        "r4_candidate_limitations": [],
    }
    permissions = {
        "paper_permissions": {"paper_1": "factual_support", "paper_2": "factual_support"},
        "chunk_permissions": {"chunk_1": "factual_support", "chunk_2": "factual_support"},
    }
    return gate, opportunities, hypotheses, plan, context, permissions


def test_valid_focus_gate_and_program_package_pass() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    result = ProgramFocusGate().validate_package(
        gate,
        opportunities,
        hypotheses,
        plan,
        shared_context=context,
        permission_map=permissions,
    )
    assert result.passed
    assert result.metrics["traceability_row_count"] == 2
    assert result.metrics["future_branch_count"] == 1


def test_gate_rejects_multiple_main_problems() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    gate["main_problem"] = [gate["main_problem"], deepcopy(gate["main_problem"])]
    result = ProgramFocusGate().validate_focus_decision(
        gate, opportunities, hypotheses, shared_context=context, permission_map=permissions
    )
    assert "main_problem_count_must_be_exactly_one" in result.errors


def test_gate_rejects_future_branch_leakage() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    plan["work_packages"][0]["opportunity_ids"] = ["OP03"]
    result = ProgramFocusGate().validate_package(
        gate, opportunities, hypotheses, plan, shared_context=context, permission_map=permissions
    )
    assert any("future_branch_leaks" in error for error in result.errors)


def test_gate_rejects_mixed_incompatible_platforms() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    plan["work_packages"][1]["platform_compatibility_key"] = "different_platform"
    result = ProgramFocusGate().validate_package(
        gate, opportunities, hypotheses, plan, shared_context=context, permission_map=permissions
    )
    assert "work_package_platform_compatibility_mismatch:WP02" in result.errors


def test_gate_rejects_discovery_only_hypothesis_support() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    permissions["chunk_permissions"]["chunk_2"] = "discovery_only"
    hypotheses[1]["readiness"] = "ready"
    result = ProgramFocusGate().validate_focus_decision(
        gate, opportunities, hypotheses, shared_context=context, permission_map=permissions
    )
    assert any("hypothesis_uses_discovery_only_chunk:H02" in error for error in result.errors)


def test_contextual_opportunity_can_be_selected_without_ready_hypothesis() -> None:
    gate, opportunities, hypotheses, _, context, permissions = _package_fixture()
    permissions["paper_permissions"] = {
        "paper_1": "contextual_or_qualified_support",
        "paper_2": "contextual_or_qualified_support",
    }
    permissions["chunk_permissions"] = {
        "chunk_1": "contextual_or_qualified_support",
        "chunk_2": "contextual_or_qualified_support",
    }
    result = ProgramFocusGate().validate_focus_decision(
        gate,
        opportunities,
        hypotheses,
        shared_context=context,
        permission_map=permissions,
    )
    assert result.passed
    assert any("contextual_permission" in warning for warning in result.warnings)

    hypotheses[0]["readiness"] = "ready"
    result = ProgramFocusGate().validate_focus_decision(
        gate,
        opportunities,
        hypotheses,
        shared_context=context,
        permission_map=permissions,
    )
    assert "ready_hypothesis_requires_factual_support:H01" in result.errors


def test_single_simulation_platform_normalizes_hybrid_downward() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    gate["project_type"] = "hybrid"
    gate["shared_platform"]["platform_type"] = "simulation"
    normalized, corrections = ProgramFocusGate.normalize_compatibility(gate)
    assert normalized["project_type"] == "simulation"
    assert corrections
    result = ProgramFocusGate().validate_package(
        normalized,
        opportunities,
        hypotheses,
        plan,
        shared_context=context,
        permission_map=permissions,
    )
    assert result.passed


def test_true_hybrid_requires_explicit_components_and_component_boundaries() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    gate["project_type"] = "hybrid"
    gate["shared_platform"].update(
        {
            "platform_type": "hybrid",
            "components": ["theory", "simulation"],
            "component_boundaries": {
                "theory": ["Analytical derivation scope"],
                "simulation": ["Controlled numerical solver scope"],
            },
        }
    )
    result = ProgramFocusGate().validate_package(
        gate,
        opportunities,
        hypotheses,
        plan,
        shared_context=context,
        permission_map=permissions,
    )
    assert result.passed


def test_no_factual_premise_requires_first_package_to_mature_evidence() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    permissions["paper_permissions"] = {
        "paper_1": "contextual_or_qualified_support",
        "paper_2": "contextual_or_qualified_support",
    }
    permissions["chunk_permissions"] = {
        "chunk_1": "contextual_or_qualified_support",
        "chunk_2": "contextual_or_qualified_support",
    }
    result = ProgramFocusGate().validate_package(
        gate,
        opportunities,
        hypotheses,
        plan,
        shared_context=context,
        permission_map=permissions,
    )
    assert "first_work_package_must_mature_literature_data_or_benchmark_before_validation" in result.errors


def test_gate_rejects_missing_full_hypothesis_statement() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    plan["narrative_markdown"] = "The program refers only to H01 and H02."
    result = ProgramFocusGate().validate_package(
        gate, opportunities, hypotheses, plan, shared_context=context, permission_map=permissions
    )
    assert any("full_main_hypothesis_statement_missing" in error for error in result.errors)


def test_gate_rejects_orphan_work_package_and_traceability_break() -> None:
    gate, opportunities, hypotheses, plan, context, permissions = _package_fixture()
    plan["work_packages"].append(
        {
            "work_package_id": "WP03",
            "hypothesis_ids": [],
            "opportunity_ids": [],
            "platform_id": "PLAT01",
            "platform_compatibility_key": "controlled_measurement",
            "metric_ids": ["M01"],
            "baseline_ids": ["B01"],
            "verification_status": "verification_deferred",
        }
    )
    result = ProgramFocusGate().validate_package(
        gate, opportunities, hypotheses, plan, shared_context=context, permission_map=permissions
    )
    assert "orphan_work_package:WP03" in result.errors
    assert "traceability_matrix_does_not_cover_work_packages" in result.errors
