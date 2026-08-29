from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from agentscope.tool import FunctionTool

from optomind_research.runtime.literature_portfolio import (
    build_literature_portfolio_report,
    build_portfolio_feedback,
)
from optomind_research.runtime.research_program_tool_provider import (
    ResearchProgramContext,
    ResearchProgramToolProvider,
    _normalize_hypothesis_payload,
    _normalize_plan_payload,
    _normalize_focus_gate_against_opportunities,
    _try_evidence_calibrated_single_spine,
    _recover_last_focus_gate_from_agent_state,
    _parse_json_object,
    _render_research_plan_markdown,
    _audit_rendered_plan_content,
    _build_readiness_summary,
    _decision_point_is_substantive,
    _sanitize_plan_packages_to_focus,
    _build_plan_only_traceability_matrix,
    _unsupported_narrative_quantitative_claims,
)
from optomind_research.runtime.research_program_schemas import ResearchPlan
from optomind_research.runtime.research_program_runner import (
    _archive_program_for_schema_migration,
    _archive_r5_agent_state_for_resume,
    _build_r5_budget_state,
    _determine_r5_discovery_stage,
    _archive_r5_phase_runtime_for_handoff,
    _load_r5_lifetime_state,
    _record_r5_phase_accounting,
    _requires_quantitative_provenance_migration,
    _run_deterministic_program_validation,
    run_research_program,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.full_review_orchestrator import (
    _archive_section_authoring_for_rebuild,
    _archive_section_runtime_for_retry,
)
from optomind_research.runtime.revision_planner import RevisionPlanner
from optomind_research.runtime.section_coverage_orchestrator import (
    SectionCoverageOrchestrator,
    SectionCoverageOrchestratorConfig,
)
from optomind_research.runtime.section_authoring_assets import (
    CanonicalAssetGraph,
    ChunkAsset,
    PaperAsset,
)
from optomind_research.runtime.section_authoring_tool_registry import (
    _make_request_more_literature,
    _make_submit_argument_plan,
    _requires_strict_citation_entailment,
    _restriction_conflict,
    _synthesis_source_requirement,
    _synthesis_source_diversity_error,
    _validate_argument_plan_data,
)
from optomind_research.runtime.tool_provider import (
    SectionAuthoringContext,
    SectionCoverageContext,
)


def _tool_text(tool: FunctionTool, **kwargs) -> str:
    value = tool(**kwargs)
    result = asyncio.run(value) if asyncio.iscoroutine(value) else value
    return " ".join(
        block.text for block in result.content if hasattr(block, "text")
    )


def _write_section_assets(
    root: Path,
    section_id: str,
    papers: list[tuple[str, str]],
    *,
    minimum_unique: int = 4,
    minimum_direct: int = 3,
) -> None:
    section = root / "sections" / section_id
    section.mkdir(parents=True, exist_ok=True)
    sources = []
    for index, (paper_id, scope) in enumerate(papers):
        sources.append(
            {
                "paper_id": paper_id,
                "title": f"Paper {paper_id}",
                "literature_role": (
                    "mechanism" if index % 2 == 0 else "method"
                ),
                "scope_fit": scope,
                "canonical_chunk_ids": [f"{paper_id}:chunk:{index}"],
                "not_usable_for": [],
            }
        )
    (section / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({"section_id": section_id, "sources": sources}),
        encoding="utf-8",
    )
    direct = len({p for p, scope in papers if scope == "direct"})
    unique = len({p for p, _ in papers})
    (section / "SECTION_MATERIAL_PACKAGE.json").write_text(
        json.dumps(
            {
                "section_id": section_id,
                "unique_sources": unique,
                "direct_sources": direct,
                "minimum_unique_sources": minimum_unique,
                "minimum_direct_sources": minimum_direct,
                "breadth_target_met": (
                    unique >= minimum_unique and direct >= minimum_direct
                ),
            }
        ),
        encoding="utf-8",
    )


def test_literature_portfolio_detects_sparse_and_concentrated_sections(
    tmp_path: Path,
):
    blueprint = {
        "sections": [
            {
                "section_id": "S01",
                "target_word_range": {"min": 1000, "max": 1400},
            },
            {
                "section_id": "S02",
                "target_word_range": {"min": 1000, "max": 1400},
            },
        ]
    }
    _write_section_assets(
        tmp_path,
        "S01",
        [
            ("paper_A", "direct"),
            ("paper_A", "direct"),
            ("paper_B", "adjacent"),
        ],
    )
    _write_section_assets(
        tmp_path,
        "S02",
        [
            ("paper_C", "direct"),
            ("paper_D", "direct"),
            ("paper_E", "direct"),
            ("paper_F", "adjacent"),
        ],
    )
    report = build_literature_portfolio_report(
        blueprint=blueprint,
        coverage_root=tmp_path,
    )
    assert report["article_unique_sources"] == 6
    assert "S01" in report["sections_needing_expansion"]
    assert report["section_reports"][0]["breadth_target_met"] is False
    feedback = build_portfolio_feedback(report)
    assert "S01" in feedback
    assert set(feedback).issubset({"S01", "S02"})
    assert feedback["S01"]["feedback_origin"].startswith("pre_authoring")


def test_article_level_shortfall_routes_bounded_expansion():
    report = {
        "article_breadth_target_met": False,
        "article_unique_sources": 12,
        "article_direct_sources": 8,
        "recommended_minimum_unique_sources": 28,
        "recommended_minimum_direct_sources": 17,
        "section_reports": [
            {
                "section_id": f"S{index:02d}",
                "unique_sources": index + 1,
                "direct_sources": index,
                "largest_source_role_share": 0.2,
                "needs_expansion": False,
                "expansion_reasons": [],
            }
            for index in range(1, 8)
        ],
    }
    feedback = build_portfolio_feedback(report)
    assert 2 <= len(feedback) <= 4
    assert set(feedback).issubset({"S01", "S02", "S03", "S04"})
    assert all(
        "article_level_source_breadth_shortfall"
        in item["feedback_items"][0]["reasons"]
        for item in feedback.values()
    )


def test_targeted_coverage_retry_archives_runtime_not_science(
    tmp_path: Path,
):
    blueprint = tmp_path / "blueprint.json"
    blueprint.write_text('{"sections":[]}', encoding="utf-8")
    config = SectionCoverageOrchestratorConfig(
        blueprint_path=blueprint,
        base_kb_sqlite=None,
        output_root=tmp_path,
        force_research_sections=["S01"],
        retry_label="test",
    )
    orchestrator = SectionCoverageOrchestrator(
        config, run_dir=tmp_path
    )
    section = tmp_path / "sections" / "S01"
    section.mkdir(parents=True)
    (section / "RESULT.json").write_text("{}", encoding="utf-8")
    (section / "AGENT_STATE.json").write_text("{}", encoding="utf-8")
    (section / "SECTION_SOURCE_LEDGER.json").write_text(
        '{"sources":[]}', encoding="utf-8"
    )
    (section / "SECTION_MATERIAL_PACKAGE.json").write_text(
        "{}", encoding="utf-8"
    )
    orchestrator._prepare_for_targeted_retry(section)
    assert not (section / "RESULT.json").exists()
    assert not (section / "SECTION_MATERIAL_PACKAGE.json").exists()
    assert (section / "SECTION_SOURCE_LEDGER.json").exists()
    archives = list((section / "_runtime_archive").glob("test_*"))
    assert len(archives) == 1
    assert (archives[0] / "RESULT.json").exists()
    assert (
        archives[0] / "SECTION_MATERIAL_PACKAGE.before_retry.json"
    ).exists()


def test_incomplete_coverage_runtime_restarts_once_and_preserves_cost(
    tmp_path: Path,
):
    blueprint = tmp_path / "blueprint.json"
    blueprint.write_text('{"sections":[]}', encoding="utf-8")
    orchestrator = SectionCoverageOrchestrator(
        SectionCoverageOrchestratorConfig(
            blueprint_path=blueprint,
            base_kb_sqlite=None,
            output_root=tmp_path,
        ),
        run_dir=tmp_path,
    )
    section = tmp_path / "sections" / "S01"
    section.mkdir(parents=True)
    previous = {
        "status": "budget_exhausted",
        "stop_reason": "final_admitted_model_call_token_overshoot",
        "estimated_cost_cny": 1.125,
    }
    (section / "RESULT.json").write_text(
        json.dumps(previous), encoding="utf-8"
    )
    (section / "AGENT_STATE.json").write_text("{}", encoding="utf-8")
    (section / "SECTION_SOURCE_LEDGER.json").write_text(
        '{"sources":[{"paper_id":"paper_A"}]}', encoding="utf-8"
    )

    assert orchestrator._should_restart_incomplete_runtime(
        section, previous, {}
    )
    receipt = orchestrator._restart_incomplete_runtime(
        section, previous, {}
    )

    assert receipt["restart_count"] == 1
    assert receipt["cumulative_cost_cny"] == 1.125
    assert not (section / "RESULT.json").exists()
    assert not (section / "AGENT_STATE.json").exists()
    assert (section / "SECTION_SOURCE_LEDGER.json").exists()
    assert not orchestrator._should_restart_incomplete_runtime(
        section, previous, receipt
    )


def test_failed_coverage_package_restarts_but_recovered_package_reuses(
    tmp_path: Path,
):
    blueprint = tmp_path / "blueprint.json"
    blueprint.write_text('{"sections":[]}', encoding="utf-8")
    orchestrator = SectionCoverageOrchestrator(
        SectionCoverageOrchestratorConfig(
            blueprint_path=blueprint,
            base_kb_sqlite=None,
            output_root=tmp_path,
        ),
        run_dir=tmp_path,
    )
    section = tmp_path / "sections" / "S05"
    section.mkdir(parents=True)
    previous = {
        "status": "budget_exhausted",
        "stop_reason": "token_budget exhausted before validation",
    }
    (section / "SECTION_MATERIAL_PACKAGE.json").write_text(
        '{"blocking_gaps_remain":true}', encoding="utf-8"
    )
    (section / "COVERAGE_RECOVERY.json").write_text(
        '{"recovered":false}', encoding="utf-8"
    )
    assert orchestrator._should_restart_incomplete_runtime(
        section, previous, {}
    )

    (section / "COVERAGE_RECOVERY.json").write_text(
        '{"recovered":true}', encoding="utf-8"
    )
    assert not orchestrator._should_restart_incomplete_runtime(
        section, previous, {}
    )


def test_recovered_coverage_package_preserves_open_gap_status():
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
    )

    assert (
        SectionCoverageOrchestrator._status_from_package(
            {
                "coverage_status": "completed_with_open_gaps",
                "blocking_gaps_remain": False,
                "breadth_target_met": False,
            }
        )
        == "needs_more_literature"
    )
    assert (
        SectionCoverageOrchestrator._status_from_package(
            {
                "coverage_status": "coverage_sufficient",
                "blocking_gaps_remain": False,
                "breadth_target_met": True,
            }
        )
        == "completed"
    )


def test_targeted_coverage_stage_does_not_double_charge_runtime_retry():
    receipt = {"cumulative_cost_cny": 1.1016}
    assert SectionCoverageOrchestrator._chargeable_prior_retry_cost(
        force_research=False,
        retry_receipt=receipt,
    ) == 1.1016
    assert SectionCoverageOrchestrator._chargeable_prior_retry_cost(
        force_research=True,
        retry_receipt=receipt,
    ) == 0.0


def test_budget_stop_gap_documentation_is_explicit_and_auditable(
    tmp_path: Path,
):
    ctx = SectionCoverageContext(
        section_id="S05",
        section_data={"section_id": "S05"},
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=tmp_path,
    )
    (tmp_path / "SEARCH_BUDGET_LEDGER.json").write_text(
        json.dumps(
            {
                "rounds": [
                    {
                        "role": "controversy",
                        "queries": ["TFLN packaging conflicting evidence"],
                        "candidate_count": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    documented = (
        SectionCoverageOrchestrator._document_budget_stop_gaps(
            ctx,
            validation_text=(
                "VALIDATION_FAILED: 2 blocking gaps remain with no documented "
                "stop reason: ['controversy', 'coverage_breadth']. Use refresh."
            ),
            worker_stop_reason="token_budget reached",
        )
    )
    assert documented
    report = json.loads(
        (tmp_path / "SECTION_GAP_REPORT.json").read_text(encoding="utf-8")
    )
    assert {gap["role"] for gap in report["gaps"]} == {
        "controversy",
        "coverage_breadth",
    }
    assert all(gap["stop_reason"] for gap in report["gaps"])
    assert report["gaps"][0]["queries_attempted"]


def _research_program_fixture(tmp_path: Path) -> ResearchProgramToolProvider:
    blueprint = {
        "input_context": {
            "user_question": "How can an optical platform be improved?"
        },
        "review_thesis": "A shared physical constraint limits performance.",
        "full_review_argument": "The review moves from mechanism to deployment.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Mechanism",
                "argument_role": "Establish the physical constraint.",
                "chapter_argument": "Loss and dispersion jointly set the limit.",
                "synthesis_task": "Separate consensus from uncertainty.",
            }
        ],
    }
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    review_path = tmp_path / "review.md"
    review_path.write_text(
        "## Mechanism\n\nThe evidence establishes a bounded physical "
        "constraint [REF:paper_A].\n\nA measurement gap remains.",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage"
    _write_section_assets(
        coverage,
        "S01",
        [("paper_A", "direct"), ("paper_B", "direct")],
        minimum_unique=2,
        minimum_direct=2,
    )
    db = tmp_path / "kb.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE text_chunks "
            "(chunk_id TEXT PRIMARY KEY, paper_id TEXT, text TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?)",
            [
                (
                    "paper_A:chunk:0",
                    "paper_A",
                    "Measured dispersion and loss jointly constrain response.",
                ),
                (
                    "paper_B:chunk:1",
                    "paper_B",
                    "Independent measurements expose a benchmark gap.",
                ),
            ],
        )
    return ResearchProgramToolProvider(
        ResearchProgramContext(
            blueprint_path=blueprint_path,
            final_review_path=review_path,
            coverage_root=coverage,
            work_dir=tmp_path / "program",
            base_kb_sqlite=db,
        )
    )


def _write_r5_reconciliation_fixture(
    tmp_path: Path,
    *,
    plan_matrix: str = "valid",
    focus_matrix: str = "empty",
) -> ResearchProgramToolProvider:
    """Create a small, fully structured R5 package for resume regressions."""

    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    opportunities = [
        {
            "opportunity_id": "OP01",
            "title": "Separate coupled limits",
            "problem": "Two optical limits remain coupled.",
            "why_it_matters": "A controlled program can separate them.",
            "origin_type": "method_gap",
            "source_section_ids": ["S01"],
            "supporting_paper_ids": ["paper_A"],
            "supporting_chunk_ids": ["paper_A:chunk:0"],
            "evidence_status": "partially_supported",
            "evidence_basis": "The canonical chunk describes the coupled limit.",
            "author_inference": "A controlled perturbation may separate the limits.",
            "uncertainty": "The perturbations may remain coupled.",
            "recommended_next_evidence": ["Controlled comparison"],
        },
        {
            "opportunity_id": "OP02",
            "title": "Check transferability",
            "problem": "Transferability is not established.",
            "why_it_matters": "A later check may expose a boundary.",
            "origin_type": "evidence_gap",
            "source_section_ids": ["S01"],
            "supporting_paper_ids": [],
            "supporting_chunk_ids": [],
            "evidence_status": "open_gap",
            "evidence_basis": "No direct evidence is attached.",
            "author_inference": "A bounded check could resolve the gap.",
            "uncertainty": "The effect may be platform dependent.",
            "recommended_next_evidence": ["Transfer test"],
        },
        {
            "opportunity_id": "OP03",
            "title": "Check scaling",
            "problem": "Scaling evidence is incomplete.",
            "why_it_matters": "Scaling may change the practical boundary.",
            "origin_type": "deployment_gap",
            "source_section_ids": ["S01"],
            "supporting_paper_ids": [],
            "supporting_chunk_ids": [],
            "evidence_status": "open_gap",
            "evidence_basis": "No direct evidence is attached.",
            "author_inference": "A bounded scaling check could be informative.",
            "uncertainty": "The effect may be too small to resolve.",
            "recommended_next_evidence": ["Scaling study"],
        },
    ]
    hypotheses = [
        {
            "hypothesis_id": "H01",
            "title": "Separate the coupled limits",
            "statement": "A controlled perturbation separates the two optical limits.",
            "source_opportunity_ids": ["OP01"],
            "mechanism_rationale": "The perturbation changes one pathway at a time.",
            "supporting_paper_ids": ["paper_A"],
            "supporting_chunk_ids": ["paper_A:chunk:0"],
            "inference_chain": ["The limits are coupled.", "The perturbation can separate them."],
            "assumptions": ["The perturbations are independently controllable."],
            "alternative_explanations": ["The coupling is an instrument artifact."],
            "falsification_conditions": ["The limits remain inseparable."],
            "novelty_status": "unknown_requires_prior_art_search",
            "confidence": "medium",
            "readiness": "needs_more_literature",
        },
        {
            "hypothesis_id": "H02",
            "title": "Transferability branch",
            "statement": "A later transfer test will expose the platform boundary.",
            "source_opportunity_ids": ["OP02"],
            "mechanism_rationale": "The gap motivates a bounded transfer check.",
            "supporting_paper_ids": [],
            "supporting_chunk_ids": [],
            "inference_chain": ["The boundary is open.", "A transfer test can probe it."],
            "assumptions": ["A comparable platform can be assembled."],
            "alternative_explanations": ["The boundary is not observable."],
            "falsification_conditions": ["The transfer test is indistinguishable."],
            "novelty_status": "unknown_requires_prior_art_search",
            "confidence": "low",
            "readiness": "needs_more_literature",
        },
        {
            "hypothesis_id": "H03",
            "title": "Scaling branch",
            "statement": "A later scaling test will identify the practical size boundary.",
            "source_opportunity_ids": ["OP03"],
            "mechanism_rationale": "The gap motivates a bounded scaling check.",
            "supporting_paper_ids": [],
            "supporting_chunk_ids": [],
            "inference_chain": ["Scaling evidence is incomplete.", "A scaling test can probe it."],
            "assumptions": ["The size range can be represented."],
            "alternative_explanations": ["The boundary is fabrication limited."],
            "falsification_conditions": ["No size-dependent change is observed."],
            "novelty_status": "unknown_requires_prior_art_search",
            "confidence": "low",
            "readiness": "needs_more_literature",
        },
    ]
    gate = {
        "schema_version": "research_harness.program_focus_gate.v1",
        "gate_id": "PFG01",
        "main_problem": {
            "problem_id": "P01",
            "statement": "Coupled optical limits prevent reliable comparison.",
            "scope": "One controlled optical platform.",
            "boundary": "Do not expand beyond the platform.",
        },
        "project_type": "experiment",
        "shared_platform": {
            "platform_id": "PLAT01",
            "platform_type": "experiment",
            "name": "Controlled optical platform",
            "description": "A single calibrated measurement route.",
            "compatibility_key": "controlled_optical_measurement",
        },
        "boundaries": {
            "personnel": ["One optical team"],
            "equipment": ["One calibrated instrument"],
            "data": ["Reference measurements"],
            "timeline": ["A staged program"],
            "budget": ["A fixed budget"],
        },
        "unified_evaluation": {
            "metrics": [{"metric_id": "M01", "name": "separation"}],
            "baselines": [{"baseline_id": "B01", "name": "reference"}],
            "comparison_protocol": "Use the same reference and uncertainty procedure.",
        },
        "selected_opportunity_ids": ["OP01"],
        "main_hypothesis_ids": ["H01"],
        "future_hypothesis_ids": ["H02", "H03"],
        "hypothesis_dependencies": [],
        "future_branches": [
            {"opportunity_id": "OP02", "reason": "Deferred transfer.", "excluded_from_current_work_packages": True},
            {"opportunity_id": "OP03", "reason": "Deferred scaling.", "excluded_from_current_work_packages": True},
        ],
    }
    work_packages = []
    for index in range(1, 4):
        work_packages.append(
            {
                "work_package_id": f"WP{index:02d}",
                "title": f"Package {index}",
                "objective": "Mature and test the selected optical question.",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "methods": ["Run the controlled comparison."],
                "inputs": ["Canonical evidence and measurements."],
                "expected_outputs": ["A discriminating result."],
                "controls_or_baselines": ["Reference condition."],
                "evaluation_metrics": ["Separation metric."],
                "dependencies": [] if index == 1 else [f"WP{index - 1:02d}"],
                "risks": ["Parameter coupling."],
                "readiness": "needs_more_literature",
                "stop_or_pivot_criteria": ["If the control fails, pivot to a narrower comparison."],
                "platform_id": "PLAT01",
                "platform_compatibility_key": "controlled_optical_measurement",
                "metric_ids": ["M01"],
                "baseline_ids": ["B01"],
                "verification_status": "verification_deferred",
                "verification_rationale": "Planned work only; no experiment was executed.",
            }
        )
    matrix = [
        {
            "problem_id": "P01",
            "opportunity_id": "OP01",
            "hypothesis_id": "H01",
            "work_package_id": package["work_package_id"],
            "proposed_tests": ["Run the controlled comparison."],
            "metrics": ["M01"],
            "baselines": ["B01"],
            "falsification_conditions": ["The limits remain inseparable."],
            "stop_or_pivot_decisions": ["If the control fails, pivot to a narrower comparison."],
        }
        for package in work_packages
    ]
    if plan_matrix == "stale":
        matrix = [dict(matrix[0], work_package_id="WP99")]
    if focus_matrix == "valid":
        gate["traceability_matrix"] = [dict(item) for item in matrix]
    elif focus_matrix == "stale":
        gate["traceability_matrix"] = [dict(matrix[0], work_package_id="WP99")]
    else:
        gate["traceability_matrix"] = []
    plan = {
        "schema_version": "research_harness.research_plan.v2",
        "title": "A bounded optical research program",
        "research_question": "How can coupled optical limits be separated?",
        "strategy": "Use a staged evidence-aware comparison.",
        "objectives": ["Separate the limits"],
        "work_packages": work_packages,
        "milestones": ["Evidence freeze", "Controlled test"],
        "human_decision_points": ["If the reference comparison fails, pivot to a narrower comparison."],
        "unresolved_literature_needs": ["Prior-art search for orthogonal perturbations."],
        "readiness_summary": {"scope": "current_mainline"},
        "paper_abstract": "A proposed program for separating coupled optical limits.",
        "problem_statement": "Coupled optical limits prevent reliable comparison.",
        "rationale": "A controlled perturbation can separate the pathways.",
        "technical_details": ["Use calibrated optical measurements."],
        "dataset_source": ["Published optical references."],
        "dataset_target": ["New controlled measurements."],
        "methods_summary": ["Run a controlled comparison."],
        "experiments": ["Compare perturbations against the reference."],
        "expected_results": ["A separated response or a falsification."],
        "results_status": "verification_deferred",
        "reference_paper_ids": ["paper_A"],
        "verification_deferred": ["All experiments remain unrun."],
        "program_focus_gate_id": "PFG01",
        "main_problem": gate["main_problem"],
        "project_type": "experiment",
        "shared_platform": gate["shared_platform"],
        "boundaries": gate["boundaries"],
        "unified_evaluation": gate["unified_evaluation"],
        "main_hypothesis_ids": ["H01"],
        "future_hypothesis_ids": ["H02", "H03"],
        "hypothesis_dependencies": [],
        "future_branches": gate["future_branches"],
        "traceability_matrix": matrix,
        "source_context": provider.shared_review_context,
        "source_limitations": list(provider.shared_review_context.get("r4_candidate_limitations", [])),
        "main_hypothesis_statements": [{"hypothesis_id": "H01", "title": hypotheses[0]["title"], "statement": hypotheses[0]["statement"]}],
        "narrative_markdown": (
            "A controlled perturbation separates the two optical limits. "
            + "The program separates source-grounded evidence from proposed validation. " * 140
        ),
        "normalization_audit": [],
    }
    for name, payload in (
        ("RESEARCH_OPPORTUNITY_MAP.json", {"opportunities": opportunities}),
        ("HYPOTHESIS_PORTFOLIO.json", {"hypotheses": hypotheses}),
        ("PROGRAM_FOCUS_GATE.json", gate),
        (
            "RESEARCH_PROBLEM_FRAME.json",
            {
                "problem_id": "P01",
                "statement": gate["main_problem"]["statement"],
                "scope": gate["main_problem"]["scope"],
                "boundary": gate["main_problem"]["boundary"],
            },
        ),
        (
            "RESEARCH_GAP_MAP.json",
            {
                "gap_count": 1,
                "gaps": [
                    {
                        "gap_id": "G01",
                        "statement": "The controlled perturbation still needs direct validation.",
                    }
                ],
            },
        ),
        (
            "PROGRAM_SHARED_CONTEXT.json",
            provider.shared_review_context,
        ),
        ("RESEARCH_PLAN.json", plan),
        ("RESEARCH_PLAN_AUDIT.json", {"status": "passed", "errors": []}),
        ("RESEARCH_PLAN_CLEANUP_AUDIT.json", {"status": "passed", "independent_validation": {"status": "passed"}}),
    ):
        (work_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    (work_dir / "RESEARCH_PLAN.md").write_text(
        plan["narrative_markdown"], encoding="utf-8"
    )
    return provider


def test_r5_reconcile_clears_stale_preplan_focus_rows(tmp_path: Path):
    provider = _write_r5_reconciliation_fixture(
        tmp_path, plan_matrix="valid", focus_matrix="stale"
    )
    (provider.ctx.work_dir / "RESEARCH_PLAN.json").unlink()
    (provider.ctx.work_dir / "RESEARCH_PLAN.md").unlink()
    report = provider.reconcile_existing_r5_artifacts()
    gate = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "ready_for_plan_resume"
    assert gate["traceability_matrix"] == []
    audit = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json").read_text(
            encoding="utf-8"
        )
    )
    assert any(
        item.get("action") == "clear_focus_matrix_for_plan_only_rebuild"
        for item in audit["normalization_corrections"]
    )


def test_r5_reconcile_rehydrates_independently_valid_plan_matrix(tmp_path: Path):
    provider = _write_r5_reconciliation_fixture(
        tmp_path, plan_matrix="valid", focus_matrix="empty"
    )
    report = provider.reconcile_existing_r5_artifacts()
    gate = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").read_text(
            encoding="utf-8"
        )
    )
    plan = json.loads(
        (provider.ctx.work_dir / "RESEARCH_PLAN.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "ready_for_plan_resume"
    assert gate["traceability_matrix"] == plan["traceability_matrix"]
    assert _run_deterministic_program_validation(provider).startswith(
        "VALIDATION_PASSED:"
    )


def test_r5_reconcile_rejects_stale_plan_matrix(tmp_path: Path):
    provider = _write_r5_reconciliation_fixture(
        tmp_path, plan_matrix="stale", focus_matrix="valid"
    )
    report = provider.reconcile_existing_r5_artifacts()
    gate = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "ready_for_plan_resume"
    assert gate["traceability_matrix"] == []
    assert any(
        item.get("action") == "clear_focus_matrix_for_plan_only_rebuild"
        for item in json.loads(
            (provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json").read_text(
                encoding="utf-8"
            )
        )["normalization_corrections"]
    )


def test_r5_zero_model_deterministic_completion_after_cleanup(
    tmp_path: Path, monkeypatch
):
    from optomind_research.runtime.task_contract import TaskStatus

    provider = _write_r5_reconciliation_fixture(
        tmp_path, plan_matrix="valid", focus_matrix="empty"
    )
    from optomind_research.runtime import research_program_runner as runner

    class NoModelWorker:
        def __init__(self, **kwargs):
            raise AssertionError("deterministic completion unexpectedly built a worker")

    monkeypatch.setattr(runner, "ResearchWorker", NoModelWorker)
    result = run_research_program(
        provider.ctx,
        run_id="r5_zero_model_finalize",
        resume_plan_only=True,
        cost_budget_cny=0.01,
        token_budget=1,
        max_iters=1,
    )
    assert result.status == TaskStatus.completed
    assert result.validation_passed is True
    assert result.total_input_tokens == 0
    assert result.estimated_cost_cny == 0.0


def test_r5_repeated_resume_preserves_matrix_and_audit_history(tmp_path: Path):
    provider = _write_r5_reconciliation_fixture(
        tmp_path, plan_matrix="valid", focus_matrix="empty"
    )
    first = provider.reconcile_existing_r5_artifacts()
    first_gate = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").read_text(
            encoding="utf-8"
        )
    )
    first_audit = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json").read_text(
            encoding="utf-8"
        )
    )
    second = provider.reconcile_existing_r5_artifacts()
    second_gate = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").read_text(
            encoding="utf-8"
        )
    )
    second_audit = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json").read_text(
            encoding="utf-8"
        )
    )
    assert first["status"] == second["status"] == "ready_for_plan_resume"
    assert second["changed_existing_artifacts"] is False
    assert first_gate == second_gate
    assert second_audit["normalization_corrections"] == first_audit[
        "normalization_corrections"
    ]
    assert len(second_gate["traceability_matrix"]) == 3


def test_research_program_tools_enforce_traceability_and_falsifiability(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    tools = {
        tool.name: tool
        for tool in provider.get_tools(provider.ctx.work_dir)
    }
    context = json.loads(
        _tool_text(tools["load_research_program_context"])
    )
    assert context["allowlist"]["paper_count"] == 2
    assert context["evidence_identifier_catalog"]
    assert context["evidence_identifier_catalog"][0]["chunk_id"]


    evidence = json.loads(
        _tool_text(
            tools["inspect_research_evidence"],
            chunk_ids_json='["paper_A:chunk:0"]',
        )
    )
    assert evidence["chunks"][0]["paper_id"] == "paper_A"
    evidence_by_paper = json.loads(
        _tool_text(
            tools["inspect_research_evidence"],
            chunk_ids_json='["paper_A"]',
        )
    )
    assert evidence_by_paper["chunks"][0]["chunk_id"] == "paper_A:chunk:0"
    assert evidence_by_paper["resolved_identifiers"]["paper_A"] == [
        "paper_A:chunk:0"
    ]
    opportunities = {
        "opportunities": [
            {
                "opportunity_id": "OP01",
                "title": "Separate coupled limits",
                "problem": "The contributions of loss and dispersion remain confounded.",
                "why_it_matters": "A discriminating test would improve design decisions.",
                "origin_type": "method_gap",
                "source_section_ids": ["S01"],
                "supporting_paper_ids": ["paper_A"],
                "supporting_chunk_ids": ["paper_A:chunk:0"],
                "evidence_status": "partially_supported",
                "evidence_basis": "The source establishes the coupled constraint.",
                "author_inference": "Orthogonal perturbations could separate the two contributions.",
                "uncertainty": "The perturbations may not remain independent.",
                "recommended_next_evidence": ["Controlled parameter sweep"],
            },
            {
                "opportunity_id": "OP02",
                "title": "Define a transferable benchmark",
                "problem": "Reported evaluations are not directly comparable.",
                "why_it_matters": "A shared benchmark would expose trade-offs.",
                "origin_type": "benchmark_gap",
                "source_section_ids": ["S01"],
                "supporting_paper_ids": ["paper_B"],
                "supporting_chunk_ids": ["paper_B:chunk:1"],
                "evidence_status": "supported_boundary",
                "evidence_basis": "Independent evidence identifies the benchmark gap.",
                "author_inference": "A common protocol can make the boundary measurable.",
                "uncertainty": "The protocol may be platform dependent.",
                "recommended_next_evidence": ["Cross-platform dataset"],
            },
            {
                "opportunity_id": "OP03",
                "title": "Test an unexplored operating regime",
                "problem": "The review leaves one operating regime unmeasured.",
                "why_it_matters": "It may reveal a different limiting mechanism.",
                "origin_type": "evidence_gap",
                "source_section_ids": ["S01"],
                "supporting_paper_ids": [],
                "supporting_chunk_ids": [],
                "evidence_status": "open_gap",
                "evidence_basis": "No direct evidence was found in the reviewed corpus.",
                "author_inference": "The missing regime is informative because mechanisms scale differently.",
                "uncertainty": "The effect may be below measurement sensitivity.",
                "recommended_next_evidence": ["Targeted literature search"],
            },
        ]
    }
    assert json.loads(
        _tool_text(
            tools["submit_research_opportunity_map"],
            opportunity_map_json=json.dumps(opportunities),
        )
    )["status"] == "ok"

    hypotheses = {
        "hypotheses": [
            {
                "hypothesis_id": "H01",
                "title": "Orthogonal perturbation hypothesis",
                "statement": "Independent perturbations will separate loss and dispersion effects.",
                "source_opportunity_ids": ["OP01"],
                "mechanism_rationale": "The two variables enter the response through distinguishable pathways.",
                "supporting_paper_ids": ["paper_A"],
                "supporting_chunk_ids": ["paper_A:chunk:0"],
                "inference_chain": ["A coupled limit exists.", "A controlled perturbation can isolate each term."],
                "assumptions": ["Perturbations remain approximately orthogonal."],
                "alternative_explanations": ["A hidden variable controls both responses."],
                "falsification_conditions": ["Both perturbations produce indistinguishable response changes."],
                "novelty_status": "unknown_requires_prior_art_search",
                "confidence": "medium",
                "readiness": "needs_more_literature",
            },
            {
                "hypothesis_id": "H02",
                "title": "Benchmark ordering hypothesis",
                "statement": "A common benchmark will reverse at least one reported method ranking.",
                "source_opportunity_ids": ["OP02"],
                "mechanism_rationale": "Current metrics weight different trade-offs.",
                "supporting_paper_ids": ["paper_B"],
                "supporting_chunk_ids": ["paper_B:chunk:1"],
                "inference_chain": ["Metrics differ.", "Rankings depend on metric choice."],
                "assumptions": ["Raw outputs can be re-evaluated."],
                "alternative_explanations": ["All methods preserve their ranking."],
                "falsification_conditions": ["Rankings remain invariant across the shared protocol."],
                "novelty_status": "candidate_novelty",
                "confidence": "medium",
                "readiness": "ready",
            },
        ]
    }
    assert json.loads(
        _tool_text(
            tools["submit_hypothesis_portfolio"],
            hypothesis_portfolio_json=json.dumps(hypotheses),
        )
    )["status"] == "ok"

    focus_gate = {
        "schema_version": "research_harness.program_focus_gate.v1",
        "gate_id": "PFG01",
        "main_problem": {
            "problem_id": "P01",
            "statement": "Coupled loss and dispersion prevent reliable comparison of the optical platform.",
            "scope": "The shared optical measurement platform.",
            "boundary": "Do not expand into unrelated device architectures.",
        },
        "project_type": "experiment",
        "shared_platform": {
            "platform_id": "PLAT01",
            "name": "Controlled optical measurement platform",
            "description": "One reference sample, one perturbation protocol, and one analysis pipeline.",
            "compatibility_key": "controlled_optical_measurement",
        },
        "boundaries": {
            "personnel": ["One optical measurement team"],
            "equipment": ["One calibrated spectrometer"],
            "data": ["Reference and perturbation measurements"],
            "timeline": ["A staged twelve-month program"],
            "budget": ["A fixed instrument and sample budget"],
        },
        "unified_evaluation": {
            "metrics": [{"metric_id": "M01", "name": "effect separation"}],
            "baselines": [{"baseline_id": "B01", "name": "unmodified reference"}],
            "comparison_protocol": "Compare every condition with the same reference and uncertainty procedure.",
        },
        "selected_opportunity_ids": ["OP01", "OP02"],
        "main_hypothesis_ids": ["H01", "H02"],
        "future_hypothesis_ids": [],
        "hypothesis_dependencies": [{
            "upstream_hypothesis_id": "H01",
            "downstream_hypothesis_id": "H02",
            "reason": "The benchmark comparison depends on first separating the coupled effects.",
        }],
        "future_branches": [{
            "opportunity_id": "OP03",
            "reason": "The unexplored operating regime requires a later feasibility study.",
            "excluded_from_current_work_packages": True,
        }],
        "traceability_matrix": [
            {
                "problem_id": "P01", "opportunity_id": "OP01",
                "hypothesis_id": "H01", "work_package_id": "WP01",
                "proposed_tests": ["Audit the perturbation protocol"],
                "metrics": ["M01"], "baselines": ["B01"],
                "falsification_conditions": ["The effects remain inseparable"],
                "stop_or_pivot_decisions": ["Pivot to a simpler perturbation"],
            },
            {
                "problem_id": "P01", "opportunity_id": "OP01",
                "hypothesis_id": "H01", "work_package_id": "WP02",
                "proposed_tests": ["Run controlled measurements"],
                "metrics": ["M01"], "baselines": ["B01"],
                "falsification_conditions": ["The response changes are indistinguishable"],
                "stop_or_pivot_decisions": ["Stop if controls fail"],
            },
            {
                "problem_id": "P01", "opportunity_id": "OP02",
                "hypothesis_id": "H02", "work_package_id": "WP03",
                "proposed_tests": ["Evaluate the shared benchmark"],
                "metrics": ["M01"], "baselines": ["B01"],
                "falsification_conditions": ["Method rankings remain invariant"],
                "stop_or_pivot_decisions": ["Pivot if the benchmark is not transferable"],
            },
        ],
    }
    assert json.loads(
        _tool_text(
            tools["submit_program_focus_gate"],
            program_focus_gate_json=json.dumps(focus_gate),
        )
    )["status"] == "ok"

    wp_base = {
        "methods": ["Controlled analysis"],
        "inputs": ["Canonical evidence and new measurements"],
        "expected_outputs": ["A discriminating result"],
        "controls_or_baselines": ["Unmodified reference condition"],
        "evaluation_metrics": ["Effect separation and uncertainty"],
        "risks": ["Parameter coupling"],
        "readiness": "ready",
        "stop_or_pivot_criteria": ["Stop if controls cannot separate the variables."],
    }
    plan = {
        "title": "Evidence-aware optical research program",
        "research_question": "How can coupled limits be separated and benchmarked?",
        "strategy": (
            "Resolve the highest-information uncertainties before optimization. "
            "Preserve the distinction between source-grounded facts, author inference, "
            "and deferred validation. Compare the competing explanations under one "
            "shared protocol, record the decision conditions that can halt or redirect "
            "the program, and keep every proposed test linked to its hypothesis, "
            "opportunity, metric, baseline, and falsification condition."
        ),
        "objectives": ["Separate mechanisms", "Build a benchmark"],
        "work_packages": [
            {
                **wp_base,
                "work_package_id": "WP01",
                "title": "Evidence completion",
                "objective": "Complete the targeted prior-art and data audit.",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "dependencies": [],
                "readiness": "needs_more_literature",
            },
            {
                **wp_base,
                "work_package_id": "WP02",
                "title": "Discriminating experiment",
                "objective": "Separate the two effects.",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "dependencies": ["WP01"],
            },
            {
                **wp_base,
                "work_package_id": "WP03",
                "title": "Benchmark evaluation",
                "objective": "Compare methods under one protocol.",
                "hypothesis_ids": ["H02"],
                "opportunity_ids": ["OP02"],
                "dependencies": ["WP01"],
            },
        ],
        "milestones": ["Evidence freeze", "Controlled test", "Benchmark release"],
        "human_decision_points": [
            "When the feasibility review is complete, approve the operating range or pivot to a narrower range."
        ],
        "unresolved_literature_needs": ["Prior-art search for orthogonal perturbations."],
        "readiness_summary": {},
        "narrative_markdown": "# Research program\n\n" + (
            "The program separates established evidence from proposed tests and "
            "uses controlled baselines to evaluate each hypothesis. "
        ) * 130,
    }
    assert json.loads(
        _tool_text(
            tools["submit_research_plan"],
            research_plan_json=json.dumps(plan),
        )
    )["status"] == "ok"
    validation = _tool_text(
        tools["validate_research_program_package"]
    )
    assert "VALIDATION_PASSED" in validation
    audit = json.loads(
        (
            provider.ctx.work_dir / "RESEARCH_PLAN_AUDIT.json"
        ).read_text(encoding="utf-8")
    )
    assert audit["metrics"]["traceable_hypothesis_count"] == 2


def test_shared_context_preserves_awaiting_r4_candidate_limitations(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    (tmp_path / "FULL_REVIEW_PACKAGE.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.full_review_package.v1",
                "status": "awaiting_human_review",
                "total_flags": 8,
                "blocking_flags": 3,
                "sections_awaiting_human_review": ["S01"],
            }
        ),
        encoding="utf-8",
    )

    shared = provider._build_shared_review_context()
    technical = shared["technical_audit"]
    limitations = shared["r4_candidate_limitations"]
    assert technical["status"] == "awaiting_human_review"
    assert technical["artifact_ref"].endswith("FULL_REVIEW_PACKAGE.json")
    assert limitations
    assert any("candidate" in item.lower() for item in limitations)
    assert any("blocking review flags" in item for item in limitations)
    assert any("S01" in item for item in limitations)


def test_research_evidence_unknown_id_returns_real_examples(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    tools = {
        tool.name: tool
        for tool in provider.get_tools(provider.ctx.work_dir)
    }
    result = json.loads(
        _tool_text(
            tools["inspect_research_evidence_batch"],
            chunk_ids_json='["invented_chunk_001"]',
        )
    )
    assert result["chunks"] == []
    assert result["unknown_chunk_ids"] == ["invented_chunk_001"]
    assert "canonical chunk_id" in result["instruction"]


def test_initial_r5_context_uses_digests_and_bounded_batch_tools(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    provider.ctx.discovery_stage = "opportunity"
    tools = {tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)}
    context = json.loads(_tool_text(tools["load_research_program_context"]))
    assert "section_digests" in context
    assert "sections" not in context
    assert context["initial_discovery_protocol"][
        "single_section_reads_disabled_for_initial_discovery"
    ] is True
    digest = context["section_digests"][0]
    assert digest["section_id"] == "S01"
    assert digest["candidate_counts"]["chunks"] == 2
    assert "permission_counts" in digest
    assert "text" not in digest
    assert "candidate_chunk_ids" not in digest

    detail = json.loads(
        _tool_text(
            tools["read_review_sections_batch"],
            section_ids_json=json.dumps(["S01"]),
        )
    )
    assert detail["status"] == "ok"
    assert detail["sections"][0]["section_id"] == "S01"
    second_detail = json.loads(
        _tool_text(
            tools["read_review_sections_batch"],
            section_ids_json=json.dumps(["S01"]),
        )
    )
    assert second_detail["status"] == "ok"
    assert second_detail["cached"] is True
    assert second_detail["proceed_signal"] == "submit_research_opportunity_map"

    misuse = json.loads(
        _tool_text(
            tools["inspect_research_evidence_batch"],
            chunk_ids_json=json.dumps(["S01"]),
        )
    )
    assert misuse["error"] == "section_id_used_as_chunk_id"
    assert misuse["section_ids_misused_as_chunk_ids"] == ["S01"]

    evidence = json.loads(
        _tool_text(
            tools["inspect_research_evidence_batch"],
            chunk_ids_json=json.dumps(["paper_A:chunk:0"]),
        )
    )
    assert evidence["status"] == "ok"
    evidence_again = json.loads(
        _tool_text(
            tools["inspect_research_evidence_batch"],
            chunk_ids_json=json.dumps(["paper_B:chunk:1"]),
        )
    )
    assert evidence_again["status"] == "ok"
    assert evidence_again["cached"] is True
    assert evidence_again["chunks"] == evidence["chunks"]
    assert evidence_again["proceed_signal"] == "submit_research_opportunity_map"

    assert "read_review_section" not in tools
    assert "inspect_research_evidence" not in tools


def test_section_batch_expands_one_request_to_all_small_review_sections(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    extra_sections = [
        {
            "section_id": "S02",
            "title": "Method",
            "argument_role": "Establish the method boundary.",
            "chapter_argument": "Compare the available routes.",
            "synthesis_task": "Separate method choice from performance claims.",
        },
        {
            "section_id": "S03",
            "title": "Deployment",
            "argument_role": "Establish the deployment boundary.",
            "chapter_argument": "Connect laboratory evidence to use conditions.",
            "synthesis_task": "Separate demonstrated use from projection.",
        },
    ]
    provider.blueprint["sections"].extend(extra_sections)
    provider.review_sections.update(
        {
            "Method": "The method evidence is bounded.",
            "Deployment": "The deployment evidence is conditional.",
        }
    )
    provider._section_ids.update({"S02", "S03"})
    tools = {tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)}
    detail = json.loads(
        _tool_text(
            tools["read_review_sections_batch"],
            section_ids_json=json.dumps(["S02"]),
        )
    )
    assert detail["status"] == "ok"
    assert detail["expanded_to_all_digest_sections"] is True
    assert detail["section_ids_used"] == ["S01", "S02", "S03"]
    assert [item["section_id"] for item in detail["sections"]] == [
        "S01",
        "S02",
        "S03",
    ]
    assert detail["proceed_signal"] == "submit_research_opportunity_map"


def test_r5_discovery_resume_stage_protocol_uses_real_artifact_shapes(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    def opportunity(
        opportunity_id: str,
        title: str,
        status: str,
        chunk_ids: list[str],
        paper_ids: list[str],
    ) -> dict:
        return {
            "opportunity_id": opportunity_id,
            "title": title,
            "problem": "A bounded research limitation remains unresolved.",
            "why_it_matters": "Resolving it changes the interpretation of the review.",
            "origin_type": "method_gap",
            "source_section_ids": ["S01"],
            "supporting_paper_ids": paper_ids,
            "supporting_chunk_ids": chunk_ids,
            "evidence_status": status,
            "evidence_basis": "A canonical review chunk states the relevant boundary.",
            "author_inference": "A controlled follow-up could distinguish the competing explanations.",
            "uncertainty": "The result may depend on calibration and operating conditions.",
            "recommended_next_evidence": ["Compare the two bounded conditions."],
        }

    opportunities = {
        "schema_version": "research_harness.opportunity_map.v1",
        "opportunities": [
            opportunity(
                "OP01",
                "Separate the coupled limit",
                "supported_boundary",
                ["paper_A:chunk:0"],
                ["paper_A"],
            ),
            opportunity(
                "OP02",
                "Measure the benchmark gap",
                "partially_supported",
                ["paper_B:chunk:1"],
                ["paper_B"],
            ),
            opportunity(
                "OP03",
                "Explore the unmeasured regime",
                "open_gap",
                [],
                [],
            ),
        ],
    }
    (work_dir / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps(opportunities), encoding="utf-8"
    )

    assert _determine_r5_discovery_stage(provider) == "hypothesis"
    provider.ctx.discovery_stage = "hypothesis"
    hypothesis_tools = {
        tool.name for tool in provider.get_tools(work_dir)
    }
    assert hypothesis_tools == {
        "load_research_program_context",
        "submit_hypothesis_portfolio",
        "submit_program_focus_gate",
    }
    compact = json.loads(
        _tool_text(
            next(
                tool
                for tool in provider.get_tools(work_dir)
                if tool.name == "load_research_program_context"
            )
        )
    )
    assert compact["mode"] == "discovery_stage_resume"
    assert compact["stage"] == "hypothesis"
    assert compact["persisted_opportunities"]
    assert "sections" not in compact

    hypotheses = {
        "schema_version": "research_harness.hypothesis_portfolio.v1",
        "hypotheses": [
            {
                "hypothesis_id": "H01",
                "title": "Separate the coupled response",
                "statement": "A controlled perturbation separates the two pathways.",
                "source_opportunity_ids": ["OP01"],
                "mechanism_rationale": "The perturbation changes one pathway at a time.",
                "supporting_paper_ids": ["paper_A"],
                "supporting_chunk_ids": ["paper_A:chunk:0"],
                "inference_chain": ["The chunk states the coupling; the test separates it."],
                "assumptions": ["The perturbations are independently controllable."],
                "alternative_explanations": ["The apparent coupling is an artifact."],
                "falsification_conditions": ["The pathways remain inseparable."],
                "novelty_status": "candidate_novelty",
                "confidence": "medium",
                "readiness": "ready",
            },
            {
                "hypothesis_id": "H02",
                "title": "Characterize the unmeasured regime",
                "statement": "A bounded search will identify whether the regime differs.",
                "source_opportunity_ids": ["OP02"],
                "mechanism_rationale": "The benchmark gap motivates a bounded comparison.",
                "supporting_paper_ids": ["paper_B"],
                "supporting_chunk_ids": ["paper_B:chunk:1"],
                "inference_chain": ["The chunk states the gap; the search tests it."],
                "assumptions": ["The regime can be measured."],
                "alternative_explanations": ["The difference is an instrument artifact."],
                "falsification_conditions": ["No difference remains after calibration."],
                "novelty_status": "unknown_requires_prior_art_search",
                "confidence": "low",
                "readiness": "needs_more_literature",
            },
        ],
    }
    (work_dir / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps(hypotheses), encoding="utf-8"
    )
    assert _determine_r5_discovery_stage(provider) == "focus"
    # A real stage resume creates a fresh provider/AgentState.  Recreate the
    # provider here rather than reusing the hypothesis-stage context cache.
    focus_provider = ResearchProgramToolProvider(provider.ctx)
    focus_provider.ctx.discovery_stage = "focus"
    compact_focus = json.loads(
        _tool_text(
            next(
                tool
                for tool in focus_provider.get_tools(work_dir)
                if tool.name == "load_research_program_context"
            )
        )
    )
    assert compact_focus["stage"] == "focus"
    assert compact_focus["persisted_opportunities"]
    assert {item["opportunity_id"] for item in compact_focus["persisted_opportunities"]} == {
        "OP01", "OP02", "OP03"
    }
    assert {item["hypothesis_id"] for item in compact_focus["persisted_hypotheses"]} == {
        "H01", "H02"
    }
    assert set(compact_focus["persisted_opportunity_ids"]) == {
        "OP01", "OP02", "OP03"
    }
    assert set(compact_focus["persisted_hypothesis_ids"]) == {"H01", "H02"}
    focus_tools = {tool.name for tool in focus_provider.get_tools(work_dir)}
    assert focus_tools == {
        "load_research_program_context",
        "submit_program_focus_gate",
    }
    assert "read_review_sections_batch" not in focus_tools
    assert "submit_research_opportunity_map" not in focus_tools
    assert "submit_hypothesis_portfolio" not in focus_tools


def test_r5_discovery_resume_archives_old_agent_state(tmp_path: Path):
    work_dir = tmp_path / "program"
    work_dir.mkdir(parents=True)
    (work_dir / "AGENT_STATE.json").write_text(
        json.dumps({"old": "react history"}), encoding="utf-8"
    )
    archive = _archive_r5_agent_state_for_resume(
        work_dir,
        stage="hypothesis",
        run_id="metalens_resume_02",
    )
    assert archive is not None
    assert not (work_dir / "AGENT_STATE.json").exists()
    assert (archive / "AGENT_STATE.json").exists()
    audit = json.loads(
        (archive / "RESUME_ARCHIVE_AUDIT.json").read_text(encoding="utf-8")
    )
    assert audit["stage"] == "hypothesis"
    assert audit["run_id"] == "metalens_resume_02"
    resume_audit = json.loads(
        (work_dir / "R5_STAGE_RESUME_AUDIT.json").read_text(encoding="utf-8")
    )
    assert resume_audit["fresh_agent_state"] is True


def test_r5_phase_accounting_keeps_separate_deltas_and_total(tmp_path: Path):
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    cost_path = tmp_path / "COST.json"
    before = {
        "model_call_count": 0,
        "tool_call_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "wall_time_seconds": 0.0,
    }
    cost_path.write_text(
        json.dumps(
            {
                "model_call_count": 2,
                "tool_call_count": 3,
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "estimated_cost_cny": 0.1,
                "wall_time_seconds": 2.0,
            }
        ),
        encoding="utf-8",
    )
    result = ResultManifest(
        run_id="r5",
        task_id="research_program",
        status=TaskStatus.completed,
    )
    _record_r5_phase_accounting(
        ResearchProgramContext(
            blueprint_path=tmp_path / "blueprint.json",
            final_review_path=tmp_path / "review.md",
            coverage_root=tmp_path / "coverage",
            work_dir=tmp_path,
        ),
        phase="initial_discovery",
        run_id="r5",
        before_cost=before,
        result=result,
    )
    accounting = json.loads(
        (tmp_path / "R5_PHASE_ACCOUNTING.json").read_text(encoding="utf-8")
    )
    assert accounting["phases"]["initial_discovery"]["totals"]["model_calls"] == 2
    assert accounting["total"]["input_tokens"] == 100
    assert accounting["total"]["estimated_cost_cny"] == 0.1


def test_r5_phase_accounting_rejects_zero_usage_plan_only_without_plan(
    tmp_path: Path,
):
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path,
    )
    result = ResultManifest(
        run_id="same_run",
        task_id="research_program",
        status=TaskStatus.completed,
        stop_reason="reused_focus_result",
    )
    zero_cost = {
        "run_id": "same_run",
        "task_id": "research_program",
        "model_call_count": 0,
        "tool_call_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "estimated_cost_cny": 0.0,
    }
    _record_r5_phase_accounting(
        context,
        phase="plan_only",
        run_id="same_run",
        before_cost=zero_cost,
        result=result,
    )
    accounting = json.loads(
        (tmp_path / "R5_PHASE_ACCOUNTING.json").read_text(encoding="utf-8")
    )
    row = accounting["phases"]["plan_only"]["runs"][0]
    assert row["status"] == "validation_failed"
    assert row["worker_status"] == "completed"
    assert row["completion_gate"] == "plan_artifacts_and_passed_audit_required"


def test_r5_legacy_reconciliation_adds_new_run_usage_without_losing_baseline(
    tmp_path: Path,
):
    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path,
    )
    (tmp_path / "COST.json").write_text(
        json.dumps(
            {
                "run_id": "resumed_plan_run",
                "task_id": "research_program",
                "total_input_tokens": 93_712,
                "total_output_tokens": 26_669,
                "estimated_cost_cny": 0.400776,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "R5_BUDGET.json").write_text(
        json.dumps(
            {
                "scope": "entire_r5_run",
                "baseline": {
                    "input_tokens": 112_601,
                    "estimated_cost_cny": 0.377674,
                },
                "current": {
                    "input_tokens": 93_712,
                    "estimated_cost_cny": 0.400776,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "R5_PHASE_ACCOUNTING.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.r5_phase_accounting.v1",
                "total": {
                    "input_tokens": 93_712,
                    "estimated_cost_cny": 0.400776,
                },
            }
        ),
        encoding="utf-8",
    )

    state = _load_r5_lifetime_state(
        context,
        json.loads((tmp_path / "COST.json").read_text(encoding="utf-8")),
    )

    assert state["lifetime_total"]["input_tokens"] == 206_313
    assert state["lifetime_total"]["estimated_cost_cny"] == 0.77845
    assert state["historical_ambiguity"] is True
    assert state["source"] == "legacy_baseline_plus_current_ledger"
    assert state["raw_observations"]["budget_baseline"]["input_tokens"] == 112_601


def test_r5_phase_accounting_new_run_id_is_monotonic_and_records_delta(
    tmp_path: Path,
):
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path,
    )
    (tmp_path / "COST.json").write_text(
        json.dumps(
            {
                "run_id": "new_run",
                "task_id": "research_program",
                "total_input_tokens": 3_000,
                "total_output_tokens": 400,
                "model_call_count": 1,
                "tool_call_count": 1,
                "estimated_cost_cny": 0.03,
            }
        ),
        encoding="utf-8",
    )
    result = ResultManifest(
        run_id="new_run",
        task_id="research_program",
        status=TaskStatus.completed,
    )
    _record_r5_phase_accounting(
        context,
        phase="plan_only",
        run_id="new_run",
        before_cost={
            "run_id": "old_run",
            "task_id": "research_program",
            "total_input_tokens": 112_601,
            "estimated_cost_cny": 0.377674,
        },
        result=result,
        budget_state={
            "current": {
                "input_tokens": 112_601,
                "estimated_cost_cny": 0.377674,
            },
            "ceiling": {
                "input_tokens": 172_601,
                "estimated_cost_cny": 1.177674,
            },
        },
    )
    accounting = json.loads(
        (tmp_path / "R5_PHASE_ACCOUNTING.json").read_text(encoding="utf-8")
    )
    assert accounting["schema_version"] == "research_harness.r5_phase_accounting.v2"
    assert accounting["total"]["input_tokens"] == 115_601
    assert accounting["total"]["estimated_cost_cny"] == 0.407674
    assert accounting["phases"]["plan_only"]["totals"]["input_tokens"] == 3_000
    assert accounting["budget"]["current"]["input_tokens"] == 115_601
    assert accounting["budget"]["invocation"]["ledger_mode"] == "fresh_run_id"


def test_research_program_resume_uses_deterministic_validation_before_model(
    tmp_path: Path,
    monkeypatch,
):
    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "RESEARCH_OPPORTUNITY_MAP.json",
        "HYPOTHESIS_PORTFOLIO.json",
        "RESEARCH_PLAN.json",
    ):
        (work_dir / name).write_text("{}", encoding="utf-8")
    (work_dir / "RESEARCH_PLAN.md").write_text(
        "# Existing research plan",
        encoding="utf-8",
    )
    (work_dir / "RESEARCH_PLAN_AUDIT.json").write_text(
        json.dumps({"status": "passed"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "optomind_research.runtime.research_program_runner."
        "_run_deterministic_program_validation",
        lambda _provider: "VALIDATION_PASSED: durable package accepted.",
    )

    def fail_if_model_runs(*args, **kwargs):
        raise AssertionError("model must not run for a validated durable package")

    monkeypatch.setattr(
        "optomind_research.runtime.research_program_runner.ResearchWorker.run",
        fail_if_model_runs,
    )
    result = run_research_program(
        provider.ctx,
        run_id="deterministic_resume",
    )
    assert result.status.value == "completed"
    assert result.validation_passed is True
    assert result.stop_reason == "deterministic_post_validation_passed"


def test_research_program_rejects_fabricated_evidence_id(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    tools = {
        tool.name: tool
        for tool in provider.get_tools(provider.ctx.work_dir)
    }
    bad = {
        "opportunities": [
            {
                "opportunity_id": f"OP0{index}",
                "title": "Candidate",
                "problem": "A substantive problem is present.",
                "why_it_matters": "It affects the scientific decision.",
                "origin_type": "evidence_gap",
                "source_section_ids": ["S01"],
                "supporting_paper_ids": ["fabricated_paper"],
                "supporting_chunk_ids": [],
                "evidence_status": "partially_supported",
                "evidence_basis": "Claimed evidence.",
                "author_inference": "This inference is long enough to be explicit.",
                "uncertainty": "The premise may be wrong.",
                "recommended_next_evidence": ["Check the source"],
            }
            for index in range(1, 4)
        ]
    }
    result = json.loads(
        _tool_text(
            tools["submit_research_opportunity_map"],
            opportunity_map_json=json.dumps(bad),
        )
    )
    assert result["status"] == "error"
    assert any("unknown_paper" in error for error in result["errors"])


def test_research_program_normalizes_common_model_aliases_without_retry():
    raw = {
        "title": "Program",
        "research_question": "What should be tested?",
        "strategy": "De-risk the measurement before fabrication.",
        "objectives": "Build a benchmark; test one mechanism",
        "work_packages": [
            {
                "id": "wp_1",
                "title": "Benchmark protocol",
                "linked_hypotheses": "H1",
                "linked_opportunities": ["OPP-01"],
                "methods": "Audit methods; run controlled measurements",
                "inputs": "Canonical evidence; reference sample",
                "outputs": "Protocol; benchmark data",
                "controls_baselines": "Commercial reference",
                "evaluation_metrics": "Repeatability; uncertainty",
                "dependencies": "None.",
                "risks": "Instrument drift",
                "readiness": "ready",
                "stop_pivot_criteria": "Pivot if repeatability is inadequate",
            }
        ],
        "milestones": "Protocol freeze",
        "human_decision_points": "Approve the operating range",
        "unresolved_literature_needs": "Independent prior-art search",
        "readiness_summary": "One work package is ready.",
        "narrative_markdown": "# Program\n\n" + "Operational rationale. " * 50,
    }
    repaired = _normalize_plan_payload(raw)
    model = ResearchPlan.model_validate(repaired)
    assert model.work_packages[0].work_package_id == "WP01"
    assert model.work_packages[0].hypothesis_ids == ["H01"]
    assert model.work_packages[0].opportunity_ids == ["OP01"]
    assert model.work_packages[0].dependencies == []
    rendered = _render_research_plan_markdown(model.model_dump())
    assert "Controls and baselines" in rendered
    assert "Stop or pivot criteria" in rendered


def test_research_program_renders_structured_milestones_as_prose():
    repaired = _normalize_plan_payload(
        {
            "work_packages": [],
            "milestones": [
                {
                    "id": "M1",
                    "description": "Freeze the measurement protocol",
                    "timeline": "Month 3",
                }
            ],
            "human_decision_points": [
                {
                    "id": "DP1",
                    "description": "Decide whether to fabricate",
                    "trigger": "Tolerance study complete",
                }
            ],
        }
    )
    assert repaired["milestones"] == [
        "M1: Freeze the measurement protocol (timeline: Month 3)"
    ]
    assert repaired["human_decision_points"] == [
        (
            "DP1: Decide whether to fabricate "
            "(trigger: Tolerance study complete)"
        )
    ]
    assert "{'id':" not in json.dumps(repaired)


def test_research_plan_flattens_python_literal_experiment_entries_safely():
    repaired = _normalize_plan_payload(
        {
            "work_packages": [],
            "experiments": [
                "{'experiment_id': 'EXP01', 'description': 'Run the planned comparison', 'verification_deferred': True}"
            ],
            "expected_results": [
                "{'summary': 'No result is claimed', 'status': 'verification_deferred'}"
            ],
        }
    )
    assert repaired["experiments"][0].startswith("Experiment EXP01:")
    assert "verification_deferred" in repaired["experiments"][0]
    assert repaired["expected_results"][0].startswith("Summary:")
    assert "{'" not in json.dumps(repaired, ensure_ascii=False)
    # The parser is literal-only; executable expressions are retained as
    # ordinary text rather than evaluated.
    unsafe = _normalize_plan_payload(
        {
            "work_packages": [],
            "experiments": ["__import__('os').system('echo unsafe')"],
        }
    )
    assert "__import__" in unsafe["experiments"][0]


def test_human_decision_points_remove_empty_labels_and_require_choices():
    repaired = _normalize_plan_payload(
        {
            "work_packages": [],
            "human_decision_points": [
                "HD01:",
                "When the feasibility review is complete, approve the route or pivot to a narrower scope.",
            ],
        }
    )
    assert repaired["human_decision_points"] == [
        "When the feasibility review is complete, approve the route or pivot to a narrower scope."
    ]
    assert any(
        item["action"] == "remove_insubstantial_human_decision_point"
        for item in repaired["normalization_audit"]
    )
    assert not _decision_point_is_substantive("HD01:")
    assert _decision_point_is_substantive(repaired["human_decision_points"][0])


def test_empty_human_decision_points_are_recovered_from_existing_stop_criteria():
    source_criteria = [
        "Stop if the control fails to separate the variables; otherwise proceed with the comparison.",
        "When the calibration error exceeds the proposed limit, pivot to the narrower operating range.",
        "If the measurement remains unstable after repeat runs, escalate to an independent review.",
        "Record the planned output for the next run.",
    ]
    repaired = _normalize_plan_payload(
        {
            "work_packages": [
                {
                    "work_package_id": "WP01",
                    "stop_or_pivot_criteria": source_criteria,
                }
            ],
            "human_decision_points": ["HD01:", "HD02:", "HD03:"],
        }
    )
    points = repaired["human_decision_points"]
    assert [point.split(":", 1)[0] for point in points] == [
        "HD01",
        "HD02",
        "HD03",
    ]
    assert all(_decision_point_is_substantive(point) for point in points)
    assert all(source in "\n".join(points) for source in source_criteria[:3])
    assert source_criteria[3] not in "\n".join(points)
    recovery = [
        item
        for item in repaired["normalization_audit"]
        if item["action"] == "recover_human_decision_point_from_stop_criteria"
    ]
    assert len(recovery) == 3
    assert all(item["source_work_package_id"] == "WP01" for item in recovery)


def test_empty_human_decision_points_still_fail_without_substantive_stop_criteria():
    repaired = _normalize_plan_payload(
        {
            "work_packages": [
                {
                    "work_package_id": "WP01",
                    "stop_or_pivot_criteria": [
                        "Record the planned output.",
                        "Use the next dataset.",
                    ],
                }
            ],
            "human_decision_points": ["HD01:", "HD02:"],
        }
    )
    assert repaired["human_decision_points"] == []
    assert not any(
        item["action"] == "recover_human_decision_point_from_stop_criteria"
        for item in repaired["normalization_audit"]
    )
    assert "human_decision_points_missing_or_insubstantial" in _audit_rendered_plan_content(
        repaired,
        "",
    )


def test_readiness_summary_separates_current_and_future_packages():
    summary = _build_readiness_summary(
        {
            "work_packages": [
                {"work_package_id": "WP01", "hypothesis_ids": ["H01"], "opportunity_ids": ["OP01"], "readiness": "ready"},
                {"work_package_id": "WP02", "hypothesis_ids": ["H02"], "opportunity_ids": ["OP02"], "readiness": "ready"},
            ]
        },
        {
            "main_hypothesis_ids": ["H01"],
            "selected_opportunity_ids": ["OP01"],
            "future_hypothesis_ids": ["H02"],
            "future_branches": [{"opportunity_id": "OP02"}],
        },
    )
    assert summary["scope"] == "current_mainline"
    assert summary["current_mainline"]["readiness_counts"]["ready"] == 1
    assert summary["all_submitted_packages"]["readiness_counts"]["ready"] == 2
    assert summary["future_branch_packages_excluded_from_current"]["package_count"] == 1


def test_numeric_stop_threshold_is_explicitly_a_proposed_calibration_target():
    repaired = _normalize_plan_payload(
        {
            "work_packages": [
                {
                    "work_package_id": "WP01",
                    "methods": ["Run the planned comparison"],
                    "inputs": ["A future dataset"],
                    "expected_outputs": ["A decision record"],
                    "controls_or_baselines": ["A matched baseline"],
                    "evaluation_metrics": ["A discriminating metric"],
                    "risks": ["The effect may be confounded"],
                    "readiness": "needs_more_literature",
                    "stop_or_pivot_criteria": [
                        "Stop if the phase deviation exceeds 0.1 rad."
                    ],
                }
            ]
        }
    )
    package = repaired["work_packages"][0]
    assert package["quantitative_target_status"] == "proposed_program_target"
    assert package["quantitative_target_provenance"] == "proposed_calibration_target"
    assert "proposed calibration target" in package["stop_or_pivot_criteria"][0].casefold()


def test_rendered_plan_content_gate_rejects_internal_and_repr_text():
    plan = {
        "experiments": ["{'experiment_id': 'EXP01'}"],
        "expected_results": ["{'status': 'verification_deferred'}"],
        "human_decision_points": ["HD01:"],
        "readiness_summary": {"ready": 1},
        "work_packages": [],
    }
    issues = _audit_rendered_plan_content(
        plan,
        "- {'experiment_id': 'EXP01'}\n- HD01:\nThis workflow uses a model turn.",
    )
    assert "experiments_contains_dict_repr" in issues
    assert "human_decision_points_missing_or_insubstantial" not in issues
    assert "human_decision_point_0_insubstantial" in issues
    assert "readiness_summary_scope_not_declared_as_current_mainline" in issues
    assert "rendered_plan_contains_internal_process_wording" in issues


def test_renderer_does_not_inject_duplicate_structured_sections_from_narrative():
    plan = {
        "title": "A Structured Research Program",
        "paper_abstract": "A planned study.",
        "problem_statement": "A bounded problem.",
        "rationale": "A bounded rationale.",
        "strategy": "Resolve the highest-value uncertainty with staged validation.",
        "main_problem": {"statement": "A bounded problem."},
        "project_type": "planned study",
        "shared_platform": {"name": "Shared platform", "description": "A common setup."},
        "boundaries": {},
        "unified_evaluation": {"metrics": [], "baselines": [], "comparison_protocol": ""},
        "main_hypothesis_statements": [
            {"hypothesis_id": "H01", "title": "Mechanism", "statement": "The mechanism is testable."}
        ],
        "readiness_summary": {
            "scope": "current_mainline",
            "current_mainline": {"package_count": 1, "readiness_counts": {"needs_more_literature": 1}},
            "all_submitted_packages": {"package_count": 1, "readiness_counts": {"needs_more_literature": 1}},
            "future_branch_packages_excluded_from_current": {"package_count": 0, "readiness_counts": {}},
        },
        "narrative_markdown": (
            "## Main Hypothesis Statements\n\n"
            "### H01: Mechanism\n\nThe mechanism is testable.\n\n"
            "## Work Packages\n\n### WP01: Duplicate package\n\n"
            "Narrative-only text that must not be rendered.\n\n"
            "## Human decision points\n\n- HD01: duplicate label"
        ),
        "future_branches": [],
        "traceability_matrix": [
            {
                "problem_id": "P01",
                "opportunity_id": "OP01",
                "hypothesis_id": "H01",
                "work_package_id": "WP01",
                "proposed_tests": ["Test the mechanism."],
                "metrics": ["M01"],
                "baselines": ["B01"],
                "falsification_conditions": ["If the mechanism fails."],
                "stop_or_pivot_decisions": ["Stop if the test fails; otherwise proceed."],
            }
        ],
        "source_limitations": [],
        "technical_details": [],
        "dataset_source": [],
        "dataset_target": [],
        "methods_summary": [],
        "experiments": [],
        "expected_results": [],
        "verification_deferred": ["No execution."],
        "research_question": "A bounded problem.",
        "objectives": [],
        "work_packages": [
            {
                "work_package_id": "WP01",
                "title": "Mechanism test",
                "objective": "Test the mechanism.",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "readiness": "needs_more_literature",
                "quantitative_target_status": "none",
                "verification_status": "verification_deferred",
                "verification_rationale": "No execution.",
                "methods": ["A planned test."],
                "inputs": [],
                "expected_outputs": [],
                "controls_or_baselines": [],
                "evaluation_metrics": [],
                "dependencies": [],
                "risks": [],
                "stop_or_pivot_criteria": ["Stop if the test fails; otherwise proceed."],
            }
        ],
        "milestones": [],
        "human_decision_points": [
            "HD01: When the test is complete, approve the route or pivot to a narrower scope."
        ],
        "unresolved_literature_needs": [],
        "reference_paper_ids": [],
    }
    markdown = _render_research_plan_markdown(plan)
    assert "Narrative-only text that must not be rendered." not in markdown
    assert "Resolve the highest-value uncertainty with staged validation." in markdown
    assert markdown.count("## Main Hypothesis Statements") == 1
    assert markdown.count("## Traceability Matrix") == 1
    assert markdown.count("## Human decision points") == 1
    assert markdown.count("### WP01:") == 1
    assert "The mechanism is testable." in markdown
    issues = _audit_rendered_plan_content(plan, markdown)
    assert not any(item.startswith("rendered_plan_duplicate_") for item in issues)


def test_research_program_detects_unsupported_exact_narrative_number():
    unsupported = _unsupported_narrative_quantitative_claims(
        "The device inherits a thermal drift of 20 nm/\u00b0C in field use."
    )
    assert len(unsupported) == 1
    assert _unsupported_narrative_quantitative_claims(
        (
            "The program proposes 20 nm/\u00b0C as a conservative design "
            "target to be calibrated."
        )
    ) == []
    assert _unsupported_narrative_quantitative_claims(
        "WP02 runs from Month 3 to Month 6."
    ) == []
    assert len(
        _unsupported_narrative_quantitative_claims(
            (
                "Reported quality factors (>2000) exceed the molecular "
                "requirement."
            )
        )
    ) == 1
    assert len(
        _unsupported_narrative_quantitative_claims(
            (
                "Atmospheric absorption corresponds to Q-factors in the "
                "range of 300\u2013800."
            )
        )
    ) == 1


def test_research_program_migrates_unsupported_narrative_number(
    tmp_path: Path,
):
    (tmp_path / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps({"hypotheses": []}),
        encoding="utf-8",
    )
    (tmp_path / "RESEARCH_PLAN.json").write_text(
        json.dumps(
            {
                "narrative_markdown": (
                    "Published devices exhibit 20 nm/\u00b0C thermal drift."
                ),
                "work_packages": [],
            }
        ),
        encoding="utf-8",
    )
    assert _requires_quantitative_provenance_migration(tmp_path) is True


def test_research_program_rebuild_budget_is_incremental(
    tmp_path: Path,
    monkeypatch,
):
    from optomind_research.runtime import research_program_runner as module

    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path / "program",
    )
    context.work_dir.mkdir(parents=True)
    context.blueprint_path.write_text(
        json.dumps({"sections": []}), encoding="utf-8"
    )
    context.final_review_path.write_text("", encoding="utf-8")
    (context.work_dir / "COST.json").write_text(
        json.dumps(
            {
                "run_id": "run",
                "task_id": "research_program",
                "total_input_tokens": 220_000,
                "estimated_cost_cny": 3.5,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            captured["contract"] = contract
            from optomind_research.runtime.task_contract import (
                ResultManifest,
                TaskStatus,
            )

            return ResultManifest(
                run_id="run",
                task_id="research_program",
                status=TaskStatus.budget_exhausted,
                stop_reason="test",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    run_research_program(
        context,
        run_id="run",
        cost_budget_cny=4.0,
        token_budget=240_000,
    )
    contract = captured["contract"]
    assert contract.token_budget == 460_000
    assert contract.cost_budget_cny == 7.5
    assert contract.max_iters == 5
    assert "read_review_sections_batch" in contract.allowed_tools
    assert "inspect_research_evidence_batch" in contract.allowed_tools
    assert "load_research_program_context" in contract.allowed_tools
    assert "submit_research_opportunity_map" in contract.allowed_tools
    assert "read_review_section" not in contract.allowed_tools
    assert "inspect_research_evidence" not in contract.allowed_tools
    assert "submit_hypothesis_portfolio" not in contract.allowed_tools
    assert "submit_program_focus_gate" not in contract.allowed_tools
    assert "submit_research_plan" not in contract.allowed_tools
    assert contract.metadata["r5_discovery_stage"] == "opportunity"


def test_r5_focus_stage_allows_one_repair_before_terminal_reserve(
    tmp_path: Path,
    monkeypatch,
):
    """Focus resumes must allow load, submit, one repair, then reserve."""

    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    def make_opportunity(
        opportunity_id: str,
        title: str,
        status: str,
        paper_id: str = "",
        chunk_id: str = "",
    ) -> dict:
        return {
            "opportunity_id": opportunity_id,
            "title": title,
            "problem": "A bounded research limitation remains unresolved.",
            "why_it_matters": "Resolving it changes the interpretation of the review.",
            "origin_type": "evidence_gap",
            "source_section_ids": ["S01"],
            "supporting_paper_ids": [paper_id] if paper_id else [],
            "supporting_chunk_ids": [chunk_id] if chunk_id else [],
            "evidence_status": status,
            "evidence_basis": "A canonical review chunk states the relevant boundary.",
            "author_inference": "A bounded follow-up could distinguish the competing explanations.",
            "uncertainty": "The result may depend on calibration and operating conditions.",
            "recommended_next_evidence": ["Compare the two bounded conditions."],
        }

    (work_dir / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.opportunity_map.v1",
                "opportunities": [
                    make_opportunity(
                        "OP01", "Separate the coupled limit", "supported_boundary",
                        "paper_A", "paper_A:chunk:0",
                    ),
                    make_opportunity(
                        "OP02", "Measure the benchmark gap", "partially_supported",
                        "paper_B", "paper_B:chunk:1",
                    ),
                    make_opportunity(
                        "OP03", "Explore the unmeasured regime", "open_gap",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    def make_hypothesis(hypothesis_id: str, opportunity_id: str) -> dict:
        return {
            "hypothesis_id": hypothesis_id,
            "title": f"Hypothesis {hypothesis_id}",
            "statement": "A bounded perturbation separates the competing pathways.",
            "source_opportunity_ids": [opportunity_id],
            "mechanism_rationale": "The perturbation changes one pathway at a time.",
            "supporting_paper_ids": ["paper_A"],
            "supporting_chunk_ids": ["paper_A:chunk:0"],
            "inference_chain": ["The review identifies a coupled limit."],
            "assumptions": ["The perturbations remain independently controllable."],
            "alternative_explanations": ["The apparent coupling is an artifact."],
            "falsification_conditions": ["The pathways remain inseparable."],
            "novelty_status": "unknown_requires_prior_art_search",
            "confidence": "medium",
            "readiness": "needs_more_literature",
        }

    (work_dir / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.hypothesis_portfolio.v1",
                "hypotheses": [
                    make_hypothesis("H01", "OP01"),
                    make_hypothesis("H02", "OP02"),
                ],
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            captured["contract"] = contract
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.waiting_for_human,
                stop_reason="offline_focus_stage_probe",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    run_research_program(
        provider.ctx,
        run_id="focus_stage_probe",
        model_tier="advanced_model",
        cost_budget_cny=0.2,
        token_budget=1000,
        max_iters=8,
    )

    contract = captured["contract"]
    assert contract.max_iters == 4
    assert contract.metadata["r5_discovery_stage"] == "focus"
    assert contract.metadata["stage_iteration_reserve"] == 1
    assert contract.allowed_tools == [
        "load_research_program_context",
        "submit_program_focus_gate",
    ]


def test_focus_to_plan_only_same_run_cannot_reuse_focus_result(
    tmp_path: Path,
    monkeypatch,
):
    """A focus RESULT cannot complete plan-only when the plan is absent."""

    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    calls = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.work_dir = kwargs["_work_dir_override"]

        def run(self, contract):
            calls.append(contract)
            if len(calls) == 1:
                focus = {
                    "status": "passed",
                    "main_hypothesis_ids": ["H01"],
                }
                (self.work_dir / "PROGRAM_FOCUS_GATE.json").write_text(
                    json.dumps(focus), encoding="utf-8"
                )
                focus_result = ResultManifest(
                    run_id=contract.run_id,
                    task_id=contract.task_id,
                    status=TaskStatus.completed,
                    stop_reason="focus_gate_passed",
                )
                (self.work_dir / "RESULT.json").write_text(
                    json.dumps(focus_result.model_dump()), encoding="utf-8"
                )
                (self.work_dir / "RESULT.md").write_text(
                    "# Focus result\n", encoding="utf-8"
                )
                (self.work_dir / "TASK.json").write_text(
                    json.dumps(contract.model_dump(mode="json")),
                    encoding="utf-8",
                )
                (self.work_dir / "PLAN.md").write_text(
                    "# Focus task plan\n", encoding="utf-8"
                )
                (self.work_dir / "AGENT_STATE.json").write_text(
                    "{}", encoding="utf-8"
                )
                return focus_result

            # The phase handoff must remove the old completion marker before
            # the plan-only worker is constructed.
            assert not (self.work_dir / "RESULT.json").exists()
            assert not (self.work_dir / "TASK.json").exists()
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.waiting_for_human,
                stop_reason="plan_only_probe_pending",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    monkeypatch.setattr(
        module.ResearchProgramToolProvider,
        "reconcile_existing_r5_artifacts",
        lambda self: {"status": "ready_for_plan_resume"},
    )

    result = run_research_program(
        provider.ctx,
        run_id="same_focus_plan_run",
        model_tier="advanced_model",
        cost_budget_cny=0.2,
        token_budget=1000,
        max_iters=4,
    )

    assert len(calls) == 2
    assert calls[0].metadata["phase_identity"].startswith(
        "research_program:initial_discovery:"
    )
    assert calls[1].metadata["phase_identity"] == (
        "research_program:plan_only:plan_only"
    )
    assert result.status == TaskStatus.waiting_for_human
    assert not (work_dir / "RESEARCH_PLAN.json").exists()
    handoffs = list(
        (work_dir / "_runtime_archive").glob(
            "r5_phase_handoff_initial_discovery_to_plan_only_same_focus_plan_run_*"
        )
    )
    assert len(handoffs) == 1
    assert (handoffs[0] / "RESULT.json").exists()
    assert (work_dir / "R5_PHASE_HANDOFF_AUDIT.json").exists()


def test_r5_recursive_plan_only_uses_remaining_root_budget(
    tmp_path: Path,
    monkeypatch,
):
    """A 70% discovery phase cannot re-open a second full CLI allowance."""

    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path / "program",
    )
    context.work_dir.mkdir(parents=True)
    context.blueprint_path.write_text(
        json.dumps({"sections": []}), encoding="utf-8"
    )
    context.final_review_path.write_text("", encoding="utf-8")
    calls = []

    def write_cost(input_tokens: int, estimated_cost_cny: float) -> None:
        (context.work_dir / "COST.json").write_text(
            json.dumps(
                {
                    "run_id": "r5_budget_root",
                    "task_id": "research_program",
                    "total_input_tokens": input_tokens,
                    "total_output_tokens": 0,
                    "model_call_count": 1 if input_tokens else 0,
                    "tool_call_count": 0,
                    "estimated_cost_cny": estimated_cost_cny,
                }
            ),
            encoding="utf-8",
        )

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            calls.append(contract)
            if len(calls) == 1:
                # Discovery has consumed 70% of the single root allowance.
                (context.work_dir / "PROGRAM_FOCUS_GATE.json").write_text(
                    json.dumps({"status": "passed"}), encoding="utf-8"
                )
                write_cost(700, 0.7)
                return ResultManifest(
                    run_id=contract.run_id,
                    task_id=contract.task_id,
                    status=TaskStatus.completed,
                    stop_reason="focus_gate_passed",
                )
            # The plan-only phase may see only the remaining 30%; it must not
            # be given a new 100% ceiling.
            write_cost(1000, 1.0)
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.budget_exhausted,
                stop_reason="remaining_budget_exhausted",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    monkeypatch.setattr(
        module.ResearchProgramToolProvider,
        "reconcile_existing_r5_artifacts",
        lambda self: {"status": "ready_for_plan_resume"},
    )

    result = run_research_program(
        context,
        run_id="r5_budget_root",
        cost_budget_cny=1.0,
        token_budget=1000,
        max_iters=8,
    )

    assert result.status == TaskStatus.waiting_for_human
    assert len(calls) == 2
    discovery_contract, plan_contract = calls
    assert discovery_contract.token_budget == 1000
    assert discovery_contract.cost_budget_cny == 1.0
    assert plan_contract.token_budget == 1000
    assert plan_contract.cost_budget_cny == 1.0
    assert plan_contract.metadata["budget_scope"] == "entire_r5_run"
    assert plan_contract.metadata["budget_remaining_before_phase_input_tokens"] == 300
    assert plan_contract.metadata["budget_remaining_before_phase_cost_cny"] == 0.3
    assert plan_contract.next_call_cost_reserve_cny == 0.045

    accounting = json.loads(
        (context.work_dir / "R5_PHASE_ACCOUNTING.json").read_text(
            encoding="utf-8"
        )
    )
    assert accounting["budget"]["ceiling"] == {
        "input_tokens": 1000,
        "estimated_cost_cny": 1.0,
    }
    assert accounting["budget"]["requested_increment"] == {
        "input_tokens": 1000,
        "estimated_cost_cny": 1.0,
    }
    assert accounting["total"]["input_tokens"] == 1000
    assert accounting["total"]["estimated_cost_cny"] == 1.0
    assert accounting["phases"]["initial_discovery"]["totals"]["input_tokens"] == 700
    assert accounting["phases"]["plan_only"]["totals"]["input_tokens"] == 300


def test_r5_explicit_resume_starts_new_envelope_from_lifetime(
    tmp_path: Path,
    monkeypatch,
):
    """A later CLI resume adds its allowance once instead of reusing old R5_BUDGET."""

    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    provider = _research_program_fixture(tmp_path)
    context = provider.ctx
    context.work_dir.mkdir(parents=True, exist_ok=True)
    (context.work_dir / "COST.json").write_text(
        json.dumps(
            {
                "run_id": "previous_run",
                "task_id": "research_program",
                "total_input_tokens": 700,
                "total_output_tokens": 120,
                "model_call_count": 2,
                "tool_call_count": 3,
                "estimated_cost_cny": 0.7,
            }
        ),
        encoding="utf-8",
    )
    # This is the old invocation's envelope.  A top-level explicit resume must
    # not inherit its ceiling or requested increment.
    (context.work_dir / "R5_BUDGET.json").write_text(
        json.dumps(
            {
                "schema_version": "optomind.r5_budget.v1",
                "baseline": {"input_tokens": 700, "estimated_cost_cny": 0.7},
                "requested_increment": {
                    "input_tokens": 100,
                    "estimated_cost_cny": 0.1,
                },
                "ceiling": {"input_tokens": 800, "estimated_cost_cny": 0.8},
                "current": {"input_tokens": 700, "estimated_cost_cny": 0.7},
            }
        ),
        encoding="utf-8",
    )
    (context.work_dir / "R5_PHASE_ACCOUNTING.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.r5_phase_accounting.v2",
                "lifetime_total": {
                    "model_calls": 2,
                    "tool_calls": 3,
                    "input_tokens": 700,
                    "output_tokens": 120,
                    "estimated_cost_cny": 0.7,
                    "wall_time_seconds": 1.0,
                },
                "total": {
                    "model_calls": 2,
                    "tool_calls": 3,
                    "input_tokens": 700,
                    "output_tokens": 120,
                    "estimated_cost_cny": 0.7,
                    "wall_time_seconds": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            captured["contract"] = contract
            # A fresh run writes a fresh cumulative ledger.  No new usage is
            # spent in this deterministic test.
            (context.work_dir / "COST.json").write_text(
                json.dumps(
                    {
                        "run_id": contract.run_id,
                        "task_id": "research_program",
                        "total_input_tokens": 0,
                        "total_output_tokens": 0,
                        "model_call_count": 0,
                        "tool_call_count": 0,
                        "estimated_cost_cny": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.budget_exhausted,
                stop_reason="explicit_resume_probe",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    monkeypatch.setattr(
        module.ResearchProgramToolProvider,
        "reconcile_existing_r5_artifacts",
        lambda self: {"status": "ready_for_plan_resume"},
    )

    result = run_research_program(
        context,
        run_id="new_explicit_resume",
        model_tier="advanced_model",
        resume_plan_only=True,
        token_budget=300,
        cost_budget_cny=0.3,
        max_iters=1,
    )

    assert result.status == TaskStatus.waiting_for_human
    contract = captured["contract"]
    assert contract.token_budget == 300
    assert contract.cost_budget_cny == 0.3
    assert contract.metadata["budget_baseline_input_tokens"] == 700
    assert contract.metadata["budget_baseline_cost_cny"] == 0.7
    assert contract.metadata["budget_ceiling_input_tokens"] == 1000
    assert contract.metadata["budget_ceiling_cost_cny"] == 1.0
    assert contract.metadata["budget_remaining_before_phase_input_tokens"] == 300
    assert contract.metadata["budget_remaining_before_phase_cost_cny"] == 0.3
    assert contract.metadata["r5_lifetime_budget"]["ledger_mode"] == "fresh_run_id"

    budget = json.loads(
        (context.work_dir / "R5_BUDGET.json").read_text(encoding="utf-8")
    )
    assert budget["baseline"] == {
        "input_tokens": 700,
        "estimated_cost_cny": 0.7,
    }
    assert budget["ceiling"] == {
        "input_tokens": 1000,
        "estimated_cost_cny": 1.0,
    }
    assert budget["requested_increment"] == {
        "input_tokens": 300,
        "estimated_cost_cny": 0.3,
    }


def test_research_program_normalizes_common_readiness_aliases():
    def normalized_readiness(value: str) -> str:
        normalized = _normalize_hypothesis_payload(
            {
                "hypotheses": [
                    {
                        "id": "H1",
                        "readiness": value,
                        "supporting_paper_ids": [],
                        "supporting_chunk_ids": [],
                        "inference_chain": ["A"],
                        "assumptions": ["B"],
                        "alternative_explanations": ["C"],
                        "falsification_conditions": ["D"],
                    }
                ]
            }
        )
        return normalized["hypotheses"][0]["readiness"]

    assert normalized_readiness("concept_validated") == (
        "needs_more_literature"
    )
    assert normalized_readiness("experimental_validation_ready") == "ready"
    assert normalized_readiness("theory_ready") == "needs_more_literature"


def test_real_shape_plan_only_resume_reuses_focus_artifacts(
    tmp_path: Path,
    monkeypatch,
):
    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.opportunity_map.v1",
                "opportunities": [
                    {
                        "opportunity_id": "OP01",
                        "title": "Separate coupled limits",
                        "problem": "The contributions remain confounded.",
                        "why_it_matters": "A controlled comparison would clarify the limit.",
                        "origin_type": "method_gap",
                        "source_section_ids": ["S01"],
                        "supporting_paper_ids": ["paper_A"],
                        "supporting_chunk_ids": ["paper_A:chunk:0"],
                        "evidence_status": "partially_supported",
                        "evidence_basis": "A canonical chunk describes the coupled limit.",
                        "author_inference": "A perturbation may separate the contributions.",
                        "uncertainty": "The separation may depend on calibration.",
                    },
                    {
                        "opportunity_id": "OP02",
                        "title": "Explore an unmeasured regime",
                        "problem": "One regime remains unmeasured.",
                        "why_it_matters": "It may expose a different mechanism.",
                        "origin_type": "evidence_gap",
                        "source_section_ids": ["S01"],
                        "supporting_paper_ids": [],
                        "supporting_chunk_ids": [],
                        "evidence_status": "open_gap",
                        "evidence_basis": "No direct evidence is currently attached.",
                        "author_inference": "A targeted search may resolve the gap.",
                        "uncertainty": "The effect may be below sensitivity.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.hypothesis_portfolio.v1",
                "hypotheses": [
                    {
                        "hypothesis_id": "H01",
                        "title": "Separate the coupled limits",
                        "statement": "A controlled perturbation separates the two coupled response pathways.",
                        "source_opportunity_ids": ["OP01"],
                        "mechanism_rationale": "The perturbation changes one pathway at a time.",
                        "supporting_paper_ids": ["paper_A"],
                        "supporting_chunk_ids": ["paper_A:chunk:0"],
                        "inference_chain": ["The chunk states the coupling; the test separates it."],
                        "assumptions": ["The perturbations are independently controllable."],
                        "alternative_explanations": ["The apparent coupling is an instrument artifact."],
                        "falsification_conditions": ["The pathways remain inseparable."],
                        "novelty_status": "candidate_novelty",
                        "confidence": "medium",
                        "readiness": "ready",
                    },
                    {
                        "hypothesis_id": "H02",
                        "title": "Characterize the unmeasured regime",
                        "statement": "A targeted evidence-maturation study will identify whether the unmeasured regime has a distinct response.",
                        "source_opportunity_ids": ["OP02"],
                        "mechanism_rationale": "The open gap motivates a bounded search before validation.",
                        "supporting_paper_ids": [],
                        "supporting_chunk_ids": [],
                        "inference_chain": ["The gap is explicit; the proposed search tests whether it is real."],
                        "assumptions": ["Relevant sources can be identified."],
                        "alternative_explanations": ["The regime is not experimentally distinguishable."],
                        "falsification_conditions": ["No relevant evidence exists after the bounded search."],
                        "novelty_status": "unknown_requires_prior_art_search",
                        "confidence": "low",
                        "readiness": "needs_more_literature",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "PROGRAM_FOCUS_GATE.json").write_text(
        json.dumps(
            {
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
                    "platform_type": "experiment",
                    "name": "Controlled measurement platform",
                    "description": "One calibrated acquisition route.",
                    "compatibility_key": "controlled_measurement",
                },
                "boundaries": {
                    "personnel": ["One team"],
                    "equipment": ["One instrument"],
                    "data": ["Reference measurements"],
                    "timeline": ["Twelve months"],
                    "budget": ["Fixed budget"],
                },
                "unified_evaluation": {
                    "metrics": [{"metric_id": "M01", "name": "response separation"}],
                    "baselines": [{"baseline_id": "B01", "name": "reference condition"}],
                    "comparison_protocol": "Use one reference and uncertainty procedure.",
                },
                "selected_opportunity_ids": ["OP01"],
                "main_hypothesis_ids": ["H01"],
                "future_hypothesis_ids": ["H02"],
                "hypothesis_dependencies": [],
                "future_branches": [
                    {
                        "opportunity_id": "OP02",
                        "reason": "The unmeasured regime is deferred.",
                        "excluded_from_current_work_packages": True,
                    }
                ],
                "traceability_matrix": [],
            }
        ),
        encoding="utf-8",
    )

    from optomind_research.runtime import research_program_runner as runner
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            captured["allowed_tools"] = contract.allowed_tools
            captured["model_tier"] = contract.model_tier
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.budget_exhausted,
                stop_reason="offline_plan_only_probe",
            )

    monkeypatch.setattr(runner, "ResearchWorker", FakeWorker)
    result = run_research_program(
        ResearchProgramContext(
            blueprint_path=provider.ctx.blueprint_path,
            final_review_path=provider.ctx.final_review_path,
            coverage_root=provider.ctx.coverage_root,
            work_dir=work_dir,
            base_kb_sqlite=provider.ctx.base_kb_sqlite,
        ),
        run_id="r5_plan_only_resume",
        model_tier="advanced_model",
        model_override=object(),
        cost_budget_cny=0.1,
        token_budget=1000,
        max_iters=1,
        resume_plan_only=True,
    )
    assert result.status == TaskStatus.waiting_for_human
    assert captured["model_tier"] == "advanced_model"
    assert "submit_research_opportunity_map" not in captured["allowed_tools"]
    assert "submit_hypothesis_portfolio" not in captured["allowed_tools"]
    assert "submit_program_focus_gate" not in captured["allowed_tools"]
    assert "submit_research_plan" in captured["allowed_tools"]
    reconciliation = json.loads(
        (work_dir / "R5_RECONCILIATION.json").read_text(encoding="utf-8")
    )
    assert reconciliation["status"] == "ready_for_plan_resume"
    assert reconciliation["recomputed_opportunities"] is False
    assert reconciliation["recomputed_hypotheses"] is False
    assert reconciliation["recomputed_focus"] is False


def test_plan_only_provider_exposes_only_bounded_tools_and_context(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    work_dir = provider.ctx.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps(
            {
                "opportunities": [
                    {
                        "opportunity_id": "OP01",
                        "title": "Separate coupled limits",
                        "problem": "The effects remain coupled.",
                        "why_it_matters": "A focused test can separate them.",
                        "supporting_chunk_ids": ["paper_A:chunk:0"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps(
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "H01",
                        "title": "Separate the effects",
                        "statement": "A controlled perturbation separates the effects.",
                        "supporting_chunk_ids": ["paper_A:chunk:0"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "PROGRAM_FOCUS_GATE.json").write_text(
        json.dumps(
            {
                "gate_id": "PFG01",
                "selected_opportunity_ids": ["OP01"],
                "main_hypothesis_ids": ["H01"],
                "future_hypothesis_ids": ["H02"],
                "future_branches": [
                    {
                        "opportunity_id": "OP02",
                        "excluded_from_current_work_packages": True,
                    }
                ],
                "project_type": "experiment",
                "shared_platform": {
                    "platform_id": "PLAT01",
                    "platform_type": "experiment",
                    "name": "Controlled platform",
                    "description": "One platform.",
                    "compatibility_key": "controlled_optical_measurement",
                },
                "main_problem": {"problem_id": "P01", "statement": "A problem."},
                "boundaries": {},
                "unified_evaluation": {},
            }
        ),
        encoding="utf-8",
    )
    provider.ctx.plan_only_resume = True
    tools = {tool.name: tool for tool in provider.get_tools(work_dir)}
    assert set(tools) == {
        "load_research_program_context",
        "submit_research_plan",
        "validate_research_program_package",
    }
    context = json.loads(_tool_text(tools["load_research_program_context"]))
    assert context["mode"] == "plan_only_resume"
    assert "sections" not in context
    assert "shared_review_context" not in context
    assert "evidence_identifier_catalog" not in context
    assert context["focus_gate"]["main_hypothesis_ids"] == ["H01"]
    assert context["selected_evidence_summary"][0]["chunk_id"] == "paper_A:chunk:0"
    assert context["plan_schema_and_scaffold"]["schema_version"] == (
        "research_harness.research_plan.v2"
    )
    second = json.loads(_tool_text(tools["load_research_program_context"]))
    assert second["status"] == "already_loaded"


def test_plan_only_does_not_accept_opportunity_container_as_plan(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    provider.ctx.plan_only_resume = True
    tools = {
        tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)
    }
    response = json.loads(
        _tool_text(
            tools["submit_research_plan"],
            research_plan_json=json.dumps(
                {"opportunities": [{"opportunity_id": "OP01"}]}
            ),
        )
    )
    assert response["status"] == "error"
    assert not (provider.ctx.work_dir / "RESEARCH_PLAN.json").exists()


def test_plan_only_sanitizes_future_unknown_links_and_platform_key():
    plan = {
        "work_packages": [
            {
                "work_package_id": "WP01",
                "hypothesis_ids": ["H01", "H99"],
                "opportunity_ids": ["OP01", "OP03"],
                "platform_id": "WRONG",
                "platform_compatibility_key": "wrong_key",
            },
            {
                "work_package_id": "WP02",
                "hypothesis_ids": ["H99"],
                "opportunity_ids": ["OP03"],
            },
            {
                "work_package_id": "WP03",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": [],
            },
        ]
    }
    gate = {
        "main_hypothesis_ids": ["H01"],
        "future_hypothesis_ids": ["H99"],
        "selected_opportunity_ids": ["OP01"],
        "future_branches": [
            {"opportunity_id": "OP03", "excluded_from_current_work_packages": True}
        ],
        "shared_platform": {
            "platform_id": "PLAT01",
            "compatibility_key": "accepted_key",
        },
    }
    sanitized, corrections = _sanitize_plan_packages_to_focus(plan, gate)
    assert [item["work_package_id"] for item in sanitized["work_packages"]] == [
        "WP01",
        "WP03",
    ]
    assert sanitized["work_packages"][0]["hypothesis_ids"] == ["H01"]
    assert sanitized["work_packages"][0]["opportunity_ids"] == ["OP01"]
    assert sanitized["work_packages"][0]["platform_id"] == "PLAT01"
    assert sanitized["work_packages"][0]["platform_compatibility_key"] == (
        "accepted_key"
    )
    assert any(
        item["action"] == "drop_future_or_unlinked_work_package"
        and item["work_package_id"] == "WP02"
        for item in corrections
    )


def test_plan_only_second_validation_returns_human_review_signal(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    provider.ctx.plan_only_resume = True
    tools = {
        tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)
    }
    first = _tool_text(tools["validate_research_program_package"])
    second = _tool_text(tools["validate_research_program_package"])
    assert first.startswith("VALIDATION_FAILED:")
    assert second.startswith("VALIDATION_AWAITING_HUMAN_REVIEW:")
    assert provider.try_auto_finalize().startswith(
        "VALIDATION_AWAITING_HUMAN_REVIEW:"
    )


def test_plan_only_rebuilds_missing_root_traceability_matrix_from_existing_data():
    """Reproduce the live failure: valid WPs arrive with an empty root matrix."""

    focus_gate = {
        "main_problem": {
            "problem_id": "P01",
            "statement": "A bounded problem requires a focused validation plan.",
        },
        "selected_opportunity_ids": ["OP01"],
        "main_hypothesis_ids": ["H01"],
        # The real plan-only run received this empty field from the accepted
        # focus artifact even though the model had already written WPs.
        "traceability_matrix": [],
    }
    plan = {
        "work_packages": [
            {
                "work_package_id": "WP01",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "methods": ["Run the controlled comparison"],
                "metric_ids": ["M01"],
                "baseline_ids": ["B01"],
                "stop_or_pivot_criteria": ["Stop if the control fails."],
            }
        ]
    }
    matrix, audit = _build_plan_only_traceability_matrix(
        plan,
        focus_gate,
        {
            "H01": {
                "hypothesis_id": "H01",
                "falsification_conditions": ["The predicted separation is absent."],
            }
        },
    )
    assert len(matrix) == 1
    row = matrix[0]
    assert row["problem_id"] == "P01"
    assert row["opportunity_id"] == "OP01"
    assert row["hypothesis_id"] == "H01"
    assert row["work_package_id"] == "WP01"
    assert row["proposed_tests"] == ["Run the controlled comparison"]
    assert row["metrics"] == ["M01"]
    assert row["baselines"] == ["B01"]
    assert row["falsification_conditions"] == [
        "The predicted separation is absent."
    ]
    assert row["stop_or_pivot_decisions"] == ["Stop if the control fails."]
    assert audit["action"] == "rebuild_traceability_matrix"
    assert audit["previous_row_count"] == 0
    assert audit["generated_row_count"] == 1
    assert audit["empty_fields_are_preserved_for_validation"] is True


def test_plan_only_discards_stale_focus_matrix_and_rebuilds_selected_spine_only():
    """A narrowed focus must not leak old hypotheses into the plan matrix."""

    focus_gate = {
        "main_problem": {"problem_id": "P01"},
        "selected_opportunity_ids": ["OP02"],
        "main_hypothesis_ids": ["H01"],
        # These rows were produced before evidence-calibrated convergence.
        "traceability_matrix": [
            {
                "problem_id": "P01",
                "opportunity_id": "OP05",
                "hypothesis_id": "H02",
                "work_package_id": "WP99",
                "proposed_tests": ["stale"],
                "metrics": ["M01"],
                "baselines": ["B01"],
                "falsification_conditions": ["stale"],
                "stop_or_pivot_decisions": ["stale"],
            }
        ],
    }
    plan = {
        # The model matrix is also invalid, and must be ignored.
        "traceability_matrix": [
            {"hypothesis_id": "H99", "work_package_id": "WP99"}
        ],
        "work_packages": [
            {
                "work_package_id": "WP01",
                # Literature maturation is linked to the selected opportunity
                # only.  With one accepted main hypothesis, the linkage is
                # deterministic and safe to record.
                "hypothesis_ids": [],
                "opportunity_ids": ["OP02"],
                "methods": ["Audit the quantitative fabrication evidence."],
                "metric_ids": ["M01"],
                "baseline_ids": ["B01"],
                "stop_or_pivot_criteria": ["If evidence remains sparse, pivot."],
            }
        ],
    }
    matrix, audit = _build_plan_only_traceability_matrix(
        plan,
        focus_gate,
        {
            "H01": {
                "hypothesis_id": "H01",
                "falsification_conditions": ["The predicted improvement is absent."],
            }
        },
    )
    assert len(matrix) == 1
    row = matrix[0]
    assert row["opportunity_id"] == "OP02"
    assert row["hypothesis_id"] == "H01"
    assert row["work_package_id"] == "WP01"
    assert {
        row["hypothesis_id"],
        row["opportunity_id"],
        row["work_package_id"],
    }.isdisjoint({"H02", "H99", "OP05", "WP99"})
    assert row["falsification_conditions"] == ["The predicted improvement is absent."]
    assert audit["stale_rows_discarded"] == 1
    assert audit["stale_rows_are_not_reused"] is True
    assert any(
        item.get("matrix_field") == "hypothesis_id"
        and item.get("source_field") == "sole_selected_focus_hypothesis"
        for item in audit["fallback_field_sources"]
    )


def test_accepted_focus_is_a_terminal_discovery_signal(tmp_path: Path):
    """Discovery must stop after focus acceptance without a generic validator."""

    provider = _research_program_fixture(tmp_path)
    (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").write_text(
        json.dumps({"status": "passed", "main_hypothesis_ids": ["H01"]}),
        encoding="utf-8",
    )
    assert "validate_task_result" not in {
        tool.name for tool in provider.get_tools(provider.ctx.work_dir)
    }
    signal = provider.try_auto_finalize()
    assert signal is not None
    assert signal.startswith("VALIDATION_PASSED:")
    assert "plan-only" in signal


def test_plan_only_submit_after_human_stop_is_terminal(tmp_path: Path):
    provider = _research_program_fixture(tmp_path)
    provider.ctx.plan_only_resume = True
    provider._plan_only_submission_count = 2
    tools = {
        tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)
    }
    result = _tool_text(
        tools["submit_research_plan"],
        research_plan_json=json.dumps({"work_packages": []}),
    )
    assert result.startswith("VALIDATION_AWAITING_HUMAN_REVIEW:")
    assert provider.try_auto_finalize().startswith(
        "VALIDATION_AWAITING_HUMAN_REVIEW:"
    )


def test_research_program_repairs_json_and_downgrades_unverified_number():
    parsed = _parse_json_object(
        '{"hypotheses":[{"id":"H1","title":"Scaling",'
        '"statement":"Response scales as D^-2",'
        '"source_opportunities":["OP1"],'
        '"mechanism_rationale":"A proposed scaling argument.",'
        '"inference_chain":"Boundary exists; scaling is inferred",'
        '"assumptions":"Fixed geometry",'
        '"alternative_explanations":"Linear scaling",'
        '"falsification_conditions":"A different exponent is measured",'
        '"novelty_status":"unknown_requires_prior_art_search",'
        '"confidence":"medium","readiness":"ready"}]}'
    )
    normalized = _normalize_hypothesis_payload(parsed)
    assert normalized["hypotheses"][0]["confidence"] == "low"
    assert (
        normalized["hypotheses"][0]["readiness"]
        == "needs_more_literature"
    )
    assert (
        normalized["hypotheses"][0]["quantitative_commitment_status"]
        == "proposed_program_target"
    )
    assert any(
        "proposed program-design target" in item
        for item in normalized["hypotheses"][0]["assumptions"]
    )
    repaired = _parse_json_object(
        '{"title":"A malformed \\\\q escape","items":[]}'
    )
    assert repaired["title"]


def test_hypothesis_confidence_aliases_are_case_insensitive_and_conservative():
    values = {
        "Moderate": "medium",
        "moderate confidence": "medium",
        "MED": "medium",
        "very-low": "low",
        "Uncertain": "low",
        "STRONG": "high",
    }
    for raw, expected in values.items():
        normalized = _normalize_hypothesis_payload(
            {
                "hypotheses": [
                    {
                        "id": "H01",
                        "confidence": raw,
                        "readiness": "needs_more_literature",
                    }
                ]
            }
        )
        assert normalized["hypotheses"][0]["confidence"] == expected


def test_focus_normalization_repairs_platform_and_future_branch_bookkeeping():
    gate = {
        "project_type": "hybrid",
        "shared_platform": {
            "platform_id": "PLAT01",
            "platform_type": "simulation",
            "name": "One solver",
        },
        "selected_opportunity_ids": ["OP01"],
        "main_hypothesis_ids": ["H01", "H02"],
        "hypothesis_dependencies": [
            {
                "upstream_hypothesis_id": "H01",
                "downstream_hypothesis_id": "H02",
                "reason": "The second test uses the first calibration.",
            },
            {
                "upstream_hypothesis_id": "H01",
                "downstream_hypothesis_id": "H02",
                "reason": "Duplicate model edge.",
            },
            {
                "upstream_hypothesis_id": "H02",
                "downstream_hypothesis_id": "H02",
                "reason": "Invalid self edge.",
            },
        ],
        "future_branches": [],
    }
    opportunities = [
        {
            "opportunity_id": "OP01",
            "title": "Selected opportunity",
            "evidence_status": "supported_boundary",
        },
        {
            "opportunity_id": "OP02",
            "title": "Deferred operating regime",
            "evidence_status": "open_gap",
        },
    ]
    normalized, corrections = _normalize_focus_gate_against_opportunities(
        gate, opportunities
    )
    assert normalized["project_type"] == "simulation"
    assert len(normalized["hypothesis_dependencies"]) == 1
    assert [row["opportunity_id"] for row in normalized["future_branches"]] == [
        "OP02"
    ]
    assert "Deferred operating regime" in normalized["future_branches"][0]["reason"]
    assert "status=open_gap" in normalized["future_branches"][0]["reason"]
    assert normalized["future_branches"][0]["excluded_from_current_work_packages"] is True
    assert corrections
    assert any(
        "self-dependency" in item["reason"]
        for item in corrections
        if item.get("field") == "hypothesis_dependencies"
    )


def test_focus_normalization_isolates_all_nonselected_accepted_hypotheses():
    """Omitted future IDs are filled only from the accepted portfolio."""

    from optomind_research.runtime.program_focus_gate import ProgramFocusGate

    gate = {
        "main_problem": {
            "problem_id": "P01",
            "statement": "A bounded optical problem remains unresolved.",
        },
        "project_type": "experiment",
        "shared_platform": {
            "platform_id": "PLAT01",
            "name": "Controlled optical platform",
            "description": "One bounded validation platform.",
            "compatibility_key": "controlled_optical_measurement",
        },
        "boundaries": {
            "personnel": ["One team"],
            "equipment": ["One instrument"],
            "data": ["Reference data"],
            "timeline": ["One year"],
            "budget": ["Fixed budget"],
        },
        "unified_evaluation": {
            "metrics": [{"metric_id": "M01"}],
            "baselines": [{"baseline_id": "B01"}],
            "comparison_protocol": "Use one fixed reference.",
        },
        "selected_opportunity_ids": ["OP01"],
        "main_hypothesis_ids": ["H01"],
        "future_hypothesis_ids": [],
        "hypothesis_dependencies": [],
        "future_branches": [],
    }
    opportunities = [
        {"opportunity_id": "OP01", "title": "Selected", "evidence_status": "supported_boundary"},
        {"opportunity_id": "OP02", "title": "Deferred 2", "evidence_status": "open_gap"},
        {"opportunity_id": "OP03", "title": "Deferred 3", "evidence_status": "partially_supported"},
    ]
    hypotheses = [
        {"hypothesis_id": "H01", "source_opportunity_ids": ["OP01"]},
        {"hypothesis_id": "H02", "source_opportunity_ids": ["OP02"]},
        {"hypothesis_id": "H03", "source_opportunity_ids": ["OP03"]},
        {"hypothesis_id": "H04", "source_opportunity_ids": ["OP01"]},
    ]

    normalized, corrections = _normalize_focus_gate_against_opportunities(
        gate,
        opportunities,
        hypotheses,
    )

    assert normalized["future_hypothesis_ids"] == ["H02", "H03", "H04"]
    assert "H01" not in normalized["future_hypothesis_ids"]
    assert all(
        value in {"H01", "H02", "H03", "H04"}
        for value in normalized["future_hypothesis_ids"]
    )
    assert sum(
        item.get("field") == "future_hypothesis_ids"
        and item.get("from") == "missing"
        for item in corrections
    ) == 3

    decision = ProgramFocusGate().validate_focus_decision(
        normalized,
        opportunities,
        hypotheses,
        shared_context={
            "review_scope_map": {"scope": "bounded"},
            "literature_relation_graph": {"relations": []},
            "technical_audit": {"status": "available"},
            "source_permissions": {"status": "available"},
            "r4_candidate_limitations": [],
        },
    )
    assert decision.passed


def test_single_main_hypothesis_does_not_require_dependency_chain():
    from optomind_research.runtime.program_focus_gate import ProgramFocusGate

    decision = ProgramFocusGate().validate_focus_decision(
        {
            "main_problem": {
                "problem_id": "P01",
                "statement": "A bounded optical problem remains unresolved.",
            },
            "project_type": "experiment",
            "shared_platform": {
                "platform_id": "PLAT01",
                "name": "Controlled platform",
                "description": "One controlled measurement route.",
                "compatibility_key": "controlled_measurement",
            },
            "boundaries": {
                "personnel": ["One team"],
                "equipment": ["One instrument"],
                "data": ["Reference data"],
                "timeline": ["One year"],
                "budget": ["Fixed budget"],
            },
            "unified_evaluation": {
                "metrics": [{"metric_id": "M01"}],
                "baselines": [{"baseline_id": "B01"}],
                "comparison_protocol": "Use one fixed reference.",
            },
            "selected_opportunity_ids": ["OP01"],
            "main_hypothesis_ids": ["H01"],
            "future_hypothesis_ids": [],
            "hypothesis_dependencies": [],
            "future_branches": [],
        },
        [{"opportunity_id": "OP01"}],
        [{"hypothesis_id": "H01"}],
        shared_context={
            "review_scope_map": {"scope": "bounded"},
            "literature_relation_graph": {"relations": []},
            "technical_audit": {"status": "available"},
            "source_permissions": {"status": "available"},
            "r4_candidate_limitations": [],
        },
    )
    assert decision.passed
    assert "main_hypotheses_must_have_dependency_chain" not in decision.errors


def test_unique_evidence_calibrated_spine_selects_h01_without_model_call():
    gate = {
        "main_hypothesis_ids": ["H01", "H02", "H03"],
        "selected_opportunity_ids": ["OP02", "OP05", "OP06"],
        "future_hypothesis_ids": [],
        "future_branches": [],
        "hypothesis_dependencies": [],
    }
    opportunities = [
        {"opportunity_id": "OP02", "title": "Tolerance", "evidence_status": "supported_boundary"},
        {"opportunity_id": "OP05", "title": "Platform", "evidence_status": "partially_supported"},
        {"opportunity_id": "OP06", "title": "Hybrid route", "evidence_status": "open_gap"},
    ]
    hypotheses = [
        {
            "hypothesis_id": "H01",
            "source_opportunity_ids": ["OP02"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
            "supporting_paper_ids": ["paper_A"],
        },
        {
            "hypothesis_id": "H02",
            "source_opportunity_ids": ["OP05"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
            "supporting_paper_ids": ["paper_B"],
        },
        {
            "hypothesis_id": "H03",
            "source_opportunity_ids": ["OP06"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
            "supporting_paper_ids": ["paper_C"],
        },
    ]
    repaired, audit = _try_evidence_calibrated_single_spine(
        gate,
        [
            "main_hypotheses_must_have_dependency_chain",
            "main_hypotheses_must_have_one_spine_root",
            "main_hypotheses_dependency_graph_disconnected",
        ],
        opportunities,
        hypotheses,
        {
            "paper_permissions": {
                "paper_A": "factual_support",
                "paper_B": "factual_support",
                "paper_C": "contextual_or_qualified_support",
            },
            "chunk_permissions": {},
        },
    )
    assert repaired is not None
    assert audit is not None
    assert repaired["main_hypothesis_ids"] == ["H01"]
    assert repaired["selected_opportunity_ids"] == ["OP02"]
    assert repaired["hypothesis_dependencies"] == []
    assert set(repaired["future_hypothesis_ids"]) == {"H02", "H03"}
    assert {row["opportunity_id"] for row in repaired["future_branches"]} == {
        "OP05",
        "OP06",
    }
    assert "not a claim that the selected hypothesis is scientific truth" in audit[
        "scientific_interpretation"
    ]


def test_single_spine_tie_does_not_choose_by_identifier():
    gate = {"main_hypothesis_ids": ["H01", "H02"]}
    opportunities = [
        {"opportunity_id": "OP01", "title": "A", "evidence_status": "supported_boundary"},
        {"opportunity_id": "OP02", "title": "B", "evidence_status": "supported_boundary"},
    ]
    hypotheses = [
        {
            "hypothesis_id": "H01",
            "source_opportunity_ids": ["OP01"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
        },
        {
            "hypothesis_id": "H02",
            "source_opportunity_ids": ["OP02"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
        },
    ]
    repaired, audit = _try_evidence_calibrated_single_spine(
        gate,
        ["main_hypotheses_must_have_dependency_chain"],
        opportunities,
        hypotheses,
        {"paper_permissions": {}, "chunk_permissions": {}},
    )
    assert repaired is None
    assert audit is None


def test_single_spine_repairs_mixed_identity_permission_and_graph_errors():
    """Reproduce the real E2E focus failure without weakening validation."""

    from optomind_research.runtime.program_focus_gate import ProgramFocusGate

    gate = {
        "gate_id": "PFG01",
        "main_problem": {
            "problem_id": "P01",
            "statement": "A bounded microscopy control problem remains unresolved.",
        },
        "project_type": "experiment",
        "shared_platform": {
            "platform_id": "PLAT01",
            "name": "Adaptive microscopy platform",
            "description": "One bounded in vivo validation platform.",
            "compatibility_key": "adaptive_microscopy_control",
        },
        "boundaries": {
            "personnel": ["One team"],
            "equipment": ["One microscope"],
            "data": ["A bounded image set"],
            "timeline": ["One year"],
            "budget": ["Fixed budget"],
        },
        "unified_evaluation": {
            "metrics": [{"metric_id": "M01"}],
            "baselines": [{"baseline_id": "B01"}],
            "comparison_protocol": "Use one fixed reference protocol.",
        },
        "selected_opportunity_ids": ["OP01", "OP03"],
        "main_hypothesis_ids": ["H01", "H02"],
        "future_hypothesis_ids": [],
        "hypothesis_dependencies": [],
        "future_branches": [],
    }
    opportunities = [
        {
            "opportunity_id": "OP01",
            "title": "Physics-constrained adaptation",
            "evidence_status": "partially_supported",
            "supporting_paper_ids": ["paper_factual"],
            "author_inference": "The route is a bounded synthesis.",
            "uncertainty": "Cross-domain stability remains unknown.",
        },
        {
            "opportunity_id": "OP03",
            "title": "Context-only control route",
            "evidence_status": "supported_boundary",
            "supporting_paper_ids": ["paper_contextual"],
            "author_inference": "A contextual extension is conceivable.",
            "uncertainty": "Direct evidence is unavailable.",
        },
    ]
    hypotheses = [
        {
            "hypothesis_id": "H01",
            "source_opportunity_ids": ["OP01"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
            "supporting_paper_ids": ["paper_factual"],
        },
        {
            "hypothesis_id": "H02",
            "source_opportunity_ids": ["OP03"],
            "readiness": "needs_more_literature",
            "confidence": "medium",
            "supporting_paper_ids": ["paper_contextual"],
        },
    ]
    permissions = {
        "paper_permissions": {
            "paper_factual": "factual_support",
            "paper_contextual": "contextual_or_qualified_support",
        },
        "chunk_permissions": {},
    }
    shared_context = {
        "review_scope_map": {"scope": "bounded"},
        "literature_relation_graph": {"relations": []},
        "technical_audit": {"status": "available"},
        "source_permissions": {"status": "available"},
        "r4_candidate_limitations": [],
    }
    first = ProgramFocusGate().validate_focus_decision(
        gate,
        opportunities,
        hypotheses,
        shared_context=shared_context,
        permission_map=permissions,
    )
    assert "main_hypotheses_dependency_graph_disconnected" in first.errors
    assert "opportunity_uses_nonfactual_permission:OP03" in first.errors

    repaired, audit = _try_evidence_calibrated_single_spine(
        gate,
        first.errors,
        opportunities,
        hypotheses,
        permissions,
    )
    assert repaired is not None
    assert audit is not None
    assert repaired["main_hypothesis_ids"] == ["H01"]
    assert repaired["selected_opportunity_ids"] == ["OP01"]
    second = ProgramFocusGate().validate_focus_decision(
        repaired,
        opportunities,
        hypotheses,
        shared_context=shared_context,
        permission_map=permissions,
    )
    assert second.passed, second.errors


def test_focus_normalization_removes_unaccepted_selected_hypothesis():
    normalized, corrections = _normalize_focus_gate_against_opportunities(
        {
            "selected_opportunity_ids": ["OP01"],
            "main_hypothesis_ids": ["H01", "H05"],
            "future_hypothesis_ids": ["H02"],
            "future_branches": [],
        },
        [{"opportunity_id": "OP01", "title": "Selected"}],
        [
            {"hypothesis_id": "H01", "source_opportunity_ids": ["OP01"]},
            {"hypothesis_id": "H02", "source_opportunity_ids": ["OP01"]},
        ],
    )
    assert normalized["main_hypothesis_ids"] == ["H01"]
    assert normalized["future_hypothesis_ids"] == ["H02"]
    assert any(
        row.get("field") == "main_hypothesis_ids"
        and row.get("from") == "H05"
        for row in corrections
    )


def test_single_spine_does_not_mask_unrelated_focus_errors():
    repaired, audit = _try_evidence_calibrated_single_spine(
        {"main_hypothesis_ids": ["H01", "H02"]},
        [
            "main_hypotheses_must_have_dependency_chain",
            "shared_platform_type_incompatible_with_project_type",
        ],
        [
            {"opportunity_id": "OP01", "evidence_status": "supported_boundary"},
            {"opportunity_id": "OP02", "evidence_status": "open_gap"},
        ],
        [
            {"hypothesis_id": "H01", "source_opportunity_ids": ["OP01"]},
            {"hypothesis_id": "H02", "source_opportunity_ids": ["OP02"]},
        ],
        {"paper_permissions": {}, "chunk_permissions": {}},
    )
    assert repaired is None
    assert audit is None


def test_recover_last_rejected_focus_candidate_is_read_only(tmp_path: Path):
    state = {
        "context": [
            {
                "type": "tool_call",
                "name": "submit_program_focus_gate",
                "input": json.dumps(
                    {
                        "program_focus_gate_json": json.dumps(
                            {"main_hypothesis_ids": ["H01"]}
                        )
                    }
                ),
            }
        ]
    }
    (tmp_path / "AGENT_STATE.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    assert _recover_last_focus_gate_from_agent_state(tmp_path) == {
        "main_hypothesis_ids": ["H01"]
    }


def test_repeated_focus_error_signature_stops_without_unbounded_retries(
    tmp_path: Path,
):
    provider = _research_program_fixture(tmp_path)
    tools = {tool.name: tool for tool in provider.get_tools(provider.ctx.work_dir)}
    first = json.loads(
        _tool_text(
            tools["submit_program_focus_gate"],
            program_focus_gate_json=json.dumps({}),
        )
    )
    second = json.loads(
        _tool_text(
            tools["submit_program_focus_gate"],
            program_focus_gate_json=json.dumps({}),
        )
    )
    assert first["status"] == "error"
    assert second["status"] == "awaiting_human_review"
    assert provider.try_auto_finalize().startswith(
        "VALIDATION_AWAITING_HUMAN_REVIEW:"
    )
    audit = json.loads(
        (provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["same_error_repeat_count"] == 2


def test_r5_focus_repair_extension_allows_two_extra_bounded_iters(
    tmp_path: Path,
    monkeypatch,
):
    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path / "program",
    )
    context.work_dir.mkdir(parents=True)
    context.blueprint_path.write_text(json.dumps({"sections": []}), encoding="utf-8")
    context.final_review_path.write_text("", encoding="utf-8")
    (context.work_dir / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps({"opportunities": [{"opportunity_id": "OP01"}]}),
        encoding="utf-8",
    )
    (context.work_dir / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps({"hypotheses": [{"hypothesis_id": "H01"}]}),
        encoding="utf-8",
    )
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            captured["contract"] = contract
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.budget_exhausted,
                stop_reason="focus_call_was_not_admitted",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    result = run_research_program(
        context,
        run_id="r5_focus_repair_extension",
        cost_budget_cny=1.0,
        token_budget=1000,
        max_iters=10,
    )
    assert result.status == TaskStatus.waiting_for_human
    # The legacy broad focus-repair extension no longer bypasses the durable
    # stage cap when the artifacts are not actually accepted.
    assert captured["contract"].max_iters == 5
    assert captured["contract"].metadata["focus_repair_extension_applied"] is True
    assert captured["contract"].metadata["focus_repair_max_iters"] == 10
    assert captured["contract"].metadata["r5_discovery_stage"] == "opportunity"


def test_preflight_reserves_research_plan_and_portfolio_budget(
    tmp_path: Path,
):
    query = tmp_path / "query.json"
    query.write_text("{}", encoding="utf-8")
    kb = tmp_path / "kb.sqlite"
    kb.touch()
    config = ReviewHarnessConfig(
        query_plan_path=query,
        base_kb_sqlite=kb,
        output_root=tmp_path,
        # Budget must be ≥ new sub-budget sum (77.5) for within_budget to be True.
        global_cost_budget_cny=85,
    )
    report = ReviewHarnessOrchestrator(
        config, run_dir=tmp_path / "run"
    ).preflight()
    assert report["stage_hard_caps_cny"]["research_plan"] == 4.0
    assert (
        report["stage_hard_caps_cny"]["section_coverage_portfolio"]
        == 4.0
    )
    assert report["within_budget"] is True


def _synthesis_gate_fixture(tmp_path: Path):
    ctx = SectionAuthoringContext(
        section_id="S01",
        section_data={
            "section_id": "S01",
            "section_contract": {"word_budget": 950},
        },
        kb_sqlite=None,
        temp_kb_sqlite=None,
        work_dir=tmp_path,
    )
    graph = CanonicalAssetGraph()
    for index in range(5):
        paper_id = f"paper_{index}"
        chunk_id = f"chunk_{index}"
        graph.papers[paper_id] = PaperAsset(
            paper_id=paper_id,
            scope_fit="direct",
        )
        graph.chunks[chunk_id] = ChunkAsset(
            chunk_id=chunk_id,
            paper_id=paper_id,
            normalized_text=f"Canonical scientific content {index}.",
            scope_fit="direct",
        )
    return ctx, graph


def test_section_synthesis_gate_scales_with_available_literature(
    tmp_path: Path,
):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    assert _synthesis_source_requirement(ctx, graph) == (4, 5)


def test_section_synthesis_plan_is_fail_open_but_finished_draft_is_audited(
    tmp_path: Path,
):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    paragraphs = [
        {
            "paragraph_index": 0,
            "function": "synthesis",
            "topic_sentence": "Compare the audited approaches.",
            "key_claims": [],
            "evidence_chunk_ids": [f"chunk_{index}" for index in range(3)],
            "paper_ids": [f"paper_{index}" for index in range(3)],
            "writing_permission": "interpretive_synthesis",
            "expected_word_count": 950,
        }
    ]
    errors = _validate_argument_plan_data(ctx, graph, paragraphs)
    assert errors == []
    assert _synthesis_source_diversity_error(
        ctx,
        graph,
        paragraphs[0]["paper_ids"],
    ) is not None


def test_finished_section_must_use_planned_synthesis_breadth(tmp_path: Path):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    error = _synthesis_source_diversity_error(
        ctx,
        graph,
        ["paper_0", "paper_1"],
    )
    assert error is not None
    assert "section-level breadth" in error
    assert _synthesis_source_diversity_error(
        ctx,
        graph,
        ["paper_0", "paper_1", "paper_2", "paper_3"],
    ) is None


def test_doi_digits_do_not_trigger_quantitative_restriction():
    restriction = (
        "paper-specific quantitative results whose measured subject is "
        "outside the section scope"
    )
    sentence = (
        "Ion slicing separates crystalline films at controlled depths "
        "[REF:doi:10.1063/5.0192018]."
    )
    assert _restriction_conflict(sentence, [restriction]) is None
    assert _restriction_conflict(
        "The adjacent device achieved 97.4% efficiency "
        "[REF:doi:10.1063/5.0192018].",
        [restriction],
    ) == restriction


def test_strict_entailment_targets_high_risk_claims_not_review_synthesis():
    assert not _requires_strict_citation_entailment(
        "Together, these fabrication routes established a practical platform "
        "[REF:doi:10.1000/example]."
    )
    assert _requires_strict_citation_entailment(
        "The measured loss was 0.8 dB/m [REF:doi:10.1000/example]."
    )
    assert _requires_strict_citation_entailment(
        "This process causes sidewall damage [REF:doi:10.1000/example]."
    )


def test_author_cannot_block_when_certified_package_has_enough_sources(
    tmp_path: Path,
    monkeypatch,
):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    (tmp_path / "SECTION_AUTHORING_CONTEXT.json").write_text(
        json.dumps({
            "coverage_status": "coverage_sufficient",
            "blocking_gaps_remain": False,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "optomind_research.runtime.section_authoring_tool_registry._build_asset_graph",
        lambda _ctx: graph,
    )
    result = json.loads(_make_request_more_literature(ctx)(json.dumps({
        "feedback_items": [{
            "role": "method",
            "severity": "blocking",
            "description": "I only inspected four of the available papers.",
            "blocking_claims": ["Need a fifth paper"],
            "suggested_queries": ["more papers"],
        }],
        "authoring_can_proceed": False,
    })))
    assert result["status"] == "rejected"
    assert result["authoring_can_proceed"] is True
    assert not (tmp_path / "SECTION_COVERAGE_FEEDBACK.json").exists()


def test_author_cannot_block_completed_with_open_gaps_when_pool_is_usable(
    tmp_path: Path,
    monkeypatch,
):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    (tmp_path / "SECTION_AUTHORING_CONTEXT.json").write_text(
        json.dumps({
            "coverage_status": "completed_with_open_gaps",
            "blocking_gaps_remain": False,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "optomind_research.runtime.section_authoring_tool_registry._build_asset_graph",
        lambda _ctx: graph,
    )
    result = json.loads(_make_request_more_literature(ctx)(json.dumps({
        "feedback_items": [{
            "role": "application",
            "severity": "blocking",
            "description": "The draft has not yet used enough supplied papers.",
            "blocking_claims": ["insufficient_synthesis_source_diversity"],
            "suggested_queries": ["retrieve more literature"],
        }],
        "authoring_can_proceed": False,
    })))
    assert result["status"] == "rejected"
    assert result["authoring_can_proceed"] is True


def test_argument_plan_can_be_revised_after_audit_requests_more_breadth(
    tmp_path: Path,
    monkeypatch,
):
    ctx, graph = _synthesis_gate_fixture(tmp_path)
    monkeypatch.setattr(
        "optomind_research.runtime.section_authoring_tool_registry._build_asset_graph",
        lambda _ctx: graph,
    )
    submit = _make_submit_argument_plan(ctx)

    def payload(source_count: int) -> str:
        return json.dumps({
            "argument_flow": f"Synthesize {source_count} audited routes.",
            "paragraphs": [{
                "paragraph_index": 0,
                "function": "synthesis",
                "topic_sentence": "Compare the audited routes.",
                "key_claims": [],
                "evidence_chunk_ids": [
                    f"chunk_{index}" for index in range(source_count)
                ],
                "paper_ids": [
                    f"paper_{index}" for index in range(source_count)
                ],
                "writing_permission": "interpretive_synthesis",
                "expected_word_count": 950,
            }],
        })

    assert json.loads(submit(payload(4)))["status"] == "ok"
    revised = json.loads(submit(payload(5)))
    assert revised["status"] == "revised"
    assert revised["revision_index"] == 1
    history = json.loads(
        (tmp_path / "SECTION_ARGUMENT_PLAN_HISTORY.json").read_text(
            encoding="utf-8"
        )
    )
    assert history["total_revisions"] == 1
    current = json.loads(
        (tmp_path / "SECTION_ARGUMENT_PLAN.json").read_text(encoding="utf-8")
    )
    assert len(current["paragraphs"][0]["paper_ids"]) == 5


def test_quality_rebuild_archives_state_but_preserves_cost(tmp_path: Path):
    (tmp_path / "RESULT.json").write_text("{}", encoding="utf-8")
    (tmp_path / "SECTION_ARGUMENT_PLAN.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "COST.json").write_text(
        '{"estimated_cost_cny": 1.25}', encoding="utf-8"
    )
    archive = _archive_section_authoring_for_rebuild(
        tmp_path,
        reason="synthesis_source_diversity",
    )
    assert archive is not None
    assert (archive / "RESULT.json").exists()
    assert (archive / "SECTION_ARGUMENT_PLAN.json").exists()
    assert (archive / "COST.snapshot.json").exists()
    assert (tmp_path / "COST.json").exists()
    assert not (tmp_path / "RESULT.json").exists()


def test_editor_vocabulary_routes_to_real_revision_actions():
    report = {
        "round": 1,
        "flags": [
            {
                "flag_id": "L2-F01",
                "type": "citation_concentration",
                "section_ids": ["S02"],
                "description": "One source dominates.",
                "recommended_action": "Use independent studies.",
            },
            {
                "flag_id": "L2-F02",
                "type": "scope_bleed",
                "section_ids": ["S03"],
                "description": "The section leaks the final thesis.",
                "recommended_action": "Restore the section boundary.",
            },
            {
                "flag_id": "L2-F03",
                "type": "novel_unmapped_editor_flag",
                "section_ids": ["S04"],
            },
        ],
    }
    plan = RevisionPlanner().plan(report)
    actions = {
        item["flag_id"]: item["action"]
        for item in plan["revisions"]
    }
    assert actions["L2-F01"] == "rerun_section_with_source_synthesis"
    assert actions["L2-F02"] == "rerun_section_with_role_boundary"
    assert plan["sections_to_revise"] == ["S02", "S03"]
    assert [
        item["flag_id"] for item in plan["human_review_flags"]
    ] == ["L2-F03"]


def test_program_schema_migration_preserves_cost_and_reopens_old_outputs(
    tmp_path: Path,
):
    (tmp_path / "HYPOTHESIS_PORTFOLIO.json").write_text(
        json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": "The response will exceed 90%.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "RESEARCH_PLAN.json").write_text(
        json.dumps({"work_packages": []}),
        encoding="utf-8",
    )
    (tmp_path / "RESULT.json").write_text("{}", encoding="utf-8")
    (tmp_path / "COST.json").write_text(
        '{"estimated_cost_cny": 2.0}', encoding="utf-8"
    )
    assert _requires_quantitative_provenance_migration(tmp_path)
    _archive_program_for_schema_migration(tmp_path)
    assert not (tmp_path / "RESULT.json").exists()
    assert not (tmp_path / "HYPOTHESIS_PORTFOLIO.json").exists()
    assert (tmp_path / "COST.json").exists()
    archives = list((tmp_path / "_runtime_archive").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "COST.snapshot.json").exists()


def test_runtime_retry_preserves_scientific_assets_and_cost(tmp_path: Path):
    for name in ("RESULT.json", "AGENT_STATE.json", "EVENTS.jsonl"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    for name in (
        "SECTION_ARGUMENT_PLAN.json",
        "SECTION_EVIDENCE_PACKET.json",
        "SECTION_DRAFT_EN.md",
        "COST.json",
    ):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    archive = _archive_section_runtime_for_retry(
        tmp_path,
        terminal_status="budget_exhausted",
    )
    assert archive is not None
    assert not (tmp_path / "RESULT.json").exists()
    assert not (tmp_path / "AGENT_STATE.json").exists()
    assert (tmp_path / "SECTION_ARGUMENT_PLAN.json").exists()
    assert (tmp_path / "SECTION_EVIDENCE_PACKET.json").exists()
    assert (tmp_path / "SECTION_DRAFT_EN.md").exists()
    assert (tmp_path / "COST.json").exists()
