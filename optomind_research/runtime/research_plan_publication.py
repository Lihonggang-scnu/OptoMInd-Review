"""Publish the research-plan branch as paired English and Chinese PDFs.

The plan reuses the review's evidence registry and bibliography resolver, but
has its own source Markdown, metadata, integrity audit, and bilingual output.
It therefore remains a sibling intellectual product rather than an appendix of
the review manuscript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .artifact_store import atomic_write_json
from .latex_publication_renderer import build_latex_publication
from .scientific_chinese_translator import translate_review_package


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def build_bilingual_research_plan_publication(
    *,
    research_program_dir: Path,
    review_content_package_path: Path,
    output_dir: Path,
    translation_model_tier: str = "c2_model",
    translation_fallback_model_tier: str = "c_model",
    translation_workers: int = 2,
    translation_cost_budget_cny: float = 2.0,
    enrich_crossref: bool = True,
    compile_pdf: bool = True,
    pdf_strict: bool = False,
    render_previews: bool = True,
) -> dict[str, Any]:
    """Create a traceable bilingual research-plan publication package."""

    research_program_dir = Path(research_program_dir).resolve()
    review_content_package_path = Path(review_content_package_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = research_program_dir / "RESEARCH_PLAN.md"
    plan_json = _read_json(research_program_dir / "RESEARCH_PLAN.json")
    review_package = _read_json(review_content_package_path)
    if not plan_path.is_file() or not plan_json:
        raise ValueError("Validated RESEARCH_PLAN.md and RESEARCH_PLAN.json are required")

    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    metadata_path = staging / "RESEARCH_PLAN_METADATA_EN.json"
    atomic_write_json(
        metadata_path,
        {
            "title": str(plan_json.get("title") or "Scientific Hypothesis and Research Plan"),
            "authors": [{"name": "OptoMind Research Planning Draft"}],
            "abstract": str(plan_json.get("paper_abstract") or ""),
            "keywords": ["research plan", "scientific hypothesis", "verification deferred"],
            "draft_only": True,
            "document_type": "research_plan",
        },
    )
    plan_package_path = staging / "RESEARCH_PLAN_CONTENT_PACKAGE.json"
    atomic_write_json(
        plan_package_path,
        {
            "schema_version": "research_harness.research_plan_content_package.v1",
            "document_type": "research_plan",
            "final_review_path": str(plan_path),
            "source_run_dir": str(review_content_package_path.parent),
            "base_kb_sqlite": review_package.get("base_kb_sqlite", ""),
            "artifacts": {
                "review_blueprint": review_package.get("artifacts", {}).get("review_blueprint", ""),
                "research_plan_json": str(research_program_dir / "RESEARCH_PLAN.json"),
                "research_problem_frame": str(research_program_dir / "RESEARCH_PROBLEM_FRAME.json"),
                "research_gap_map": str(research_program_dir / "RESEARCH_GAP_MAP.json"),
            },
        },
    )
    english = build_latex_publication(
        content_package_path=plan_package_path,
        output_dir=output_dir / "latex_en",
        metadata_path=metadata_path,
        language="en",
        document_type="research_plan",
        enrich_crossref=enrich_crossref,
        compile_pdf=compile_pdf,
        pdf_strict=pdf_strict,
        render_previews=render_previews,
    )
    translation = translate_review_package(
        content_package_path=plan_package_path,
        source_markdown_path=plan_path,
        output_dir=output_dir / "translation_zh",
        english_metadata_path=metadata_path,
        model_tier=translation_model_tier,
        fallback_model_tier=translation_fallback_model_tier,
        workers=translation_workers,
        cost_budget_cny=translation_cost_budget_cny,
    )
    chinese: dict[str, Any] = {"status": "not_run_translation_failed", "artifacts": {}}
    translated_path = str(translation.get("translated_path") or "")
    translated_metadata = str(translation.get("translated_metadata_path") or "")
    if translation.get("status") == "completed" and translated_path:
        chinese = build_latex_publication(
            content_package_path=plan_package_path,
            output_dir=output_dir / "latex_zh",
            metadata_path=Path(translated_metadata) if translated_metadata else None,
            source_markdown_path=Path(translated_path),
            language="zh-CN",
            document_type="research_plan",
            enrich_crossref=enrich_crossref,
            compile_pdf=compile_pdf,
            pdf_strict=pdf_strict,
            render_previews=render_previews,
        )
    report = {
        "schema_version": "research_harness.bilingual_research_plan_publication.v1",
        "status": "completed" if (
            english.get("status") != "failed"
            and translation.get("status") == "completed"
            and chinese.get("status") != "failed"
        ) else "failed",
        "verification_status": "verification_deferred",
        "english": english,
        "translation": translation,
        "chinese": chinese,
        "artifacts": {
            "english_pdf": str(english.get("artifacts", {}).get("compiled_pdf") or ""),
            "chinese_pdf": str(chinese.get("artifacts", {}).get("compiled_pdf") or ""),
            "english_source_archive": str(english.get("artifacts", {}).get("arxiv_source_zip") or ""),
            "chinese_source_archive": str(chinese.get("artifacts", {}).get("arxiv_source_zip") or ""),
            "plan_content_package": str(plan_package_path),
            "plan_metadata": str(metadata_path),
        },
    }
    atomic_write_json(output_dir / "BILINGUAL_RESEARCH_PLAN_REPORT.json", report)
    return report
