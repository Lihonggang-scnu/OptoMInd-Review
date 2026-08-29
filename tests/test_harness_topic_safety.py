"""Fail-closed and cross-topic safety tests for the reusable Research Harness."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from optomind_research.runtime.full_review_orchestrator import (
    SectionMaterialBundle,
)
from optomind_research.runtime.harness_observability import (
    HarnessObservability,
)
from optomind_research.runtime.review_content_evaluator import (
    evaluate_review_content,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.review_lead_tool_provider import _kb_overview
from optomind_research.runtime.topic_identity import (
    anchor_retrieval_query,
    assess_blueprint_topic_alignment,
    assess_retrieval_query_alignment,
    assess_topic_alignment,
    build_topic_identity_contract,
)
from run_review_harness import _prepare_query_plan


def _metalens_plan() -> dict:
    return {
        "input": {
            "user_query": (
                "Achromatic metalenses for augmented-reality near-eye displays"
            )
        },
        "output": {
            "problem_understanding": (
                "Review achromatic metalenses for augmented-reality near-eye "
                "displays across bandwidth, efficiency, field of view, "
                "fabrication tolerance, and large-area integration."
            ),
            "scope_definition": {
                "main_scope": (
                    "Achromatic metalens architectures and near-eye display "
                    "integration."
                ),
                "scope_items": [
                    "Dispersion engineering",
                    "Broadband metasurface lenses",
                    "Near-eye display constraints",
                ],
            },
            "keyword_decomposition": {
                "keywords": [
                    "achromatic metalens augmented reality",
                    "broadband achromatic metalens",
                    "near-eye display metasurface lens",
                    "metalens field of view",
                    "large area metalens fabrication",
                ]
            },
            "extra_notes": "",
        },
    }


def _fallback_package() -> dict:
    fallback = {
        "input": {
            "user_query": (
                "Research question requiring English normalization during "
                "human review"
            )
        },
        "output": {
            "problem_understanding": (
                "Reformulate the user's research question into an English "
                "scholarly search target."
            ),
            "scope_definition": {
                "main_scope": "General optical literature scope.",
                "scope_items": ["General background"],
            },
            "keyword_decomposition": {
                "keywords": ["optical thin film", "multilayer optical coating"]
            },
            "extra_notes": "",
        },
    }
    return {
        "status": "deterministic_fallback_after_repair_failed",
        "needs_human_confirmation": True,
        "result": fallback,
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }


def test_deterministic_query_fallback_never_enters_downstream(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, question: _fallback_package(),
    )
    observer = HarnessObservability(tmp_path, "fallback-run")
    observer.start_run(entry_mode="test", resumed=False)
    plan_path, metrics = _prepare_query_plan(
        question="任意新的光学问题",
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path is None
    assert metrics["execution_ready"] is False
    assert metrics["terminal_status"] == "needs_model_recovery"
    package = json.loads(
        (tmp_path / "REVIEW_CONTENT_PACKAGE.json").read_text(
            encoding="utf-8"
        )
    )
    assert package["status"] == "needs_model_recovery"
    assert not (tmp_path / "review_lead").exists()


def test_valid_model_query_plan_creates_topic_contract(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    package = {
        "status": "primary_valid",
        "needs_human_confirmation": True,
        "result": _metalens_plan(),
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, question: package,
    )
    observer = HarnessObservability(tmp_path, "valid-run")
    observer.start_run(entry_mode="test", resumed=False)
    plan_path, metrics = _prepare_query_plan(
        question="一个新的光学问题",
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path is not None
    assert metrics["execution_ready"] is True
    contract = json.loads(
        (tmp_path / "TOPIC_IDENTITY.json").read_text(encoding="utf-8")
    )
    assert contract["valid"] is True
    assert "metalens" in contract["core_anchor_tokens"]


def test_optional_notes_salvage_status_is_execution_ready(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    package = {
        "status": "primary_valid_optional_notes_dropped",
        "needs_human_confirmation": True,
        "result": _metalens_plan(),
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, question: package,
    )
    observer = HarnessObservability(tmp_path, "notes-salvage-run")
    observer.start_run(entry_mode="test", resumed=False)
    plan_path, metrics = _prepare_query_plan(
        question="一个新的光学问题",
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path is not None
    assert metrics["execution_ready"] is True


def test_same_confirmed_question_reuses_query_plan_without_model(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    question = "一个新的光学问题"
    query_dir = tmp_path / "query_planner"
    query_dir.mkdir(parents=True)
    (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps({"user_question": question}, ensure_ascii=False),
        encoding="utf-8",
    )
    (query_dir / "query_plan.json").write_text(
        json.dumps(_metalens_plan()),
        encoding="utf-8",
    )
    (tmp_path / "QUERY_PLAN_ENTRY_GATE.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "execution_ready": True,
                "auto_confirmation_requested": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, raw: (_ for _ in ()).throw(
            AssertionError("cached plan should avoid a model call")
        ),
    )
    observer = HarnessObservability(tmp_path, "resume-run")
    observer.start_run(entry_mode="test", resumed=True)
    plan_path, metrics = _prepare_query_plan(
        question=question,
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path == query_dir / "query_plan.json"
    assert metrics["reused"] is True
    assert metrics["cost_cny"] == 0.0


def test_auto_confirm_resume_reuses_plan_from_manual_confirmation_stop(
    monkeypatch,
    tmp_path: Path,
):
    """Current --auto-confirm-query-plan is the confirmation itself."""

    from optomind_research import query_planner as module

    question = "Achromatic metalenses for augmented-reality near-eye displays"
    query_dir = tmp_path / "query_planner"
    query_dir.mkdir(parents=True)
    (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps({"user_question": question}),
        encoding="utf-8",
    )
    (query_dir / "query_plan.json").write_text(
        json.dumps(_metalens_plan()),
        encoding="utf-8",
    )
    (tmp_path / "QUERY_PLAN_ENTRY_GATE.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "execution_ready": True,
                "auto_confirmation_requested": False,
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def never_call_model(self, raw):
        calls.append(raw)
        raise AssertionError("cached plan should avoid a model call")

    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        never_call_model,
    )
    observer = HarnessObservability(tmp_path, "manual-then-auto-resume")
    observer.start_run(entry_mode="test", resumed=True)
    plan_path, metrics = _prepare_query_plan(
        question=question,
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path == query_dir / "query_plan.json"
    assert metrics["reused"] is True
    assert metrics["status"] == "reused_confirmed_query_plan"
    assert metrics["cost_cny"] == 0.0
    assert calls == []


def test_resume_cache_requires_exact_question_match(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    incoming_question = "A different incoming research question"
    query_dir = tmp_path / "query_planner"
    query_dir.mkdir(parents=True)
    (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps(
            {"user_question": "The previously cached research question"}
        ),
        encoding="utf-8",
    )
    (query_dir / "query_plan.json").write_text(
        json.dumps(_metalens_plan()),
        encoding="utf-8",
    )
    (tmp_path / "QUERY_PLAN_ENTRY_GATE.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "execution_ready": True,
                "auto_confirmation_requested": False,
            }
        ),
        encoding="utf-8",
    )
    package = {
        "status": "primary_valid",
        "needs_human_confirmation": True,
        "result": _metalens_plan(),
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    calls = []
    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, raw: calls.append(raw) or package,
    )
    observer = HarnessObservability(tmp_path, "question-mismatch-run")
    observer.start_run(entry_mode="test", resumed=True)
    plan_path, metrics = _prepare_query_plan(
        question=incoming_question,
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path is not None
    assert calls == [incoming_question]
    assert "reused" not in metrics
    saved = json.loads(
        (query_dir / "ORIGINAL_USER_QUESTION.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["user_question"] == incoming_question


@pytest.mark.parametrize(
    "gate, cached_plan",
    [
        (
            {"status": "passed", "execution_ready": False},
            _metalens_plan(),
        ),
        (
            {"status": "failed", "execution_ready": True},
            _metalens_plan(),
        ),
        ({"status": "passed", "execution_ready": True}, {}),
    ],
)
def test_resume_cache_fails_closed_when_gate_or_plan_is_invalid(
    monkeypatch,
    tmp_path: Path,
    gate,
    cached_plan,
):
    from optomind_research import query_planner as module

    question = "Achromatic metalenses for augmented-reality near-eye displays"
    query_dir = tmp_path / "query_planner"
    query_dir.mkdir(parents=True)
    (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
        json.dumps({"user_question": question}),
        encoding="utf-8",
    )
    (query_dir / "query_plan.json").write_text(
        json.dumps(cached_plan),
        encoding="utf-8",
    )
    (tmp_path / "QUERY_PLAN_ENTRY_GATE.json").write_text(
        json.dumps(gate),
        encoding="utf-8",
    )
    package = {
        "status": "primary_valid",
        "needs_human_confirmation": True,
        "result": _metalens_plan(),
        "final_validation": {"ok": True, "errors": [], "warnings": []},
    }
    calls = []
    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        lambda self, raw: calls.append(raw) or package,
    )
    observer = HarnessObservability(tmp_path, "invalid-cache-run")
    observer.start_run(entry_mode="test", resumed=True)
    plan_path, metrics = _prepare_query_plan(
        question=question,
        query_plan_path=None,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=True,
        observability=observer,
    )
    assert plan_path is not None
    assert calls == [question]
    assert metrics["planner_generation_status"] == "primary_valid"
    assert "reused" not in metrics


def test_provided_query_plan_path_bypasses_cache_and_model(
    monkeypatch,
    tmp_path: Path,
):
    from optomind_research import query_planner as module

    plan_path = tmp_path / "human-confirmed-plan.json"
    plan_path.write_text(json.dumps(_metalens_plan()), encoding="utf-8")
    calls = []

    def never_call_model(self, raw):
        calls.append(raw)
        raise AssertionError("provided plan must not call QueryPlannerAgent")

    monkeypatch.setattr(
        module.QueryPlannerAgent,
        "plan_review_dict",
        never_call_model,
    )
    observer = HarnessObservability(tmp_path, "provided-plan-run")
    observer.start_run(entry_mode="test", resumed=False)
    returned_path, metrics = _prepare_query_plan(
        question="Ignored because a plan was provided",
        query_plan_path=plan_path,
        work_dir=tmp_path,
        mock=False,
        auto_confirm=False,
        observability=observer,
    )
    assert returned_path == plan_path
    assert metrics["status"] == "provided_confirmed_plan"
    assert metrics["cost_cny"] == 0.0
    assert calls == []


def test_fallback_placeholder_contract_is_invalid():
    contract = build_topic_identity_contract(
        _fallback_package()["result"]
    )
    assert contract["valid"] is False
    assert contract["placeholder_markers"]


def test_kb_overview_hides_unrelated_historical_concepts(tmp_path: Path):
    kb = tmp_path / "kb.sqlite"
    conn = sqlite3.connect(kb)
    conn.execute("CREATE TABLE concepts(label TEXT)")
    conn.executemany(
        "INSERT INTO concepts(label) VALUES (?)",
        [
            ("passive daytime radiative cooling",),
            ("atmospheric transparency window",),
            ("broadband achromatic metalens",),
            ("metalens dispersion engineering",),
        ],
    )
    conn.commit()
    conn.close()
    overview = _kb_overview(kb, build_topic_identity_contract(_metalens_plan()))
    serialized = json.dumps(overview["top_concepts"]).lower()
    assert "metalens" in serialized
    assert "radiative cooling" not in serialized


def test_topic_alignment_rejects_cross_topic_review():
    contract = build_topic_identity_contract(_metalens_plan())
    result = assess_topic_alignment(
        "Passive daytime radiative cooling uses the atmospheric window.",
        contract,
    )
    assert result["status"] == "failed"


def test_generic_section_query_is_anchored_to_scientific_object():
    contract = build_topic_identity_contract(_metalens_plan())
    corrected, audit = anchor_retrieval_query(
        "manufacturing-aware inverse design process models",
        contract,
    )
    assert audit["changed"] is True
    assert (
        assess_retrieval_query_alignment(corrected, contract)["status"]
        == "passed"
    )
    assert "metalens" in corrected.lower()


def test_blueprint_gate_checks_each_section_not_only_review_wide():
    contract = build_topic_identity_contract(_metalens_plan())
    blueprint = {
        "review_thesis": (
            "Achromatic metalenses can support broadband near-eye displays."
        ),
        "full_review_argument": (
            "The review connects metalens dispersion engineering to augmented "
            "reality integration."
        ),
        "taxonomy_principle": "Organize achromatic metalens mechanisms.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Achromatic metalens foundations",
                "chapter_argument": (
                    "Explain broadband metalens dispersion mechanisms."
                ),
                "key_questions": ["How is metalens dispersion controlled?"],
                "synthesis_task": (
                    "Compare achromatic metalens physical mechanisms."
                ),
            },
            {
                "section_id": "S02",
                "title": "Future integrated co-design frameworks",
                "chapter_argument": (
                    "Integrate generic process models into optimisation."
                ),
                "key_questions": ["How can manufacturing scale?"],
                "synthesis_task": "Compare generic optimisation workflows.",
            },
        ],
    }
    result = assess_blueprint_topic_alignment(blueprint, contract)
    assert result["status"] == "failed"
    assert result["failed_section_ids"] == ["S02"]


def test_invalidated_stage_is_archived_and_prior_cost_preserved(
    tmp_path: Path,
):
    plan_path = tmp_path / "query.json"
    plan_path.write_text(json.dumps(_metalens_plan()), encoding="utf-8")
    kb = tmp_path / "kb.sqlite"
    sqlite3.connect(kb).close()
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=plan_path,
            base_kb_sqlite=kb,
            output_root=tmp_path,
        ),
        run_dir=tmp_path / "run",
    )
    stage_dir = harness.work_dir / "review_lead"
    stage_dir.mkdir(parents=True)
    (stage_dir / "RESULT.json").write_text("{}", encoding="utf-8")
    harness.stage_costs["review_lead"] = {
        "estimated_cost_cny": 0.5,
        "input_tokens": 10,
        "output_tokens": 2,
    }
    archive = harness._archive_invalid_stage(
        "review_lead",
        stage_dir,
        reason="test",
    )
    assert (archive / "RESULT.json").exists()
    assert (archive / "INVALIDATION.json").exists()
    assert any(
        key.startswith("review_lead_invalidated_attempt_")
        for key in harness.stage_costs
    )


def test_section_role_plan_persists_topic_anchored_queries(tmp_path: Path):
    from optomind_research.runtime.section_coverage_tool_registry import (
        COVERAGE_ROLES,
        _make_query_review_knowledge_base,
        _make_submit_literature_role_plan,
    )
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    contract = build_topic_identity_contract(_metalens_plan())
    ctx = SectionCoverageContext(
        section_id="S08",
        section_data={
            "title": "Integrated co-design frameworks",
            "chapter_argument": (
                "Integrate manufacturing constraints into inverse design."
            ),
            "required_roles": ["frontier"],
            "optional_roles": [],
            "topic_identity": contract,
        },
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "staging.sqlite",
        work_dir=tmp_path,
    )
    before_plan = json.loads(
        _make_query_review_knowledge_base(ctx)(
            "manufacturing-aware inverse design",
            role="frontier",
        )
    )
    assert before_plan["error_code"] == "coverage_plan_required"

    raw_plan = {
        role: {
            "priority": "required" if role == "frontier" else "not_needed",
            "coverage_question": f"Question for {role}",
            "intended_synthesis": f"Synthesis for {role}",
            "queries": (
                ["manufacturing-aware inverse design process models"]
                if role == "frontier"
                else []
            ),
        }
        for role in COVERAGE_ROLES
    }
    submitted = json.loads(
        _make_submit_literature_role_plan(ctx)(json.dumps(raw_plan))
    )
    assert submitted["status"] == "ok"
    assert submitted["query_topic_corrections"]
    artifact = json.loads(
        (tmp_path / "SECTION_COVERAGE_PLAN.json").read_text(
            encoding="utf-8"
        )
    )
    query = artifact["roles"]["frontier"]["queries"][0]
    assert (
        assess_retrieval_query_alignment(query, contract)["status"]
        == "passed"
    )


def test_coverage_gate_rejects_off_topic_material_packages(tmp_path: Path):
    query_plan_path = tmp_path / "query_plan.json"
    query_plan_path.write_text(
        json.dumps(_metalens_plan()),
        encoding="utf-8",
    )
    kb = tmp_path / "base.sqlite"
    sqlite3.connect(kb).close()
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan_path,
            base_kb_sqlite=kb,
            output_root=tmp_path,
        ),
        run_dir=tmp_path / "run",
    )
    package = tmp_path / "SECTION_MATERIAL_PACKAGE.json"
    ledger = tmp_path / "SOURCE_LEDGER.json"
    package.write_text(
        json.dumps(
            {
                "section_id": "S01",
                "section_title": "Atmospheric-window radiative cooling",
                "chapter_argument": "Explain thermal emission to deep space.",
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "title": "Passive daytime radiative cooling",
                        "abstract": "Selective thermal emission below ambient.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = harness._coverage_topic_alignment(
        material_bundles={
            "S01": SectionMaterialBundle(
                material_package_path=package,
                source_ledger_path=ledger,
                kb_sqlite=kb,
            )
        },
        topic_identity=build_topic_identity_contract(_metalens_plan()),
    )
    assert result["status"] == "failed"
    assert result["failed_section_ids"] == ["S01"]


def test_coverage_gate_defers_empty_specialist_sections_without_false_drift(
    tmp_path: Path,
):
    query_plan_path = tmp_path / "query_plan.json"
    query_plan_path.write_text(json.dumps(_metalens_plan()), encoding="utf-8")
    kb = tmp_path / "base.sqlite"
    sqlite3.connect(kb).close()
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan_path,
            base_kb_sqlite=kb,
            output_root=tmp_path,
        ),
        run_dir=tmp_path / "run",
    )

    def bundle(
        section_id: str,
        title: str,
        argument: str,
        sources: list[dict],
    ) -> SectionMaterialBundle:
        section_dir = tmp_path / section_id
        section_dir.mkdir()
        package = section_dir / "SECTION_MATERIAL_PACKAGE.json"
        ledger = section_dir / "SECTION_SOURCE_LEDGER.json"
        package.write_text(
            json.dumps(
                {
                    "section_id": section_id,
                    "section_title": title,
                    "chapter_argument": argument,
                    "total_sources": len(sources),
                    "unique_sources": len(sources),
                    "direct_sources": len(sources),
                    "coverage_outcome": (
                        "material_ready_with_limits"
                        if sources
                        else "needs_more_literature"
                    ),
                }
            ),
            encoding="utf-8",
        )
        ledger.write_text(
            json.dumps({"section_id": section_id, "sources": sources}),
            encoding="utf-8",
        )
        return SectionMaterialBundle(package, ledger, kb)

    result = harness._coverage_topic_alignment(
        material_bundles={
            "S01": bundle(
                "S01",
                "Physical foundations of metasurface phase control",
                "Explain phase engineering for metalens imaging and flat optics.",
                [{"title": "Metasurface phase control for metalens imaging"}],
            ),
            "S03": bundle(
                "S03",
                "Metasurface nanofabrication and manufacturing throughput",
                "Relate fabrication fidelity to deployable flat optics.",
                [],
            ),
            "S06": bundle(
                "S06",
                "Dynamic and reconfigurable metasurfaces",
                "Compare tunability mechanisms for adaptive optics.",
                [],
            ),
        },
        topic_identity=build_topic_identity_contract(_metalens_plan()),
    )

    assert result["status"] == "passed"
    assert result["failed_section_ids"] == []
    assert result["assessable_section_ids"] == ["S01"]
    assert result["deferred_section_ids"] == ["S03", "S06"]
    assert result["section_results"]["S03"]["status"] == "not_assessed"


def test_coverage_gate_accepts_specialist_evidence_via_section_anchor(
    tmp_path: Path,
):
    query_plan_path = tmp_path / "query_plan.json"
    query_plan_path.write_text(json.dumps(_metalens_plan()), encoding="utf-8")
    kb = tmp_path / "base.sqlite"
    sqlite3.connect(kb).close()
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan_path,
            base_kb_sqlite=kb,
            output_root=tmp_path,
        ),
        run_dir=tmp_path / "run",
    )
    package = tmp_path / "SECTION_MATERIAL_PACKAGE.json"
    ledger = tmp_path / "SECTION_SOURCE_LEDGER.json"
    package.write_text(
        json.dumps(
            {
                "section_id": "S03",
                "section_title": "Metalens nanofabrication and throughput",
                "chapter_argument": (
                    "Nanoimprint fidelity governs scalable metalens manufacture."
                ),
                "total_sources": 1,
                "unique_sources": 1,
                "direct_sources": 1,
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text(
        json.dumps(
            {
                "section_id": "S03",
                "sources": [
                    {
                        "title": (
                            "High-throughput nanoimprint manufacturing of "
                            "large-area metalenses"
                        )
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = harness._coverage_topic_alignment(
        material_bundles={"S03": SectionMaterialBundle(package, ledger, kb)},
        topic_identity=build_topic_identity_contract(_metalens_plan()),
    )

    assert result["status"] == "passed"
    section = result["section_results"]["S03"]
    assert section["evidence_core_hits"]
    assert section["section_specific_hits"]


def test_coverage_gate_rejects_off_topic_evidence_inside_valid_frame(
    tmp_path: Path,
):
    query_plan_path = tmp_path / "query_plan.json"
    query_plan_path.write_text(json.dumps(_metalens_plan()), encoding="utf-8")
    kb = tmp_path / "base.sqlite"
    sqlite3.connect(kb).close()
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan_path,
            base_kb_sqlite=kb,
            output_root=tmp_path,
        ),
        run_dir=tmp_path / "run",
    )
    package = tmp_path / "SECTION_MATERIAL_PACKAGE.json"
    ledger = tmp_path / "SECTION_SOURCE_LEDGER.json"
    package.write_text(
        json.dumps(
            {
                "section_id": "S03",
                "section_title": "Metasurface nanofabrication and throughput",
                "chapter_argument": "Scale manufacturing for flat optics.",
                "total_sources": 1,
                "unique_sources": 1,
                "direct_sources": 1,
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text(
        json.dumps(
            {
                "section_id": "S03",
                "sources": [
                    {
                        "title": "Passive daytime radiative cooling for buildings",
                        "abstract": "Thermal emission through the atmospheric window.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = harness._coverage_topic_alignment(
        material_bundles={"S03": SectionMaterialBundle(package, ledger, kb)},
        topic_identity=build_topic_identity_contract(_metalens_plan()),
    )

    assert result["status"] == "failed"
    assert result["failed_section_ids"] == ["S03"]


def test_final_quality_gate_blocks_cross_topic_manuscript(tmp_path: Path):
    contract = build_topic_identity_contract(_metalens_plan())
    blueprint = {
        "topic_identity": contract,
        "sections": [
            {
                "section_id": "S01",
                "title": "Achromatic metalens foundations",
                "target_word_range": {"min": 100, "max": 300},
            }
        ],
    }
    review = tmp_path / "FINAL_REVIEW_EN.md"
    review.write_text(
        "## Achromatic metalens foundations\n\n"
        + " ".join(
            [
                (
                    "Passive daytime radiative cooling uses selective thermal "
                    "emission through the atmospheric window."
                )
            ]
            * 20
        ),
        encoding="utf-8",
    )
    report = evaluate_review_content(
        final_review_path=review,
        blueprint=blueprint,
        visual_plan_path=None,
        citation_map_path=None,
        output_dir=tmp_path,
    )
    assert report["status"] == "failed"
    assert "review_topic_identity_mismatch" in report["blocking_issues"]


def test_visual_plan_topic_gate_reads_values_not_only_json_keys(
    tmp_path: Path,
):
    contract = build_topic_identity_contract(_metalens_plan())
    blueprint = {
        "topic_identity": contract,
        "sections": [{
            "section_id": "S01",
            "title": "Achromatic metalens foundations",
            "target_word_range": {"min": 80, "max": 200},
        }],
    }
    review = tmp_path / "FINAL_REVIEW_EN.md"
    review.write_text(
        "## Achromatic metalens foundations\n\n"
        + " ".join([
            (
                "Achromatic metalens design coordinates dispersion control, "
                "phase engineering, nanostructure response, and broadband focusing."
            )
        ] * 12),
        encoding="utf-8",
    )
    visual = tmp_path / "VISUAL_EDITORIAL_PLAN.json"
    visual.write_text(
        json.dumps({
            "placements": [],
            "conceptual_figure_requests": [{
                "section_id": "S01",
                "description": " ".join([
                    (
                        "An achromatic metalens diagram should compare phase "
                        "dispersion, group delay, nanostructure geometry, "
                        "broadband focusing, and chromatic aberration control."
                    )
                ] * 8),
            }],
        }),
        encoding="utf-8",
    )
    report = evaluate_review_content(
        final_review_path=review,
        blueprint=blueprint,
        visual_plan_path=visual,
        citation_map_path=None,
        output_dir=tmp_path,
    )
    assert report["metrics"]["visual_plan_topic_alignment"][
        "status"
    ] == "passed"
    assert "visual_plan_topic_identity_mismatch" not in report[
        "blocking_issues"
    ]
