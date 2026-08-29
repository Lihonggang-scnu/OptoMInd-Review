"""Deterministic acceptance tests for the low-cost Research Harness upgrade."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from agentscope.tool import FunctionTool

from optomind_research.runtime.cost_ledger import (
    CostLedger,
    estimate_call_cost_cny,
)
from optomind_research.runtime.full_review_orchestrator import (
    FullReviewOrchestrator,
    OrchestratorConfig,
    SectionMaterialBundle,
)
from optomind_research.runtime.global_audit_tool_provider import (
    GlobalAuditToolProvider,
)
from optomind_research.runtime.review_lead_tool_provider import (
    ReviewLeadContext,
    ReviewLeadToolProvider,
)
from optomind_research.runtime.review_content_evaluator import (
    evaluate_review_content,
)
from optomind_research.runtime.review_mentor_library import (
    retrieve_mentor_moves,
)
from optomind_research.runtime.supplemental_visual_ingest import (
    extract_visual_candidates,
)
from optomind_research.runtime.stop_controller import StopController
from optomind_research.runtime.task_contract import TaskContract, TaskStatus
from optomind_research.runtime.tool_provider import ToolProvider
from optomind_research.runtime.visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
    validate_visual_editorial_plan_file,
)
from optomind_research.runtime.visual_editor_runner import (
    VISUAL_EDITOR_TOOL_RESULT_TOKENS,
    visual_editor_input_fingerprint,
)
from tests.test_research_worker_runtime import (
    ScriptedFakeModel,
    _make_text_response,
    _make_tool_call_response,
)


def _tool_text(tool: FunctionTool, **kwargs) -> str:
    value = tool(**kwargs)
    result = asyncio.run(value) if asyncio.iscoroutine(value) else value
    return " ".join(
        block.text for block in result.content if hasattr(block, "text")
    )


def _test_topic_identity() -> dict:
    return {
        "schema_version": "research_harness.topic_identity.v1",
        "fingerprint": "test-physical-topic",
        "normalized_question": "Review the scientific argument architecture.",
        "core_anchor_tokens": ["physical"],
        "supporting_anchor_tokens": ["mechanism", "evidence"],
        "anchor_phrases": ["scientific argument architecture"],
        "valid": True,
        "policy": "Deterministic test contract.",
    }


def _sample_blueprint() -> dict:
    roles = [
        ["foundation", "mechanism"],
        ["mechanism", "method"],
        ["method", "frontier"],
        ["frontier", "controversy"],
        ["application", "frontier"],
    ]
    sections = []
    for index, required in enumerate(roles, start=1):
        sections.append(
            {
                "section_id": f"S{index:02d}",
                "title": f"Distinct scientific argument stage {index}",
                "argument_role": (
                    f"This section performs a distinct argumentative operation {index} "
                    "that advances the article-wide thesis without repeating background."
                ),
                "chapter_argument": (
                    f"The section must adjudicate physical question {index} by "
                    "comparing mechanisms, limits, and implications across studies."
                ),
                "key_questions": [
                    f"Which evidence changes the interpretation at stage {index}?"
                ],
                "required_roles": required,
                "optional_roles": [
                    role
                    for role in (
                        "foundation",
                        "mechanism",
                        "method",
                        "frontier",
                        "controversy",
                        "application",
                    )
                    if role not in required
                ][:2],
                "synthesis_task": (
                    "Separate established conclusions, conditional judgments, and "
                    "genuinely unresolved questions for this argument stage."
                ),
                "mentor_guidance": (
                    "Use a transferable review-writing move to turn evidence into "
                    "a bounded judgment without importing mentor-topic facts."
                ),
                "scope_guardrails": ["Avoid repeating the general introduction."],
                "transition_from_previous": (
                    "" if index == 1 else "Build on the preceding established result."
                ),
                "transition_to_next": (
                    "" if index == 5 else "Pass the resulting constraint forward."
                ),
                "visual_argument_slots": (
                    [
                        {
                            "purpose": "Clarify the mechanism or comparison",
                            "preferred_kind": "schematic_or_data_figure",
                        }
                    ]
                    if index <= 3
                    else []
                ),
                "target_word_range": {"min": 900, "max": 1800},
            }
        )
    return {
        "topic_identity": _test_topic_identity(),
        "methodology_identity": "critical_narrative_review",
        "review_thesis": (
            "The review will explain how apparently separate technical routes are "
            "governed by a shared physical constraint and diverge at deployment."
        ),
        "full_review_argument": (
            "The article progresses from governing principles through design choices "
            "and evidence quality to practical boundaries and research priorities."
        ),
        "taxonomy_principle": (
            "Primary organization follows physical mechanism; material and device "
            "platforms form a secondary comparison axis."
        ),
        "narrative_strategy": (
            "Each section resolves one intellectual tension and hands a defined "
            "constraint to the next section."
        ),
        "sections": sections,
        "global_visual_strategy": {
            "policy": "Use figures only when they perform an argumentative task."
        },
    }


def test_official_cny_pricing_and_unknown_model_fallback(tmp_path: Path):
    assert estimate_call_cost_cny("qwen3.7-flash", 32_000, 0) == 0.0064
    assert estimate_call_cost_cny("qwen3.7-flash", 100_000, 0) == 0.06
    assert estimate_call_cost_cny("qwen3.7-flash", 1_000_000, 0) == 1.2
    assert estimate_call_cost_cny("qwen3.6-flash", 100_000, 10_000) == 0.192
    # Unknown models use conservative A-class-like defaults.
    assert estimate_call_cost_cny("unknown-future-model", 1_000_000, 0) == 12.0

    ledger = CostLedger(tmp_path, "run", "task")
    ledger.record_call("qwen3.7-flash", 100_000, 10_000)
    ledger.save("completed", None)
    payload = json.loads((tmp_path / "COST.json").read_text(encoding="utf-8"))
    assert payload["billing_currency"] == "CNY"
    assert payload["estimated_cost_cny"] > 0
    assert payload["per_model"]["qwen3.7-flash"]["rate_source"] == "configured_model_rate"


def test_cost_budget_stops_before_another_call(tmp_path: Path):
    contract = TaskContract(
        run_id="cost_run",
        task_id="cost_task",
        goal="test",
        cost_budget_cny=1.0,
        next_call_cost_reserve_cny=0.25,
    )
    controller = StopController(contract, tmp_path)
    status, reason = controller.check(estimated_cost_cny=0.8)
    assert status == TaskStatus.budget_exhausted
    assert "estimated_cost_cny" in reason


def test_harness_resume_costs_are_monotonic_and_recovered_from_timeline(
    tmp_path: Path,
):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    run_dir = tmp_path / "resume_run"
    run_dir.mkdir()
    (run_dir / "HARNESS_COST.json").write_text(
        json.dumps({
            "stages": {
                "section_coverage": {
                    "estimated_cost_cny": 3.0,
                    "input_tokens": 300,
                    "output_tokens": 30,
                }
            }
        }),
        encoding="utf-8",
    )
    (run_dir / "HARNESS_EVENTS.jsonl").write_text(
        json.dumps({
            "event": "stage_finished",
            "stage": "section_coverage",
            "estimated_cost_cny": 7.5,
            "input_tokens": 750,
            "output_tokens": 75,
            "wall_time_seconds": 90.0,
        }) + "\n",
        encoding="utf-8",
    )
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=tmp_path / "query.json",
            base_kb_sqlite=tmp_path / "kb.sqlite",
            output_root=tmp_path,
        ),
        run_dir=run_dir,
    )
    recovered = harness.stage_costs["section_coverage"]
    assert recovered["estimated_cost_cny"] == 7.5
    assert recovered["input_tokens"] == 750
    assert recovered["output_tokens"] == 75

    harness._record_stage(
        "section_coverage",
        "completed",
        1.0,
        100,
        10,
        {"reused": True},
    )
    retained = harness.stage_costs["section_coverage"]
    assert retained["estimated_cost_cny"] == 7.5
    assert retained["input_tokens"] == 750
    assert retained["last_attempt_cost_cny"] == 1.0


def test_resume_stage_ceiling_adds_new_allowance_to_historical_spend():
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessOrchestrator,
    )

    assert ReviewHarnessOrchestrator._cumulative_stage_ceiling(
        16.5, 3.0
    ) == 19.5


def test_review_lead_blueprint_validator_and_m1_boundary(tmp_path: Path):
    m1_path = tmp_path / "m1.json"
    m1_path.write_text(
        json.dumps(
            {
                "taxonomy_design": [
                    {
                        "move": "Organize the field by mechanism.",
                        "why_it_matters": "It prevents mixed classification levels.",
                        "reuse_for_our_review_system": "Use one primary axis.",
                        "possible_overreach": "",
                        "confidence": "high",
                        "source_genre": "review",
                        "adequacy_for_batch_library": "pass",
                        "source_paper_id": "mentor-paper-only",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ctx = ReviewLeadContext(
        user_question="Review a general optical research topic.",
        problem_understanding="Review mechanisms, methods, frontiers, and uses.",
        scope_definition="Cover the governing physics and practical limits.",
        work_dir=tmp_path / "lead",
        m1_library_path=m1_path,
        topic_identity=_test_topic_identity(),
    )
    provider = ReviewLeadToolProvider(ctx)
    tools = {tool.name: tool for tool in provider.get_tools(ctx.work_dir)}

    mentor = json.loads(
        _tool_text(
            tools["consult_review_mentor"],
            categories_json='["taxonomy_design"]',
            planning_question="How should the taxonomy be organized?",
            max_per_category=2,
        )
    )
    assert mentor["status"] == "ok"
    assert "source_paper_id" not in json.dumps(mentor)

    submitted = json.loads(
        _tool_text(
            tools["submit_review_blueprint"],
            blueprint_json=json.dumps(_sample_blueprint()),
        )
    )
    assert submitted["status"] == "ok"
    validation = _tool_text(tools["validate_review_blueprint_package"])
    assert "VALIDATION_PASSED" in validation


def test_review_lead_normalizes_unambiguous_schema_aliases(tmp_path: Path):
    blueprint = _sample_blueprint()
    for section in blueprint["sections"]:
        section["id"] = section.pop("section_id")
        section["transitions"] = {
            "from_previous": section.pop("transition_from_previous"),
            "to_next": section.pop("transition_to_next"),
        }
        section["visual_slots"] = section.pop("visual_argument_slots")
        section.pop("optional_roles")
    ctx = ReviewLeadContext(
        user_question="Review a general optical research topic.",
        problem_understanding="Review mechanisms, methods, frontiers, and uses.",
        scope_definition="Cover governing physics and practical limits.",
        work_dir=tmp_path / "lead_alias",
        topic_identity=_test_topic_identity(),
    )
    provider = ReviewLeadToolProvider(ctx)
    tools = {tool.name: tool for tool in provider.get_tools(ctx.work_dir)}
    submitted = json.loads(
        _tool_text(
            tools["submit_review_blueprint"],
            blueprint_json=json.dumps(blueprint),
        )
    )
    assert submitted["status"] == "ok"
    stored = json.loads(
        (ctx.work_dir / "REVIEW_BLUEPRINT.json").read_text(encoding="utf-8")
    )
    assert stored["sections"][0]["section_id"] == "S01"
    assert "visual_argument_slots" in stored["sections"][0]
    assert "transition_to_next" in stored["sections"][0]
    assert "VALIDATION_PASSED" in _tool_text(
        tools["validate_review_blueprint_package"]
    )


def test_global_audit_context_uses_inline_refs_when_audit_is_stale(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "S01"
    work_dir.mkdir()
    (work_dir / "SECTION_DRAFT_EN.md").write_text(
        "Supported synthesis [REF:doi:10.1000/a] and "
        "context [REF:CorpusId:123].",
        encoding="utf-8",
    )
    (work_dir / "SECTION_CITATION_AUDIT.json").write_text(
        json.dumps({"total_citations": 0, "citations": []}),
        encoding="utf-8",
    )
    provider = GlobalAuditToolProvider(
        merged_draft="",
        section_registry={
            "sections": [
                {
                    "section_id": "S01",
                    "title": "Section",
                    "status": "completed",
                    "work_dir": str(work_dir),
                }
            ]
        },
        blueprint={"input_context": {"user_question": "Optics review"}},
        work_dir=tmp_path,
        round_num=1,
    )
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    context = json.loads(_tool_text(tools["load_global_review_context"]))
    section = context["sections"][0]
    assert section["citation_count"] == 2
    assert section["citation_count_sources"] == {
        "audit_total": 0,
        "audit_rows": 0,
        "inline_unique_markers": 2,
    }


def test_global_audit_provider_bounded_read_submit_validate(tmp_path: Path):
    sections = []
    for index in (1, 2):
        work_dir = tmp_path / f"S{index:02d}"
        work_dir.mkdir()
        (work_dir / "SECTION_DRAFT_EN.md").write_text(
            (
                "This section establishes a distinct argument. "
                "It synthesizes several studies into a bounded conclusion.\n\n"
                "The closing paragraph transfers one constraint to the next stage."
            ),
            encoding="utf-8",
        )
        sections.append(
            {
                "section_id": f"S{index:02d}",
                "title": f"Section {index}",
                "argument_role": f"Role {index}",
                "status": "completed",
                "work_dir": str(work_dir),
            }
        )
    provider = GlobalAuditToolProvider(
        merged_draft="",
        section_registry={"sections": sections},
        blueprint={
            "input_context": {"user_question": "Review an optical topic."},
            "full_review_argument": "A staged scientific argument.",
        },
        work_dir=tmp_path,
        round_num=1,
    )
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    assert "consult_review_mentor_for_audit" not in (
        provider.get_allowed_tool_names()
    )
    context = json.loads(_tool_text(tools["load_global_review_context"]))
    assert context["section_count"] == 2
    window = json.loads(
        _tool_text(
            tools["read_section_text"],
            section_id="S01",
            start_paragraph=0,
            max_paragraphs=1,
        )
    )
    assert window["returned_paragraphs"] == 1
    second_window = json.loads(
        _tool_text(
            tools["read_section_text"],
            section_id="S02",
            start_paragraph=0,
            max_paragraphs=1,
        )
    )
    assert second_window["section_reads_remaining"] == 0
    blocked_window = json.loads(
        _tool_text(
            tools["read_section_text"],
            section_id="S01",
            start_paragraph=1,
            max_paragraphs=1,
        )
    )
    assert blocked_window["error"] == "section_read_budget_exhausted"
    bad = json.loads(
        _tool_text(
            tools["submit_audit_flags"],
            flags_json=json.dumps(
                [
                    {
                        "type": "scope_drift",
                        "severity": "warning",
                        "section_ids": ["S99"],
                        "description": "This deliberately references an unknown section.",
                        "blocking": False,
                    }
                ]
            ),
        )
    )
    assert bad["status"] == "error"
    good = json.loads(
        _tool_text(
            tools["submit_audit_flags"],
            flags_json=json.dumps(
                [
                    {
                        "type": "cross_section_progression",
                        "severity": "warning",
                        "section_ids": ["S01", "S02"],
                        "description": (
                            "The second section repeats the first section's setup "
                            "instead of advancing the article-wide argument."
                        ),
                        "blocking": False,
                    }
                ]
            ),
        )
    )
    assert good["status"] == "ok"
    assert "VALIDATION_PASSED" in _tool_text(
        tools["validate_global_audit_package"]
    )
    # Real models commonly use semantically clear aliases.  The adapter must
    # normalize them instead of wasting another expensive editor turn.
    aliased = json.loads(
        _tool_text(
            tools["submit_audit_flags"],
            flags_json=json.dumps(
                {
                    "flags": [
                        {
                            "category": "Source Concentration",
                            "severity": "major",
                            "location": "S01 and S02",
                            "description": (
                                "Both sections rely too heavily on the same "
                                "paper and therefore lack independent synthesis."
                            ),
                            "rationale": "The citation distribution is concentrated.",
                            "recommendation": "Add independent sources before revision.",
                        }
                    ]
                }
            ),
        )
    )
    assert aliased["status"] == "ok"
    normalized = json.loads(provider.output_path.read_text(encoding="utf-8"))
    assert normalized["flags"][0]["type"] == "source_concentration"
    assert normalized["flags"][0]["severity"] == "error"
    assert normalized["flags"][0]["blocking"] is True
    assert normalized["flags"][0]["section_ids"] == ["S01", "S02"]


class _CustomValidationProvider(ToolProvider):
    def get_allowed_tool_names(self):
        return ["write_custom_package", "validate_custom_package"]

    def get_tools(self, work_dir: Path):
        def write_custom_package() -> str:
            (work_dir / "CUSTOM.json").write_text("{}", encoding="utf-8")
            return '{"status":"ok"}'

        def validate_custom_package() -> str:
            return (
                "VALIDATION_PASSED: custom package exists."
                if (work_dir / "CUSTOM.json").exists()
                else "VALIDATION_FAILED: missing custom package."
            )

        return [
            FunctionTool(write_custom_package),
            FunctionTool(validate_custom_package),
        ]


def test_research_worker_accepts_new_allowlisted_validator(tmp_path: Path):
    from optomind_research.runtime.research_worker import ResearchWorker

    work_dir = tmp_path / "custom"
    contract = TaskContract(
        run_id="dynamic_validator_run",
        task_id="dynamic_validator_task",
        goal="Write and validate a custom package.",
        allowed_tools=[
            "write_custom_package",
            "validate_custom_package",
        ],
        expected_outputs=["CUSTOM.json"],
        max_iters=5,
        token_budget=20_000,
    )
    model = ScriptedFakeModel(
        [
            _make_tool_call_response("write_custom_package", {}),
            _make_tool_call_response("validate_custom_package", {}),
            _make_text_response("Complete."),
        ]
    )
    result = ResearchWorker(
        tool_provider=_CustomValidationProvider(),
        _model_override=model,
        _work_dir_override=work_dir,
    ).run(contract)
    assert result.status == TaskStatus.completed
    assert result.validation_passed is True


def test_cost_ledger_accumulates_across_process_resume(tmp_path: Path):
    first = CostLedger(tmp_path, "same_run", "same_task")
    first.record_call("qwen3.7-flash", 10_000, 1_000)
    first.record_tool_call()
    first.save("validation_failed", "simulated interruption")

    second = CostLedger(tmp_path, "same_run", "same_task")
    assert second.total_input_tokens == 10_000
    assert second.model_call_count == 1
    second.record_call("qwen3.7-flash", 20_000, 2_000)
    second.save("completed", None)

    value = json.loads((tmp_path / "COST.json").read_text(encoding="utf-8"))
    assert value["total_input_tokens"] == 30_000
    assert value["total_output_tokens"] == 3_000
    assert value["model_call_count"] == 2
    assert value["estimated_cost_cny"] > 0


def test_cost_ledger_recovers_paid_calls_from_interrupted_event_log(
    tmp_path: Path,
):
    (tmp_path / "EVENTS.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "model_call_end",
                        "model": "qwen3.7-max",
                        "input_tokens": 1200,
                        "output_tokens": 300,
                    }
                ),
                json.dumps(
                    {
                        "event": "tool_call",
                        "tool": "some_tool",
                        "call_id": "call-1",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = CostLedger(tmp_path, "interrupted", "task")
    assert ledger.model_call_count == 1
    assert ledger.total_input_tokens == 1200
    assert ledger.total_output_tokens == 300
    assert ledger.tool_call_count == 1
    assert ledger.estimated_cost_cny() > 0


def test_worker_does_not_reuse_completed_result_for_different_task(
    tmp_path: Path,
):
    from optomind_research.runtime.research_worker import ResearchWorker

    work_dir = tmp_path / "shared"
    first = TaskContract(
        run_id="shared_run",
        task_id="task_one",
        goal="write",
        allowed_tools=["write_custom_package", "validate_custom_package"],
        expected_outputs=["CUSTOM.json"],
        max_iters=4,
    )
    script = [
        _make_tool_call_response("write_custom_package", {}),
        _make_tool_call_response("validate_custom_package", {}),
        _make_text_response("Complete."),
    ]
    result1 = ResearchWorker(
        tool_provider=_CustomValidationProvider(),
        _model_override=ScriptedFakeModel(list(script)),
        _work_dir_override=work_dir,
    ).run(first)
    assert result1.task_id == "task_one"

    second = first.model_copy(
        update={"task_id": "task_two", "goal": "write again"}
    )
    result2 = ResearchWorker(
        tool_provider=_CustomValidationProvider(),
        _model_override=ScriptedFakeModel(list(script)),
        _work_dir_override=work_dir,
    ).run(second)
    assert result2.task_id == "task_two"


def test_full_review_incomplete_section_cannot_report_completed(tmp_path: Path):
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text('{"sections":[]}', encoding="utf-8")
    orchestrator = FullReviewOrchestrator(
        OrchestratorConfig(
            blueprint_path=blueprint_path,
            output_root=tmp_path,
        )
    )
    orchestrator._section_registry = {
        "sections": [
            {"section_id": "S01", "status": "completed"},
            {"section_id": "S02", "status": "budget_exhausted"},
        ]
    }
    assert (
        orchestrator._determine_final_status(
            {"blocking_flags": 0, "total_flags": 0}
        )
        == "partial"
    )


def test_full_review_preserves_review_lead_section_contract(tmp_path: Path):
    kb = tmp_path / "kb.sqlite"
    conn = sqlite3.connect(str(kb))
    try:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT PRIMARY KEY, text TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('chunk_1', 'scientific text')"
        )
        conn.commit()
    finally:
        conn.close()
    package = tmp_path / "SECTION_MATERIAL_PACKAGE.json"
    ledger = tmp_path / "SECTION_SOURCE_LEDGER.json"
    package.write_text("{}", encoding="utf-8")
    ledger.write_text('{"sources":[]}', encoding="utf-8")
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text('{"sections":[]}', encoding="utf-8")
    orchestrator = FullReviewOrchestrator(
        OrchestratorConfig(
            blueprint_path=blueprint_path,
            output_root=tmp_path,
            material_bundles={
                "S01": SectionMaterialBundle(
                    material_package_path=package,
                    source_ledger_path=ledger,
                    kb_sqlite=kb,
                )
            },
        ),
        run_dir=tmp_path / "full_review",
    )
    orchestrator._work_dir = tmp_path / "full_review"
    orchestrator._work_dir.mkdir()
    section = _sample_blueprint()["sections"][0]
    context = orchestrator._build_section_context(
        section,
        None,
        _sample_blueprint()["sections"][1],
        _sample_blueprint(),
        orchestrator._work_dir / "sections" / "S01",
    )
    assert context.section_data["chapter_argument"] == section[
        "chapter_argument"
    ]
    assert context.section_data["required_roles"] == section[
        "required_roles"
    ]
    assert context.section_data["synthesis_task"] == section[
        "synthesis_task"
    ]
    assert context.section_data["section_contract"]["word_budget"] == 1350
    assert context.mentor_advice["guidance"] == section["mentor_guidance"]


def test_section_coverage_uses_stable_run_dir_and_reuses_package(
    tmp_path: Path,
):
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
        SectionCoverageOrchestratorConfig,
    )

    sample_section = dict(_sample_blueprint()["sections"][0])
    # Keep this resume/cache test intentionally small.  Production sections
    # derive a larger article-quality breadth target from their word budget.
    sample_section["literature_coverage_target"] = {
        "minimum_unique_sources": 4,
        "minimum_direct_sources": 4,
    }
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(
        json.dumps({"sections": [sample_section]}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "coverage"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "RESULT.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "total_input_tokens": 123,
                "total_output_tokens": 45,
                "estimated_cost_cny": 0.2,
            }
        ),
        encoding="utf-8",
    )
    (section_dir / "SECTION_MATERIAL_PACKAGE.json").write_text(
        '{"blocking_gaps_remain":false}',
        encoding="utf-8",
    )
    (section_dir / "SECTION_CONTEXT.json").write_text(
        json.dumps({
            "section_id": "S01",
            "section_title": sample_section["title"],
            "chapter_argument": sample_section["chapter_argument"],
            "required_roles": sample_section["required_roles"],
        }),
        encoding="utf-8",
    )
    (section_dir / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({
            "roles": {
                role: {"priority": "required"}
                for role in sample_section["required_roles"]
            }
        }),
        encoding="utf-8",
    )
    (section_dir / "LOCAL_COVERAGE_AUDIT.json").write_text(
        json.dumps({"blocking_gaps": []}),
        encoding="utf-8",
    )
    (section_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({
            "section_id": "S01",
            "sources": [
                {
                    "paper_id": f"paper_{role}_{replica}",
                    "literature_role": role,
                    "scope_fit": "direct",
                    "canonical_chunk_ids": [f"chunk_{role}_{replica}"],
                    "acquisition_status": "fulltext",
                    "section_id": "S01",
                }
                for role in sample_section["required_roles"]
                for replica in range(2)
            ],
        }),
        encoding="utf-8",
    )
    orchestrator = SectionCoverageOrchestrator(
        SectionCoverageOrchestratorConfig(
            blueprint_path=blueprint_path,
            base_kb_sqlite=None,
            output_root=tmp_path / "unused",
        ),
        run_dir=run_dir,
    )
    result = orchestrator.run()
    assert result.work_dir == run_dir
    assert result.status == "completed"
    assert result.sections_completed == 1
    # Reusing an already-paid package incurs no new cost in this invocation.
    # The historical amount remains explicit instead of being charged twice.
    assert result.total_cost_cny == 0.0
    assert orchestrator.records[0]["previous_cost_cny"] == 0.2
    assert "S01" in result.material_bundles


def test_section_coverage_deterministically_finalizes_after_worker_budget_stop(
    tmp_path: Path,
):
    """A paid run that wrote valid artifacts must not need one more LLM turn."""
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
        SectionCoverageOrchestratorConfig,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text('{"sections":[]}', encoding="utf-8")
    work_dir = tmp_path / "coverage" / "sections" / "S01"
    work_dir.mkdir(parents=True)
    context = SectionCoverageContext(
        section_id="S01",
        section_data={
            "section_id": "S01",
            "title": "Mechanism",
                "chapter_argument": "Explain the governing mechanism.",
                "required_roles": ["mechanism"],
                "optional_roles": [],
                "literature_coverage_target": {
                    "minimum_unique_sources": 3,
                    "minimum_direct_sources": 3,
                },
        },
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=work_dir,
    )
    (work_dir / "SECTION_CONTEXT.json").write_text(
        json.dumps(
            {
                "section_id": "S01",
                "section_title": "Mechanism",
                "chapter_argument": "Explain the governing mechanism.",
                "required_roles": ["mechanism"],
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps(
            {"roles": {"mechanism": {"priority": "required"}}}
        ),
        encoding="utf-8",
    )
    (work_dir / "LOCAL_COVERAGE_AUDIT.json").write_text(
        json.dumps({"blocking_gaps": []}),
        encoding="utf-8",
    )
    (work_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(
            {
                "section_id": "S01",
                    "sources": [
                        {
                            "paper_id": f"paper_mechanism_{replica}",
                            "literature_role": "mechanism",
                            "scope_fit": "direct",
                            "canonical_chunk_ids": [
                                f"chunk_mechanism_{replica}"
                            ],
                            "acquisition_status": "fulltext",
                            "section_id": "S01",
                        }
                        for replica in range(3)
                    ],
            }
        ),
        encoding="utf-8",
    )
    orchestrator = SectionCoverageOrchestrator(
        SectionCoverageOrchestratorConfig(
            blueprint_path=blueprint_path,
            base_kb_sqlite=None,
            output_root=tmp_path / "unused",
        ),
        run_dir=tmp_path / "coverage",
    )
    receipt = orchestrator._try_deterministic_finalize(
        context,
        worker_status="budget_exhausted",
        stop_reason="final call not admitted",
    )
    assert receipt["recovered"] is True
    assert (work_dir / "SECTION_MATERIAL_PACKAGE.json").exists()
    durable = json.loads(
        (work_dir / "COVERAGE_RECOVERY.json").read_text(encoding="utf-8")
    )
    assert durable["worker_status"] == "budget_exhausted"
    assert "VALIDATION_PASSED" in durable["validation_result"]


def test_unified_harness_preflight_is_hard_budgeted(tmp_path: Path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    # global_cost_budget_cny uses the new default (85.0) so that sub-budget
    # caps (now summing to 80.0, including the quality-first visual envelope)
    # stay within the global ceiling.
    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "query.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
    )
    orchestrator = ReviewHarnessOrchestrator(
        config,
        run_dir=tmp_path / "run",
    )
    preflight = orchestrator.preflight()
    assert preflight["within_budget"] is True
    assert preflight["allocated_max_cny"] == 80.0
    assert preflight["unallocated_reserve_cny"] == 40.0
    result = orchestrator.run()
    assert result.status == "failed"
    assert result.completed_stage == "query_plan_missing"


def test_unified_harness_detects_missing_planned_material_sections():
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessOrchestrator,
    )

    blueprint = {
        "sections": [
            {"section_id": "S01"},
            {"section_id": "S02"},
            {"section_id": "S03"},
        ]
    }
    assert ReviewHarnessOrchestrator._missing_planned_material_sections(
        blueprint,
        {"S01": object(), "S03": object()},
    ) == ["S02"]


def test_react_context_ceiling_is_not_the_primary_cost_gate(tmp_path: Path):
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestratorConfig,
    )

    coverage = SectionCoverageOrchestratorConfig(
        blueprint_path=tmp_path / "blueprint.json",
        base_kb_sqlite=None,
        output_root=tmp_path,
    )
    authoring = OrchestratorConfig(
        blueprint_path=tmp_path / "blueprint.json",
        output_root=tmp_path,
    )
    assert coverage.token_budget_per_section == 500_000
    assert authoring.section_token_budget == 1_000_000


def test_mentor_retrieval_uses_writing_problem_and_hides_source_ids(
    tmp_path: Path,
):
    library = tmp_path / "m1.json"
    library.write_text(
        json.dumps(
            {
                "taxonomy_design": [
                    {
                        "move": "Organize competing routes by their physical mechanism.",
                        "why_it_matters": "Mechanism axes prevent mixed classification levels.",
                        "reuse_for_our_review_system": "Use mechanism as the primary axis.",
                        "confidence": "high",
                        "source_genre": "review",
                        "adequacy_for_batch_library": "pass",
                        "source_paper_id": "secret-mentor-a",
                        "evidence_locator": "page 3",
                    },
                    {
                        "move": "List examples in chronological order.",
                        "why_it_matters": "A timeline can expose historical development.",
                        "reuse_for_our_review_system": "Use a timeline for history.",
                        "confidence": "medium",
                        "source_genre": "primary_article",
                        "adequacy_for_batch_library": "borderline",
                        "source_paper_id": "secret-mentor-b",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = retrieve_mentor_moves(
        library,
        categories=["taxonomy_design"],
        planning_question=(
            "How should competing physical mechanisms define the taxonomy axis?"
        ),
        max_per_category=1,
    )
    serialized = json.dumps(result)
    assert "physical mechanism" in serialized
    assert "secret-mentor" not in serialized
    assert "evidence_locator" not in serialized


def test_supplemental_html_visual_ingest_keeps_single_figure_pending(
    tmp_path: Path,
):
    from PIL import Image

    image_path = tmp_path / "figure1.png"
    Image.new("RGB", (240, 180), color=(40, 80, 120)).save(image_path)
    html_path = tmp_path / "paper.html"
    html_path.write_text(
        (
            "<html><body><section><h2>Mechanism</h2><figure>"
            f"<img src=\"{image_path.as_uri()}\" alt=\"Figure 1 mechanism\">"
            "<figcaption>Figure 1. Optical mechanism and measured response.</figcaption>"
            "</figure></section></body></html>"
        ),
        encoding="utf-8",
    )
    kb_path = tmp_path / "supplemental.sqlite"
    result = extract_visual_candidates(
        source_path=html_path,
        staging_kb=kb_path,
        output_dir=tmp_path / "visuals",
        paper_id="paper-1",
        doi="10.1000/test",
        title="A test optical paper",
    )
    assert result["status"] == "pending_multimodal_review"
    assert result["eligible_visual_chunks"] == 1
    conn = sqlite3.connect(str(kb_path))
    try:
        row = conn.execute(
            "SELECT chunk_kind, visual_argument_status, local_image_path "
            "FROM visual_chunks"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "single_figure"
    assert row[1] == "pending_multimodal_review"
    assert Path(row[2]).is_file()


def test_content_quality_gate_checks_article_not_sentence_citation_density(
    tmp_path: Path,
):
    blueprint = _sample_blueprint()
    # Keep the deterministic fixture compact while satisfying its own
    # declared contract.
    for section in blueprint["sections"]:
        section["target_word_range"] = {"min": 100, "max": 300}
    paragraphs = []
    citations = []
    for index, section in enumerate(blueprint["sections"], 1):
        body = " ".join(
            [
                (
                    "The literature converges on a bounded physical interpretation "
                    "while exposing a distinct engineering trade-off."
                )
            ]
            * 8
        )
        paragraphs.append(
            f"## {section['title']}\n\n{body} "
            f"A pivotal quantitative observation is traceable [REF:paper-{index}]."
        )
        citations.append(
            {
                "section_id": section["section_id"],
                "paper_id": f"paper-{index}",
                "trace_status": "verified",
            }
        )
    review_path = tmp_path / "FINAL_REVIEW_EN.md"
    review_path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    citation_path = tmp_path / "FULL_REVIEW_CITATION_MAP.json"
    citation_path.write_text(
        json.dumps({"citations": citations}),
        encoding="utf-8",
    )
    visual_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    visual_path.write_text(
        json.dumps(
            {
                "placements": [],
                "conceptual_figure_requests": [
                    {
                        "section_id": "S01",
                        "status": "pending_generation_and_review",
                    }
                ],
                "unfilled_visual_needs": [],
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_review_content(
        final_review_path=review_path,
        blueprint=blueprint,
        visual_plan_path=visual_path,
        citation_map_path=citation_path,
        output_dir=tmp_path,
    )
    assert report["status"] != "failed"
    assert report["metrics"]["unique_inline_reference_count"] == 5
    assert report["metrics"]["recommended_minimum_unique_papers"] == 20
    assert "low_review_wide_source_diversity" in report["warnings"]
    assert "citation_map_missing_for_referenced_review" not in report[
        "blocking_issues"
    ]


def test_quality_gate_accepts_identity_resolved_final_text_only_citation(
    tmp_path: Path,
):
    blueprint = _sample_blueprint()
    for section in blueprint["sections"]:
        section["target_word_range"] = {"min": 30, "max": 200}
    review_path = tmp_path / "FINAL_REVIEW_EN.md"
    review_path.write_text(
        "\n\n".join(
            f"## {section['title']}\n\n"
            + (
                "The literature supports a bounded physical interpretation "
                "while exposing a distinct engineering trade-off. "
            )
            * 8
            + "[REF:paper-1]"
            for section in blueprint["sections"]
        ),
        encoding="utf-8",
    )
    citation_path = tmp_path / "FINAL_CITATION_MAP.json"
    citation_path.write_text(
        json.dumps(
            {
                "citations": [
                    {
                        "paper_id": "paper-1",
                        "citation_identity": "doi:10.1000/example",
                        "trace_status": "final_text_only",
                        "doi": "10.1000/example",
                        "title": "A resolved example paper",
                        "year": "2024",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_review_content(
        final_review_path=review_path,
        blueprint=blueprint,
        visual_plan_path=None,
        citation_map_path=citation_path,
        output_dir=tmp_path,
    )

    assert "citation_map_contains_unresolved_entries" not in report[
        "blocking_issues"
    ]
    assert report["metrics"]["citation_map_unresolved_count"] == 0
    assert report["metrics"]["citation_map_final_text_only_count"] == 1
    assert "citation_map_final_text_only_entries" in report["warnings"]


def test_final_content_gate_rejects_any_missing_planned_section(tmp_path: Path):
    blueprint = _sample_blueprint()
    for section in blueprint["sections"]:
        section["target_word_range"] = {"min": 50, "max": 200}
    delivered = blueprint["sections"][:-1]
    review_path = tmp_path / "FINAL_REVIEW_EN.md"
    review_path.write_text(
        "\n\n".join(
            (
                f"## {section['title']}\n\n"
                + " ".join(
                    ["A complete scientific paragraph advances this argument."]
                    * 12
                )
            )
            for section in delivered
        ),
        encoding="utf-8",
    )
    report = evaluate_review_content(
        final_review_path=review_path,
        blueprint=blueprint,
        visual_plan_path=None,
        citation_map_path=None,
        output_dir=tmp_path,
    )
    assert report["status"] == "failed"
    assert "planned_sections_not_delivered" in report["blocking_issues"]
    assert report["metrics"]["missing_planned_sections"] == ["S05"]


def test_literature_feedback_retry_archives_only_affected_section(
    tmp_path: Path,
):
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(
        json.dumps({"sections": _sample_blueprint()["sections"][:2]}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "authoring" / "full_review"
    for section_id in ("S01", "S02"):
        section_dir = run_dir / "sections" / section_id
        section_dir.mkdir(parents=True)
        (section_dir / "SECTION_DRAFT_EN.md").write_text(
            f"draft for {section_id}", encoding="utf-8"
        )
    orchestrator = FullReviewOrchestrator(
        OrchestratorConfig(
            blueprint_path=blueprint_path,
            output_root=tmp_path / "authoring",
        ),
        run_dir=run_dir,
    )
    orchestrator._work_dir = run_dir
    orchestrator._state = {"run_id": "feedback-test", "state": "partial"}
    orchestrator._section_registry = {
        "sections": [
            {
                "section_id": "S01",
                "status": "needs_more_literature",
                "work_dir": str(run_dir / "sections" / "S01"),
                "cost_cny": 0.4,
            },
            {
                "section_id": "S02",
                "status": "completed",
                "work_dir": str(run_dir / "sections" / "S02"),
                "cost_cny": 0.5,
            },
        ]
    }
    reopened = orchestrator.prepare_literature_feedback_retry(["S01"])
    assert reopened == ["S01"]
    assert orchestrator._section_registry["sections"][0]["status"] == "pending"
    assert orchestrator._section_registry["sections"][0]["cost_cny"] == 0.4
    assert not (
        run_dir / "sections" / "S01" / "SECTION_DRAFT_EN.md"
    ).exists()
    assert (
        run_dir / "sections" / "S02" / "SECTION_DRAFT_EN.md"
    ).exists()
    assert list(
        (run_dir / ".history").rglob("SECTION_DRAFT_EN.md")
    )


def test_feedback_blueprint_targets_only_author_reported_gap(tmp_path: Path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    authoring_dir = tmp_path / "authoring"
    feedback_dir = authoring_dir / "sections" / "S02"
    feedback_dir.mkdir(parents=True)
    (feedback_dir / "SECTION_COVERAGE_FEEDBACK.json").write_text(
        json.dumps(
            {
                "state": "needs_more_literature",
                "feedback_items": [
                    {
                        "role": "controversy",
                        "severity": "blocking",
                        "description": (
                            "Find direct evidence that distinguishes two "
                            "competing interpretations under matched conditions."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "q.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
    )
    harness = ReviewHarnessOrchestrator(
        config, run_dir=tmp_path / "harness"
    )
    path = harness._write_feedback_blueprint(
        _sample_blueprint(),
        authoring_dir,
        ["S02"],
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert [item["section_id"] for item in value["sections"]] == ["S02"]
    assert "controversy" in value["sections"][0]["required_roles"]
    assert value["feedback_scope"] == "pivotal_author_reported_gaps_only"


def test_review_lead_accepts_concise_but_meaningful_argument_role(
    tmp_path: Path,
):
    blueprint = _sample_blueprint()
    blueprint["sections"][0]["argument_role"] = (
        "Establishes the governing physical constraint."
    )
    context = ReviewLeadContext(
        user_question="Review a general optical topic.",
        problem_understanding="Compare mechanisms and practical limits.",
        scope_definition="Cover foundations through applications.",
        work_dir=tmp_path,
        topic_identity=_test_topic_identity(),
    )
    provider = ReviewLeadToolProvider(context)
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    _tool_text(
        tools["submit_review_blueprint"],
        blueprint_json=json.dumps(blueprint),
    )
    validation = _tool_text(tools["validate_review_blueprint_package"])
    assert validation.startswith("VALIDATION_PASSED")


def test_section_coverage_systemic_failure_is_fail_fast():
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
    )

    assert SectionCoverageOrchestrator._is_systemic_runtime_failure(
        {
            "status": "waiting_for_human",
            "stop_reason": (
                "Unexpected permission request for tools: "
                "['load_section_context']"
            ),
        }
    )
    assert not SectionCoverageOrchestrator._is_systemic_runtime_failure(
        {
            "status": "needs_more_literature",
            "stop_reason": "required controversy evidence remains sparse",
        }
    )


def test_section_coverage_no_more_candidates_is_not_systemic_runtime_failure():
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
    )

    assert not SectionCoverageOrchestrator._is_systemic_runtime_failure(
        {
            "status": "failed",
            "stop_reason": "no more candidates after bounded waves",
        }
    )


def test_visual_editor_batches_article_context_and_candidates(
    tmp_path: Path,
    monkeypatch,
):
    """The low-cost path should inspect an article in one tool round."""
    review_dir = tmp_path / "review"
    draft_dir = review_dir / "sections" / "S01"
    draft_dir.mkdir(parents=True)
    (draft_dir / "SECTION_DRAFT_EN.md").write_text(
        "# Mechanism\n\nA compact scientific argument.",
        encoding="utf-8",
    )
    context = VisualEditorContext(
        blueprint={
            "sections": [{
                "section_id": "S01",
                "title": "Mechanism",
                "argument_role": "Explain the mechanism.",
            }]
        },
        review_work_dir=review_dir,
        work_dir=tmp_path / "visual",
    )
    provider = VisualEditorToolProvider(context)
    monkeypatch.setattr(
        provider,
        "_verified_candidates_for_section",
        lambda section_id, *, top_k: [{
            "chunk_id": "visual_1",
            "paper_id": "paper_1",
            "local_image_path": str(tmp_path / "figure.png"),
            "path_verified": True,
        }],
    )
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    assert "inspect_article_visual_candidates" in tools
    payload = json.loads(
        _tool_text(
            tools["inspect_article_visual_candidates"],
            top_k_per_section=4,
            draft_excerpt_characters=1000,
        )
    )
    assert payload["section_count"] == 1
    assert payload["sections"][0]["candidates"][0][
        "visual_chunk_id"
    ] == "visual_1"
    assert "compact scientific argument" in payload["sections"][0]["draft_excerpt"]
    assert payload["cache_safe"] is True


def test_visual_article_payload_keeps_all_sections_below_cache_limit(
    tmp_path: Path,
    monkeypatch,
):
    review_dir = tmp_path / "review"
    sections = []
    for index in range(1, 9):
        section_id = f"S{index:02d}"
        draft_dir = review_dir / "sections" / section_id
        draft_dir.mkdir(parents=True)
        (draft_dir / "SECTION_DRAFT_EN.md").write_text(
            "Long scientific paragraph. " * 200,
            encoding="utf-8",
        )
        sections.append(
            {
                "section_id": section_id,
                "title": f"Section {index}",
                "argument_role": "A detailed argumentative role. " * 30,
            }
        )
    provider = VisualEditorToolProvider(
        VisualEditorContext(
            blueprint={"sections": sections},
            review_work_dir=review_dir,
            work_dir=tmp_path / "visual",
        )
    )

    def _fake_candidates(section_id: str, *, top_k: int):
        return [
            {
                "chunk_id": f"{section_id}-visual-{candidate_index}",
                "paper_id": f"{section_id}-paper-{candidate_index}",
                "doi": f"10.1/{section_id.lower()}.{candidate_index}",
                "title": "A very long but relevant scientific paper title " * 5,
                "local_image_path": str(
                    tmp_path / ("deep-directory-" * 10) / "figure.png"
                ),
                "caption_preview": "Detailed scientific caption " * 30,
                "visual_argument_type": "mechanism_anchor",
                "score": 0.91,
            }
            for candidate_index in range(top_k)
        ]

    monkeypatch.setattr(
        provider,
        "_verified_candidates_for_section",
        _fake_candidates,
    )
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    raw = _tool_text(
        tools["inspect_article_visual_candidates"],
        top_k_per_section=6,
        draft_excerpt_characters=1400,
    )
    payload = json.loads(raw)
    assert payload["section_count"] == 8
    assert payload["all_sections_included"] is True
    assert payload["cache_safe"] is True
    # The envelope must stay inside the limit the visual editor contract
    # actually declares, converted to ASCII-JSON characters.  Asserting a
    # bare literal let the provider guard drift above the ResearchWorker
    # default (1800 tokens) without any test noticing.
    assert len(raw) < VISUAL_EDITOR_TOOL_RESULT_TOKENS * 4
    assert {
        item["section_id"] for item in payload["sections"]
    } == {f"S{index:02d}" for index in range(1, 9)}
    # Worst-case padding (very long titles and captions) still leaves the
    # full six-candidate shortlist intact for every section: the relaxation
    # is what lets the editor see enough source figures to place any.
    assert all(
        len(item["candidates"]) == 6
        for item in payload["sections"]
    )
    assert all(
        "local_image_path" not in candidate
        for item in payload["sections"]
        for candidate in item["candidates"]
    )


def test_visual_candidate_shortlist_default_matches_the_clamp_ceiling(
    tmp_path: Path,
    monkeypatch,
):
    """The no-argument call must not fall back to the old narrow shortlist.

    The model normally invokes this tool with no arguments, so the parameter
    default -- not the clamp ceiling -- is what decides real exposure.  A
    default below the ceiling silently reverts the widened envelope to the
    two-per-section shortlist that left the editor unable to place any
    source figure.
    """

    review_dir = tmp_path / "review"
    sections = []
    for index in range(1, 4):
        section_id = f"S{index:02d}"
        draft_dir = review_dir / "sections" / section_id
        draft_dir.mkdir(parents=True)
        (draft_dir / "SECTION_DRAFT_EN.md").write_text(
            "Short draft.", encoding="utf-8"
        )
        sections.append(
            {
                "section_id": section_id,
                "title": f"Section {index}",
                "argument_role": "role",
            }
        )
    provider = VisualEditorToolProvider(
        VisualEditorContext(
            blueprint={"sections": sections},
            review_work_dir=review_dir,
            work_dir=tmp_path / "visual",
        )
    )
    requested: list[int] = []

    def _fake_candidates(section_id: str, *, top_k: int):
        requested.append(top_k)
        return [
            {
                "chunk_id": f"{section_id}-v{i}",
                "paper_id": f"{section_id}-p{i}",
                "doi": f"10.1/{section_id.lower()}.{i}",
                "title": "T",
                "local_image_path": str(tmp_path / "figure.png"),
                "caption_preview": "c",
                "visual_argument_type": "mechanism_anchor",
                "score": 0.9,
            }
            for i in range(top_k)
        ]

    monkeypatch.setattr(
        provider, "_verified_candidates_for_section", _fake_candidates
    )
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    payload = json.loads(
        _tool_text(tools["inspect_article_visual_candidates"])
    )

    assert requested and max(requested) == 6, (
        "calling the tool with no arguments must request the full "
        "six-candidate shortlist"
    )
    # Small drafts leave room for the whole shortlist, so nothing is trimmed.
    assert payload["cache_safe"] is True
    assert all(len(item["candidates"]) == 6 for item in payload["sections"])


def test_saved_visual_plan_can_be_revalidated_without_model_call(
    tmp_path: Path,
):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"canonical-local-asset")
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    plan_path.write_text(
        json.dumps({
            "placements": [{
                "visual_chunk_id": "visual_1",
                "paper_id": "paper_1",
                "local_image_path": str(image_path),
                "status": "verified_existing",
            }],
            "conceptual_figure_requests": [{
                "status": "pending_generation_and_review",
                "required_disclosure": "AI-generated conceptual illustration",
            }],
        }),
        encoding="utf-8",
    )
    assert validate_visual_editorial_plan_file(plan_path).startswith(
        "VALIDATION_PASSED"
    )


def test_saved_visual_plan_rejects_stale_input_fingerprint(tmp_path: Path):
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    plan_path.write_text(
        json.dumps({
            "input_fingerprint": "old-input",
            "placements": [],
            "conceptual_figure_requests": [],
        }),
        encoding="utf-8",
    )
    result = validate_visual_editorial_plan_file(
        plan_path,
        "new-input",
    )
    assert result.startswith("VALIDATION_FAILED")
    assert "fingerprint is stale" in result


def test_visual_plan_accounts_for_every_declared_section_need(
    tmp_path: Path,
):
    context = VisualEditorContext(
        blueprint={
            "sections": [
                {
                    "section_id": "S01",
                    "title": "Mechanism",
                    "visual_argument_slots": [
                        {
                            "purpose": (
                                "Explain the causal mechanism without "
                                "inventing quantitative evidence."
                            )
                        }
                    ],
                },
                {
                    "section_id": "S02",
                    "title": "Benchmark",
                    "visual_argument_slots": [
                        {
                            "purpose": (
                                "Compare the benchmark dimensions using a "
                                "traceable scientific figure."
                            )
                        }
                    ],
                },
            ]
        },
        review_work_dir=tmp_path / "review",
        work_dir=tmp_path / "visual",
    )
    provider = VisualEditorToolProvider(context)
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    submitted = json.loads(
        _tool_text(
            tools["submit_visual_editorial_plan"],
            plan_json=json.dumps(
                {
                    "placements": [],
                    "conceptual_figure_requests": [
                        {
                            "section_id": "S01",
                            "figure_kind": "mechanism_schematic",
                            "argumentative_purpose": (
                                "Clarify the causal sequence used by the "
                                "mechanistic argument."
                            ),
                            "generation_brief": (
                                "Draw a non-quantitative mechanism schematic "
                                "with clearly labeled causal stages."
                            ),
                        }
                    ],
                    "unfilled_visual_needs": [],
                }
            ),
        )
    )
    assert submitted["status"] == "ok"
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    assert {
        item["section_id"]
        for item in plan["conceptual_figure_requests"]
    } == {"S01"}
    assert {
        item["section_id"] for item in plan["unfilled_visual_needs"]
    } == {"S02"}
    assert plan["unfilled_visual_needs"][0]["status"] == (
        "unfilled_requires_editorial_decision"
    )
    assert validate_visual_editorial_plan_file(
        provider.plan_path,
        expected_visual_section_ids={"S01", "S02"},
    ).startswith("VALIDATION_PASSED")


def test_visual_plan_validator_rejects_silently_omitted_need(
    tmp_path: Path,
):
    plan_path = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    plan_path.write_text(
        json.dumps(
            {
                "placements": [],
                "conceptual_figure_requests": [],
                "unfilled_visual_needs": [],
            }
        ),
        encoding="utf-8",
    )
    result = validate_visual_editorial_plan_file(
        plan_path,
        expected_visual_section_ids={"S01"},
    )
    assert result.startswith("VALIDATION_FAILED")
    assert "silently omitted" in result


def test_visual_editor_fingerprint_tracks_draft_prompt_and_kb(tmp_path: Path):
    review_dir = tmp_path / "review"
    section_dir = review_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    draft = section_dir / "SECTION_DRAFT_EN.md"
    draft.write_text("Initial mechanism discussion.", encoding="utf-8")
    kb_path = tmp_path / "knowledge.sqlite"
    kb_path.write_bytes(b"kb-v1")
    blueprint = {"sections": [{"section_id": "S01"}]}

    original = visual_editor_input_fingerprint(
        blueprint=blueprint,
        review_work_dir=review_dir,
        kb_sqlite_paths=[kb_path],
        role_prompt="Select traceable visuals.",
    )
    draft.write_text("Revised mechanism discussion.", encoding="utf-8")
    revised = visual_editor_input_fingerprint(
        blueprint=blueprint,
        review_work_dir=review_dir,
        kb_sqlite_paths=[kb_path],
        role_prompt="Select traceable visuals.",
    )
    reprompted = visual_editor_input_fingerprint(
        blueprint=blueprint,
        review_work_dir=review_dir,
        kb_sqlite_paths=[kb_path],
        role_prompt="Select only directly useful traceable visuals.",
    )
    assert original != revised
    assert revised != reprompted


def test_visual_editor_recovers_only_prevalidated_items(tmp_path: Path):
    """One bad placement must not discard the article's verified figures."""

    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"traceable-image")
    context = VisualEditorContext(
        blueprint={
            "sections": [
                {"section_id": "S01", "title": "Mechanism"},
                {"section_id": "S02", "title": "Application"},
            ]
        },
        review_work_dir=tmp_path / "review",
        work_dir=tmp_path / "visual",
    )
    provider = VisualEditorToolProvider(context)
    provider._inspected = {
        "S01": {
            "visual_ok": {
                "chunk_id": "visual_ok",
                "paper_id": "paper_ok",
                "doi": "10.1/ok",
                "local_image_path": str(image_path),
                "visual_argument_type": "mechanism_anchor",
                "caption_preview": "A traceable physical mechanism.",
            }
        }
    }
    tools = {tool.name: tool for tool in provider.get_tools(tmp_path)}
    submitted = json.loads(
        _tool_text(
            tools["submit_visual_editorial_plan"],
            plan_json=json.dumps(
                {
                    "placements": [
                        {
                            "section_id": "S01",
                            "visual_chunk_id": "visual_ok",
                            "argumentative_purpose": (
                                "This figure anchors the physical mechanism "
                                "discussed in the surrounding prose."
                            ),
                            "placement_guidance": "Place after the mechanism paragraph.",
                        },
                        {
                            "section_id": "S02",
                            "visual_chunk_id": "visual_ok",
                            "argumentative_purpose": (
                                "This invalid cross-section reuse must be rejected."
                            ),
                        },
                    ],
                    "conceptual_figure_requests": [],
                    "unfilled_visual_needs": [],
                }
            ),
        )
    )
    assert submitted["status"] == "error"
    assert submitted["safe_partial_saved"] is True
    recovered = provider.finalize_safe_partial_plan()
    assert recovered["recovered"] is True
    plan = json.loads(provider.plan_path.read_text(encoding="utf-8"))
    assert [item["visual_chunk_id"] for item in plan["placements"]] == [
        "visual_ok"
    ]
    assert plan["recovery"]["human_review_recommended"] is True
