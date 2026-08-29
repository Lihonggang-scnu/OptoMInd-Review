"""Build paired English and Chinese LaTeX publications from one review package."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from .artifact_store import atomic_write_json
from .latex_publication_renderer import build_latex_publication
from .scientific_chinese_translator import translate_review_package


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _resolve_review_path(package_path: Path) -> Path:
    package = _read_json(package_path)
    value = str(package.get("final_review_path") or "").strip()
    if not value:
        raise ValueError("Content package has no final_review_path")
    candidate = Path(value)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    for base in (package_path.parent, Path(__file__).resolve().parents[2]):
        resolved = (base / candidate).resolve()
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(candidate)


def build_bilingual_publication(
    *,
    content_package_path: Path,
    output_dir: Path,
    metadata_path: Optional[Path] = None,
    translation_model_tier: str = "c2_model",
    translation_fallback_model_tier: str = "c_model",
    translation_workers: int = 3,
    translation_cost_budget_cny: float = 3.0,
    enrich_crossref: bool = True,
    compile_pdf: bool = True,
    pdf_strict: bool = False,
    render_previews: bool = True,
    translation_fail_open: bool = False,
) -> dict[str, Any]:
    """Create two independently validated PDFs and source archives."""

    started = time.monotonic()
    package_path = Path(content_package_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    review_path = _resolve_review_path(package_path)

    english_report = build_latex_publication(
        content_package_path=package_path,
        output_dir=output_dir / "latex_en",
        metadata_path=metadata_path,
        enrich_crossref=enrich_crossref,
        compile_pdf=compile_pdf,
        pdf_strict=pdf_strict,
        render_previews=render_previews,
        language="en",
    )
    translation_report = translate_review_package(
        content_package_path=package_path,
        source_markdown_path=review_path,
        output_dir=output_dir / "translation_zh",
        english_metadata_path=metadata_path,
        model_tier=translation_model_tier,
        fallback_model_tier=translation_fallback_model_tier,
        workers=translation_workers,
        cost_budget_cny=translation_cost_budget_cny,
        allow_partial_output=translation_fail_open,
    )
    chinese_report: dict[str, Any] = {
        "status": "not_run_translation_failed",
        "artifacts": {},
    }
    translated_path = str(translation_report.get("translated_path") or "")
    translated_metadata_path = str(
        translation_report.get("translated_metadata_path") or ""
    )
    if (
        translation_report.get("status") in {
            "completed",
            "completed_with_warnings",
        }
        and translated_path
        and translated_metadata_path
    ):
        chinese_report = build_latex_publication(
            content_package_path=package_path,
            output_dir=output_dir / "latex_zh",
            metadata_path=Path(translated_metadata_path),
            source_markdown_path=Path(translated_path),
            enrich_crossref=enrich_crossref,
            compile_pdf=compile_pdf,
            pdf_strict=pdf_strict,
            render_previews=render_previews,
            language="zh-CN",
        )

    status = (
        "completed"
        if (
            english_report.get("status") != "failed"
            and translation_report.get("status") in {
                "completed",
                "completed_with_warnings",
            }
            and chinese_report.get("status") != "failed"
        )
        else "failed"
    )
    report = {
        "schema_version": "research_harness.bilingual_publication.v1",
        "status": status,
        "content_package_path": str(package_path),
        "english": english_report,
        "translation": translation_report,
        "chinese": chinese_report,
        "estimated_translation_cost_cny": float(
            translation_report.get("estimated_cost_cny", 0.0) or 0.0
        ),
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "artifacts": {
            "english_pdf": str(
                english_report.get("artifacts", {}).get("compiled_pdf") or ""
            ),
            "english_source_archive": str(
                english_report.get("artifacts", {}).get("arxiv_source_zip")
                or ""
            ),
            "chinese_markdown": translated_path,
            "chinese_pdf": str(
                chinese_report.get("artifacts", {}).get("compiled_pdf") or ""
            ),
            "chinese_source_archive": str(
                chinese_report.get("artifacts", {}).get("arxiv_source_zip")
                or ""
            ),
            "translation_report": str(
                output_dir / "translation_zh" / "TRANSLATION_REPORT.json"
            ),
        },
    }
    atomic_write_json(output_dir / "BILINGUAL_PUBLICATION_REPORT.json", report)
    return report
