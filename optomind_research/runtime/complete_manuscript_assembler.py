"""Assemble validated article components without asking an LLM to rewrite them."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .article_completion_schemas import ArticleCompletionPackage
from .artifact_store import atomic_write_json, atomic_write_text


def _read_json(path: Optional[Path], default: Any) -> Any:
    if path is None or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _strip_top_title(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    if lines and re.match(r"^#\s+[^#]", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def assemble_complete_manuscript(
    *,
    completion_package_path: Path,
    body_review_path: Path,
    output_dir: Path,
    final_visual_package_path: Optional[Path] = None,
    global_figure_plan_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Assemble the complete English manuscript and a machine-readable manifest."""

    completion_raw = _read_json(completion_package_path, {})
    completion = ArticleCompletionPackage.model_validate(completion_raw)
    if not body_review_path.exists():
        raise FileNotFoundError(body_review_path)
    body = _strip_top_title(
        body_review_path.read_text(encoding="utf-8")
    )
    if not body:
        raise ValueError("body review is empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    components = [
        f"# {completion.title.strip()}",
        "## Abstract\n\n" + completion.abstract.strip(),
        "## Introduction\n\n" + completion.introduction.strip(),
        body,
        "## Challenges and Future Outlook\n\n"
        + completion.challenge_and_outlook.strip(),
        "## Conclusion\n\n" + completion.conclusion.strip(),
    ]
    manuscript = "\n\n".join(value for value in components if value).strip()
    manuscript_path = output_dir / "COMPLETE_REVIEW_EN.md"
    atomic_write_text(manuscript_path, manuscript)

    visual_package = _read_json(final_visual_package_path, {})
    global_plan = _read_json(global_figure_plan_path, {})
    figure_lines = ["# Figure Assets and Placement Guidance", ""]
    for figure in (
        visual_package.get("figures", [])
        if isinstance(visual_package, dict)
        else []
    ):
        if not isinstance(figure, dict):
            continue
        figure_lines.extend(
            [
                f"## {figure.get('figure_id', 'Figure')}",
                "",
                f"- Local image: {figure.get('local_path', '')}",
                f"- Placement: {figure.get('placement_anchor', '')}",
                f"- Purpose: {figure.get('purpose', '')}",
                f"- Caption: {figure.get('caption_en', '')}",
                f"- Provenance: {figure.get('data_provenance_level', '')}",
                "",
            ]
        )
    figure_placement_path = output_dir / "ARTICLE_FIGURE_PLACEMENTS.md"
    atomic_write_text(
        figure_placement_path,
        "\n".join(figure_lines).strip() + "\n",
    )
    manifest = {
        "schema_version": "complete_manuscript_manifest.v1",
        "status": "assembled",
        "manuscript_path": str(manuscript_path),
        "completion_package_path": str(completion_package_path),
        "body_review_path": str(body_review_path),
        "component_order": [
            "title",
            "abstract",
            "introduction",
            "body_sections",
            "challenge_and_outlook",
            "conclusion",
        ],
        "visual_package_path": (
            str(final_visual_package_path)
            if final_visual_package_path
            else ""
        ),
        "global_figure_plan_path": (
            str(global_figure_plan_path) if global_figure_plan_path else ""
        ),
        "materialized_figure_count": len(
            visual_package.get("figures", [])
            if isinstance(visual_package, dict)
            else []
        ),
        "planned_article_figure_count": len(
            global_plan.get("article_level_figures", [])
            if isinstance(global_plan, dict)
            else []
        ),
        "figure_placement_path": str(figure_placement_path),
        "word_count": len(re.findall(r"\b[\w'-]+\b", manuscript)),
    }
    manifest_path = output_dir / "COMPLETE_MANUSCRIPT_MANIFEST.json"
    atomic_write_json(manifest_path, manifest)
    return manifest
