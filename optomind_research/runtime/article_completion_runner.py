"""Run one bounded article-completion editor through the shared workbench."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .article_completion_tool_provider import (
    ArticleCompletionContext,
    ArticleCompletionToolProvider,
)
from .artifact_store import atomic_write_json
from .article_synthesis_map_builder import collect_article_synthesis_inputs
from .research_worker import ResearchWorker
from .task_contract import ResultManifest, TaskContract, TaskStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "roles" / "Article Completion Editor.txt"
)


def _persist_recovered_result(
    *,
    ctx: ArticleCompletionContext,
    provider: ArticleCompletionToolProvider,
    result: ResultManifest,
    reason: str,
) -> ResultManifest:
    recovered = result.model_copy(
        update={
            "status": TaskStatus.completed,
            "stop_reason": reason,
            "validation_passed": True,
            "success_criteria_met": [
                *result.success_criteria_met,
                "deterministic_article_completion_validation:passed",
            ],
            "success_criteria_failed": [],
            "output_paths": {
                **result.output_paths,
                "article_completion_package": str(provider.package_path),
                "article_completion_validation": str(
                    provider.validation_path
                ),
            },
        }
    )
    atomic_write_json(
        ctx.work_dir / "RESULT.json",
        recovered.model_dump(mode="json"),
    )
    cost_path = ctx.work_dir / "COST.json"
    if cost_path.exists():
        cost = json.loads(cost_path.read_text(encoding="utf-8"))
        cost["status"] = TaskStatus.completed.value
        cost["stop_reason"] = recovered.stop_reason
        atomic_write_json(cost_path, cost)
    return recovered


def run_article_completion(
    ctx: ArticleCompletionContext,
    *,
    model_tier: str = "premium_model",
    model_override: Any = None,
    cost_budget_cny: float = 6.0,
    token_budget: int = 220_000,
) -> ResultManifest:
    """Generate the synthesis map and front/back matter in one agent task."""

    provider = ArticleCompletionToolProvider(ctx)
    prior_input_tokens = 0
    prior_cost_cny = 0.0
    # Resume without another paid call when the same body fingerprint already
    # produced a valid completion package.
    prior_result_path = ctx.work_dir / "RESULT.json"
    if (
        provider.package_path.exists()
        and provider.input_path.exists()
        and provider.map_path.exists()
        and prior_result_path.exists()
    ):
        try:
            stored_input = json.loads(
                provider.input_path.read_text(encoding="utf-8")
            )
            current_input = collect_article_synthesis_inputs(
                ctx.blueprint_path,
                ctx.sections_root,
            )
            prior_result = ResultManifest.model_validate_json(
                prior_result_path.read_text(encoding="utf-8")
            )
            persisted_validation = provider.validate_persisted_package()
            if (
                stored_input.get("input_fingerprint")
                == current_input.get("input_fingerprint")
                and "VALIDATION_PASSED"
                in persisted_validation
            ):
                return _persist_recovered_result(
                    ctx=ctx,
                    provider=provider,
                    result=prior_result,
                    reason=(
                        "reused_validated_article_completion: input "
                        "fingerprint unchanged; no model call required"
                    ),
                )
            # A formerly completed cache can become invalid when deterministic
            # quality gates are strengthened.  Downgrade the terminal marker
            # so ResearchWorker may resume from AGENT_STATE and repair it.
            invalidated = prior_result.model_copy(
                update={
                    "status": TaskStatus.validation_failed,
                    "stop_reason": (
                        "cached_article_completion_failed_current_validation"
                    ),
                    "validation_passed": False,
                    "success_criteria_failed": [
                        "deterministic_article_completion_validation:not_passed"
                    ],
                }
            )
            atomic_write_json(
                prior_result_path,
                invalidated.model_dump(mode="json"),
            )
            cost_path = ctx.work_dir / "COST.json"
            if cost_path.exists():
                cost = json.loads(cost_path.read_text(encoding="utf-8"))
                cost["status"] = TaskStatus.validation_failed.value
                cost["stop_reason"] = invalidated.stop_reason
                atomic_write_json(cost_path, cost)
        except Exception:
            # A malformed cache must never block a clean rerun.
            pass

    # A failed long ReAct history often teaches an economical model to repeat
    # the same malformed final submission.  Preserve it for audit, but start a
    # compact repair conversation while carrying prior spend into the enlarged
    # cumulative ceiling.
    if prior_result_path.exists() and not provider.package_path.exists():
        try:
            prior_result = ResultManifest.model_validate_json(
                prior_result_path.read_text(encoding="utf-8")
            )
            prior_input_tokens = int(prior_result.total_input_tokens or 0)
            prior_cost_cny = float(prior_result.estimated_cost_cny or 0.0)
            if prior_result.status in {
                TaskStatus.budget_exhausted,
                TaskStatus.validation_failed,
            }:
                state_path = ctx.work_dir / "AGENT_STATE.json"
                if state_path.exists():
                    archive_dir = (
                        ctx.work_dir
                        / "_runtime_archive"
                        / datetime.now(timezone.utc).strftime(
                            "%Y%m%dT%H%M%S%fZ"
                        )
                    )
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(state_path), str(archive_dir / state_path.name))
        except Exception:
            pass
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    worker = ResearchWorker(
        tool_provider=provider,
        _model_override=model_override,
        _system_prompt_override=prompt,
        _work_dir_override=ctx.work_dir,
    )
    suffix = hashlib.sha256(
        str(ctx.work_dir.resolve()).encode("utf-8")
    ).hexdigest()[:8]
    contract = TaskContract(
        run_id="article_completion_" + suffix,
        task_id="complete_review_article",
        goal=(
            "Build a validated article-wide synthesis map and write the title, "
            "abstract, introduction, challenge/outlook, and conclusion around "
            "the completed body sections."
        ),
        constraints=[
            "Use English only.",
            "Do not invent scientific identifiers or references.",
            "Treat section handoff cards as writing memory, not evidence.",
            "Include a complete, non-empty, citation-free abstract in the same final submission.",
            "Do not add new topics or evidence in the conclusion.",
        ],
        success_criteria=[
            "Every body section appears in the synthesis map.",
            "Challenges link to verified body sections.",
            "The introduction promises only content delivered by the body.",
            "The outlook separates established, conditional, and speculative judgements.",
            "The deterministic article completion validator passes.",
        ],
        allowed_tools=provider.get_allowed_tool_names(),
        skill_ids=["top-review-architecture"],
        model_tier=model_tier,
        max_iters=12,
        token_budget=max(
            token_budget,
            prior_input_tokens + 120_000 if prior_input_tokens else token_budget,
        ),
        cost_budget_cny=max(
            cost_budget_cny,
            prior_cost_cny + 1.0 if prior_cost_cny else cost_budget_cny,
        ),
        # Reserve enough for a short correction call while still allowing the
        # already-generated final tool submission to execute.
        next_call_cost_reserve_cny=min(0.35, cost_budget_cny * 0.15),
        wall_time_budget_seconds=600.0,
        expected_outputs=[
            "ARTICLE_SYNTHESIS_INPUT.json",
            "ARTICLE_SYNTHESIS_MAP.json",
            "ARTICLE_SYNTHESIS_MAP_AUDIT.json",
            "ARTICLE_COMPLETION_PACKAGE.json",
            "ARTICLE_COMPLETION_VALIDATION.json",
            "ARTICLE_TITLE.txt",
            "ARTICLE_ABSTRACT_EN.md",
            "ARTICLE_INTRODUCTION_EN.md",
            "ARTICLE_CHALLENGES_OUTLOOK_EN.md",
            "ARTICLE_CONCLUSION_EN.md",
        ],
    )
    result = worker.run(contract)
    if result.status != TaskStatus.completed and provider.package_path.exists():
        validation_result = provider.validate_persisted_package()
        if "VALIDATION_PASSED" in validation_result:
            result = _persist_recovered_result(
                ctx=ctx,
                provider=provider,
                result=result,
                reason=(
                    "deterministic_post_write_validation: the paid model "
                    "call wrote a valid package before the next-call cost "
                    "reserve stopped the agent"
                ),
            )
    return result


# Additive staged full-manuscript completion entry point.  The original
# run_article_completion behavior above is unchanged.
from .staged_article_completion import (  # noqa: E402
    run_staged_article_completion,
)
