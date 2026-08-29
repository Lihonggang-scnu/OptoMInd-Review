from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.research_plan_publication import (
    build_bilingual_research_plan_publication,
)
from optomind_research.runtime.research_program_schemas import ResearchPlan
from optomind_research.runtime.research_program_tool_provider import (
    _normalize_plan_payload,
    _render_research_plan_markdown,
)


def _raw_plan() -> dict:
    packages = []
    for index in range(1, 4):
        packages.append(
            {
                "work_package_id": f"WP{index:02d}",
                "title": f"Planned validation route {index}",
                "objective": "Separate a literature-grounded mechanism from an alternative explanation.",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "methods": ["Plan a discriminating measurement and analysis protocol."],
                "inputs": ["A future traceable measurement dataset."],
                "expected_outputs": ["A pre-registered decision record."],
                "controls_or_baselines": ["A matched baseline condition."],
                "evaluation_metrics": ["Pre-specified discriminating metric."],
                "dependencies": [] if index == 1 else ["WP01"],
                "risks": ["The proposed contrast may remain confounded."],
                "readiness": "needs_more_literature",
                "stop_or_pivot_criteria": ["Pivot if the contrast is not identifiable."],
            }
        )
    return {
        "title": "A Traceable Optical Research Program",
        "research_question": "Which mechanism should be tested next?",
        "strategy": "Use evidence-grounded hypotheses and discriminating planned validation routes.",
        "objectives": ["Resolve the highest-value knowledge gap."],
        "work_packages": packages,
        "milestones": ["Review the planned decision record."],
        "human_decision_points": ["Approve the future execution scope."],
        "unresolved_literature_needs": ["Locate an independent benchmark study."],
        "narrative_markdown": "This program converts a bounded research gap into falsifiable hypotheses while separating evidence from planned validation. " * 20,
    }


def test_plan_protocol_forces_unexecuted_work_to_verification_deferred() -> None:
    normalized = _normalize_plan_payload(_raw_plan())
    model = ResearchPlan.model_validate(normalized)
    plan = model.model_dump()
    assert plan["results_status"] == "verification_deferred"
    assert plan["verification_deferred"]
    assert all(
        package["verification_status"] == "verification_deferred"
        for package in plan["work_packages"]
    )
    assert plan["dataset_source"]
    assert plan["dataset_target"]
    markdown = _render_research_plan_markdown(plan)
    assert "## Verification Status" in markdown
    assert "verification_deferred" in markdown
    assert "## Experiments" in markdown
    assert "## Expected Results" in markdown


def test_plan_bilingual_wrapper_uses_a_sibling_document_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from optomind_research.runtime import research_plan_publication as module

    program_dir = tmp_path / "run" / "research_program"
    program_dir.mkdir(parents=True)
    plan = _normalize_plan_payload(_raw_plan())
    plan["reference_paper_ids"] = ["paper_A"]
    (program_dir / "RESEARCH_PLAN.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    (program_dir / "RESEARCH_PLAN.md").write_text(
        _render_research_plan_markdown(plan) + "\n[REF:paper_A]\n",
        encoding="utf-8",
    )
    review_package = tmp_path / "run" / "REVIEW_CONTENT_PACKAGE.json"
    review_package.write_text(
        json.dumps({"base_kb_sqlite": "", "artifacts": {}}),
        encoding="utf-8",
    )
    calls: list[dict] = []

    def fake_latex(**kwargs):
        calls.append(kwargs)
        output = Path(kwargs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        pdf = output / "main.pdf"
        pdf.write_bytes(b"%PDF-test")
        return {
            "status": "compiled_awaiting_metadata",
            "artifacts": {"compiled_pdf": str(pdf), "arxiv_source_zip": ""},
        }

    def fake_translate(**kwargs):
        output = Path(kwargs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        translated = output / "RESEARCH_PLAN_ZH.md"
        metadata = output / "PUBLICATION_METADATA_ZH.json"
        translated.write_text("# \u8ba1\u5212\n\n\u9a8c\u8bc1\u5ef6\u540e\u3002", encoding="utf-8")
        metadata.write_text(json.dumps({"title": "\u8ba1\u5212", "abstract": "\u6458\u8981"}), encoding="utf-8")
        return {
            "status": "completed",
            "translated_path": str(translated),
            "translated_metadata_path": str(metadata),
        }

    monkeypatch.setattr(module, "build_latex_publication", fake_latex)
    monkeypatch.setattr(module, "translate_review_package", fake_translate)
    report = build_bilingual_research_plan_publication(
        research_program_dir=program_dir,
        review_content_package_path=review_package,
        output_dir=tmp_path / "published_plan",
    )
    assert report["verification_status"] == "verification_deferred"
    assert len(calls) == 2
    assert all(call["document_type"] == "research_plan" for call in calls)
    content_package = json.loads(
        Path(report["artifacts"]["plan_content_package"]).read_text(encoding="utf-8")
    )
    assert content_package["document_type"] == "research_plan"
    assert content_package["source_run_dir"] == str(review_package.parent)
