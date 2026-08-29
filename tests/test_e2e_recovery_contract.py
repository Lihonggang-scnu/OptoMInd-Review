from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfWriter

from optomind_research.runtime.coverage_ledger import (
    get_audit,
    get_query,
    record_audit,
    record_query,
)
from optomind_research.runtime.delivery_contract import build_delivery_gate
from optomind_research.runtime.scientific_chinese_translator import (
    ScientificChineseTranslator,
)


def _write_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def test_delivery_gate_requires_four_readable_pdfs_and_audits(tmp_path: Path) -> None:
    paths = {}
    for key in ("review", "review_zh", "plan", "plan_zh"):
        path = tmp_path / f"{key}.pdf"
        _write_pdf(path)
        paths[key] = str(path)
    plan_dir = tmp_path / "research_program"
    plan_dir.mkdir()
    (plan_dir / "RESEARCH_PLAN_AUDIT.json").write_text(
        json.dumps({"status": "passed"}), encoding="utf-8"
    )
    package = {
        "latex_pdf_path": paths["review"],
        "chinese_latex_pdf_path": paths["review_zh"],
        "research_plan_latex_pdf_path": paths["plan"],
        "research_plan_chinese_latex_pdf_path": paths["plan_zh"],
        "artifacts": {},
    }
    gate = build_delivery_gate(
        work_dir=tmp_path,
        package=package,
        quality_report={"status": "passed"},
        latex_report={"status": "compiled_awaiting_metadata"},
        chinese_translation_report={"status": "completed"},
        chinese_latex_report={"status": "compiled_awaiting_metadata"},
        research_plan_publication_report={"status": "completed"},
        require_review=True,
        require_chinese_review=True,
        require_research_plan=True,
    )
    assert gate["passed"] is True
    paths["plan_zh"] and Path(paths["plan_zh"]).unlink()
    failed = build_delivery_gate(
        work_dir=tmp_path,
        package=package,
        quality_report={"status": "passed"},
        latex_report={"status": "compiled_awaiting_metadata"},
        chinese_translation_report={"status": "completed"},
        chinese_latex_report={"status": "compiled_awaiting_metadata"},
        research_plan_publication_report={"status": "completed"},
        require_review=True,
        require_chinese_review=True,
        require_research_plan=True,
    )
    assert failed["passed"] is False
    assert "chinese_research_plan_pdf" in failed["blocking_checks"]


def test_translation_single_unit_failure_resumes_without_retransmitting_successes(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "review.md"
    source.write_text(
        "First scientific paragraph with a protected equation $Q=10^6$.\n\n"
        "Second scientific paragraph [REF:paper_B].",
        encoding="utf-8",
    )
    calls: list[tuple[str, list[str]]] = []

    def fake_translate_batch(self, batch, *, tier):
        calls.append((tier, [unit.unit_id for unit in batch]))
        if tier == "c2_model" and any(unit.unit_id == "B0002" for unit in batch):
            raise RuntimeError("B0068 simulated unit failure")
        for unit in batch:
            unit.translated = (
                "这是中文译文，公式为 $Q=10^6$。"
                if unit.unit_id == "B0001"
                else "这是中文译文，并保留引用 [REF:paper_B]。"
            )
            unit.status = "validated"
            unit.model_tier = tier
            unit.model_name = tier
        return []

    monkeypatch.setattr(
        ScientificChineseTranslator,
        "_translate_batch",
        fake_translate_batch,
    )
    output_dir = tmp_path / "translation"
    translator = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=output_dir,
        semantic_audit=False,
        max_batch_items=2,
        max_batch_chars=10000,
        cost_budget_cny=10.0,
    )
    first = translator.translate()
    assert first["status"] == "completed"
    assert first["failed_unit_ids"] == []
    assert any(unit["unit_id"] == "B0002" and unit["status"] == "validated" for unit in first["units"])
    assert len(calls) == 4
    state = json.loads((output_dir / "TRANSLATION_STATE.json").read_text(encoding="utf-8"))
    assert all(row["status"] == "validated" for row in state["records"].values())

    calls.clear()
    second = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=output_dir,
        semantic_audit=False,
        max_batch_items=2,
        max_batch_chars=10000,
        cost_budget_cny=10.0,
    ).translate()
    assert second["status"] == "completed"
    assert second["cache_hit_count"] == 2
    assert calls == []


def test_coverage_ledger_reuses_query_and_audit_by_topic_role_material(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "COVERAGE_GLOBAL_LEDGER.json"
    record_query(
        ledger_path,
        topic_fingerprint="topic-1",
        role="mechanism",
        query="radiative cooling mechanism",
        candidates=[{"title": "A", "doi": "10.1/a", "abstract": "body"}],
    )
    assert get_query(ledger_path, "topic-1", "mechanism", "radiative cooling mechanism")
    record_audit(
        ledger_path,
        topic_fingerprint="topic-1",
        role="mechanism",
        identity="doi:10.1/a",
        decision="approved",
        scope_fit="direct",
        role_fit=["mechanism"],
        audit_reason="Direct mechanism discussion.",
        not_usable_for=[],
    )
    audit = get_audit(ledger_path, "topic-1", "mechanism", "doi:10.1/a")
    assert audit and audit["decision"] == "approved"
    assert audit["scope_fit"] == "direct"


def test_r5_auto_continues_internal_discovery_checkpoints(
    tmp_path: Path, monkeypatch
) -> None:
    from optomind_research.runtime import research_program_runner as module
    from optomind_research.runtime.research_program_runner import ResearchProgramContext
    from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

    blueprint = tmp_path / "blueprint.json"
    review = tmp_path / "review.md"
    blueprint.write_text(json.dumps({"sections": []}), encoding="utf-8")
    review.write_text("A bounded review.", encoding="utf-8")
    context = ResearchProgramContext(
        blueprint_path=blueprint,
        final_review_path=review,
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path / "research_program",
    )
    stages = iter(["opportunity", "hypothesis", "hypothesis", "focus", "focus"])
    monkeypatch.setattr(module, "_determine_r5_discovery_stage", lambda provider: next(stages))
    monkeypatch.setattr(module, "_run_r5_plan_completion_validation", lambda provider: "VALIDATION_PASSED")
    calls: list[str] = []
    monkeypatch.setattr(module, "_focus_gate_is_passed", lambda work_dir: len(calls) >= 3)

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def run(self, contract):
            calls.append(str(contract.metadata.get("r5_discovery_stage") or contract.metadata.get("phase_identity")))
            return ResultManifest(
                run_id=contract.run_id,
                task_id=contract.task_id,
                status=TaskStatus.completed,
                stop_reason="offline_stage_complete",
            )

    monkeypatch.setattr(module, "ResearchWorker", FakeWorker)
    result = module.run_research_program(
        context,
        run_id="r5_auto_resume",
        cost_budget_cny=5.0,
        token_budget=100_000,
        max_iters=5,
        auto_continue_discovery=True,
    )
    assert result.status == TaskStatus.completed
    assert len(calls) == 4


def _harness_publication_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    query_plan = tmp_path / "query.json"
    query_plan.write_text(json.dumps({"output": {}}), encoding="utf-8")
    kb_path = tmp_path / "kb.sqlite"
    kb_path.write_bytes(b"")
    run_dir = tmp_path / "run"
    review = run_dir / "authoring" / "FINAL_REVIEW_EN.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("# Review\n\nA complete review draft.", encoding="utf-8")
    visual = run_dir / "visual" / "VISUAL_PLAN.json"
    visual.parent.mkdir(parents=True, exist_ok=True)
    visual.write_text(json.dumps({"placements": []}), encoding="utf-8")
    plan_dir = run_dir / "research_program"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "RESEARCH_PLAN.md"
    plan.write_text("# Research plan\n\nA complete plan draft.", encoding="utf-8")
    (plan_dir / "RESEARCH_PLAN_AUDIT.json").write_text(
        json.dumps({"status": "passed"}), encoding="utf-8"
    )
    return query_plan, kb_path, run_dir, plan


def _patch_harness_publication_dependencies(monkeypatch, module, *, quality_status="passed"):
    monkeypatch.setattr(
        module,
        "evaluate_review_content",
        lambda **_: {"status": quality_status, "metrics": {}},
    )

    def fake_translation(**kwargs):
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        translated = output_dir / "FINAL_REVIEW_ZH.md"
        translated.write_text("# 中文综述\n\n完整中文内容。", encoding="utf-8")
        metadata = output_dir / "PUBLICATION_METADATA_ZH.json"
        metadata.write_text(json.dumps({"title": "中文综述"}), encoding="utf-8")
        return {
            "status": "completed",
            "translated_path": str(translated),
            "translated_metadata_path": str(metadata),
            "cumulative_input_tokens": 10,
            "cumulative_output_tokens": 10,
            "cumulative_estimated_cost_cny": 0.01,
            "failed_unit_ids": [],
        }

    def fake_latex(**kwargs):
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf = output_dir / "article.pdf"
        _write_pdf(pdf)
        return {
            "status": "submission_ready",
            "artifacts": {"compiled_pdf": str(pdf)},
        }

    def fake_plan_publication(**kwargs):
        output_dir = Path(str(kwargs["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        en = output_dir / "latex_en" / "plan.pdf"
        zh = output_dir / "latex_zh" / "plan.pdf"
        _write_pdf(en)
        _write_pdf(zh)
        report = {
            "status": "completed",
            "verification_status": "verification_deferred",
            "english": {"status": "submission_ready"},
            "translation": {"status": "completed"},
            "chinese": {"status": "submission_ready"},
            "artifacts": {
                "english_pdf": str(en),
                "chinese_pdf": str(zh),
            },
        }
        (output_dir / "BILINGUAL_RESEARCH_PLAN_REPORT.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return report

    monkeypatch.setattr(module, "translate_review_package", fake_translation)
    monkeypatch.setattr(module, "build_latex_publication", fake_latex)
    monkeypatch.setattr(
        module,
        "build_bilingual_research_plan_publication",
        fake_plan_publication,
    )


def test_fresh_finish_gate_sees_all_four_new_publication_paths(
    tmp_path: Path, monkeypatch
) -> None:
    from optomind_research.runtime import review_harness_orchestrator as module
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    query_plan, kb_path, run_dir, plan = _harness_publication_fixture(tmp_path)
    _patch_harness_publication_dependencies(monkeypatch, module)
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
            produce_chinese_publication=True,
            produce_research_plan_publication=True,
        ),
        run_dir=run_dir,
    )
    result = harness._finish(
        "completed", "packaging", run_dir / "authoring" / "FINAL_REVIEW_EN.md",
        run_dir / "visual" / "VISUAL_PLAN.json", plan,
    )
    package = json.loads(result.package_path.read_text(encoding="utf-8"))
    gate = package["delivery_gate"]
    assert gate["passed"] is True
    assert result.status == "completed"
    assert all(
        gate["checks"][name]["ok"]
        for name in (
            "english_review_pdf",
            "chinese_review_pdf",
            "english_research_plan_pdf",
            "chinese_research_plan_pdf",
        )
    )
    assert gate["checks"]["research_plan_audit"]["status"] == "passed"
    assert package["artifacts"]["delivery_gate"]


def test_configured_research_plan_missing_is_blocking_at_finish(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    kb_path = tmp_path / "kb.sqlite"
    kb_path.write_bytes(b"")
    run_dir = tmp_path / "run"
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_research_plan_publication=True,
        ),
        run_dir=run_dir,
    )
    result = harness._finish("completed", "packaging", None, None, None)
    package = json.loads(result.package_path.read_text(encoding="utf-8"))
    assert result.status != "completed"
    assert package["delivery_gate"]["passed"] is False
    assert "english_research_plan_pdf" in package["delivery_gate"]["blocking_checks"]


def test_configured_review_missing_is_blocking_at_finish(tmp_path: Path) -> None:
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    kb_path = tmp_path / "kb.sqlite"
    kb_path.write_bytes(b"")
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
        ),
        run_dir=tmp_path / "run",
    )
    result = harness._finish("completed", "packaging", None, None, None)
    package = json.loads(result.package_path.read_text(encoding="utf-8"))
    assert result.status != "completed"
    assert package["delivery_gate"]["passed"] is False
    assert "english_review_pdf" in package["delivery_gate"]["blocking_checks"]


def test_needs_attention_quality_report_is_preserved_but_fails_open(
    tmp_path: Path, monkeypatch
) -> None:
    from optomind_research.runtime import review_harness_orchestrator as module
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    query_plan, kb_path, run_dir, _ = _harness_publication_fixture(tmp_path)
    _patch_harness_publication_dependencies(
        monkeypatch, module, quality_status="needs_attention"
    )
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=kb_path,
            output_root=tmp_path,
            produce_latex_publication=True,
        ),
        run_dir=run_dir,
    )
    result = harness._finish(
        "completed", "packaging", run_dir / "authoring" / "FINAL_REVIEW_EN.md",
        run_dir / "visual" / "VISUAL_PLAN.json", None,
    )
    package = json.loads(result.package_path.read_text(encoding="utf-8"))
    assert result.status == "completed"
    assert package["quality_gate"]["status"] == "needs_attention"
    assert package["delivery_gate"]["checks"]["english_review_audit"]["ok"] is True
    assert package["delivery_gate"]["passed"] is True
