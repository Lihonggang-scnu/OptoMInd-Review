"""Run the article-level Visual Editor on the shared ResearchWorker."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .research_worker import ResearchWorker
from .task_contract import ResultManifest, TaskContract, TaskStatus
from .visual_editor_tool_provider import (
    VisualEditorContext,
    VisualEditorToolProvider,
    validate_visual_editorial_plan_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Tool-result envelope for the visual editor, in tokens.  Without an explicit
# value the contract inherits ResearchWorker's 1800-token default (~7.2 kB of
# ASCII JSON), which is smaller than the article-map payload the tool provider
# believes it is delivering whole -- AgentScope would truncate a map the
# provider had already declared complete.  The article map is this role's
# primary evidence: it must hold several source-figure candidates per section
# or the editor cannot place any figure at all.  The payload guards in
# ``inspect_article_visual_candidates`` are sized against this number.
VISUAL_EDITOR_TOOL_RESULT_TOKENS = 6000


def _cache_path_signature(path: Path) -> Dict[str, Any]:
    """Fingerprint legacy files, snapshot directories, and their payloads."""

    path = Path(path)
    if path.is_dir():
        files = sorted(
            (candidate for candidate in path.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(path).as_posix(),
        )[:500]
        return {
            "path": str(path.resolve()),
            "kind": "snapshot_directory",
            "files": [
                {
                    "relative": str(candidate.relative_to(path)),
                    "size": candidate.stat().st_size,
                    "mtime_ns": candidate.stat().st_mtime_ns,
                }
                for candidate in files
            ],
        }
    if path.is_file():
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "kind": "file",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return {"path": str(path.resolve()), "missing": True}


def visual_editor_input_fingerprint(
    *,
    blueprint: Dict[str, Any],
    review_work_dir: Path,
    kb_sqlite_paths: List[Path],
    role_prompt: str,
) -> str:
    """Fingerprint every input that can change a visual editorial decision."""

    section_drafts: Dict[str, str] = {}
    for section in blueprint.get("sections", []):
        if not isinstance(section, dict) or not section.get("section_id"):
            continue
        section_id = str(section["section_id"])
        draft_path = (
            Path(review_work_dir)
            / "sections"
            / section_id
            / "SECTION_DRAFT_EN.md"
        )
        section_drafts[section_id] = (
            draft_path.read_text(encoding="utf-8", errors="replace")
            if draft_path.is_file()
            else ""
        )
    kb_signatures = []
    for raw_path in kb_sqlite_paths:
        kb_signatures.append(_cache_path_signature(Path(raw_path)))
    payload = {
        "blueprint": blueprint,
        "section_drafts": section_drafts,
        "knowledge_bases": kb_signatures,
        "role_prompt": role_prompt,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def run_visual_editor(
    *,
    blueprint: Dict[str, Any],
    review_work_dir: Path,
    output_dir: Path,
    kb_sqlite_paths: List[Path],
    model_tier: str = "advanced_model",
    model_override: Any = None,
    cost_budget_cny: float = 3.0,
) -> ResultManifest:
    prompt = (
        PROJECT_ROOT / "prompts" / "roles" / "Visual Editor.txt"
    ).read_text(encoding="utf-8")
    input_fingerprint = visual_editor_input_fingerprint(
        blueprint=blueprint,
        review_work_dir=review_work_dir,
        kb_sqlite_paths=kb_sqlite_paths,
        role_prompt=prompt,
    )
    nested_budget = min(0.8, max(0.0, cost_budget_cny * 0.25))
    outer_budget = max(0.3, cost_budget_cny - nested_budget)
    context = VisualEditorContext(
        blueprint=blueprint,
        review_work_dir=review_work_dir,
        work_dir=output_dir,
        kb_sqlite_paths=kb_sqlite_paths,
        classifier_cost_budget_cny=nested_budget,
        input_fingerprint=input_fingerprint,
    )
    provider = VisualEditorToolProvider(context)
    expected_visual_section_ids = provider._expected_visual_section_ids()
    worker_work_dir = (
        output_dir / "worker_runs" / input_fingerprint
    )
    worker = ResearchWorker(
        tool_provider=provider,
        _model_override=model_override,
        _system_prompt_override=prompt,
        _work_dir_override=worker_work_dir,
    )
    contract = TaskContract(
        run_id="visual_editor_" + input_fingerprint,
        task_id="article_visual_plan",
        goal=(
            "Create and validate an article-level plan of traceable existing "
            "figures, conceptual figure requests, and explicitly unfilled needs."
        ),
        constraints=[
            "Use English only.",
            "Never invent quantitative or experimental visual evidence.",
            "Use only visual IDs and paths returned by inspection tools.",
            "A missing figure must not invalidate otherwise sound prose.",
        ],
        success_criteria=[
            "Every selected existing figure has a verified local path and provenance.",
            "Every figure has a clear argumentative purpose.",
            "Conceptual requests are disclosed and contain no invented results.",
            "The deterministic visual plan validator passes.",
        ],
        allowed_tools=provider.get_allowed_tool_names(),
        skill_ids=["visual-evidence-curation"],
        model_tier=model_tier,
        # Visual planning may inspect the article, local cache, source maps,
        # permissions, and then validate a remount plan. Keep a generous
        # iteration ceiling and rely on the explicit cost/wall-time budgets.
        max_iters=24,
        token_budget=180_000,
        cost_budget_cny=outer_budget,
        next_call_cost_reserve_cny=0.3,
        wall_time_budget_seconds=300.0,
        expected_outputs=[],
        # Without this the contract inherits ResearchWorker's 1800-token
        # default, which is ~7.2 kB of ASCII JSON -- below the article-map
        # payload guard in the tool provider, so AgentScope would truncate a
        # map the provider believed it had delivered whole.  The editor needs
        # to see many candidates per section to place source figures at all.
        metadata={
            "context_tool_result_limit": VISUAL_EDITOR_TOOL_RESULT_TOKENS
        },
    )
    result = worker.run(contract)
    fingerprint_plan = worker_work_dir / "VISUAL_EDITORIAL_PLAN.json"
    if fingerprint_plan.is_file():
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fingerprint_plan, provider.plan_path)
    elif (
        provider.plan_path.is_file()
        and validate_visual_editorial_plan_file(
            provider.plan_path,
            input_fingerprint,
            expected_visual_section_ids,
        ).startswith("VALIDATION_PASSED")
    ):
        worker_work_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(provider.plan_path, fingerprint_plan)
    if not provider.plan_path.is_file():
        recovery = provider.finalize_safe_partial_plan()
        if recovery.get("recovered"):
            result = result.model_copy(
                update={
                    "status": TaskStatus.completed,
                    "stop_reason": (
                        "deterministic_safe_partial_recovery: "
                        "invalid visual items were dropped without replacement"
                    ),
                    "validation_passed": True,
                    "success_criteria_met": [
                        *result.success_criteria_met,
                        "deterministic_visual_plan_validation:passed",
                    ],
                    "success_criteria_failed": [],
                    "output_paths": {
                        **result.output_paths,
                        "visual_editorial_plan": str(provider.plan_path),
                        "visual_recovery": str(provider.recovery_path),
                    },
                }
            )
    return result
