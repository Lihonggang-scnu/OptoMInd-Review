"""Run the Phase-3 Review Lead through the shared AgentScope workbench."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.research_worker import ResearchWorker
from optomind_research.runtime.review_lead_tool_provider import (
    ReviewLeadContext,
    ReviewLeadToolProvider,
)
from optomind_research.runtime.task_contract import ResultManifest, TaskContract
from optomind_research.runtime.topic_identity import (
    build_topic_identity_contract,
)

def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def context_from_query_plan(
    query_plan_path: Path,
    output_dir: Path,
    kb_sqlite: Optional[Path],
    m1_library_path: Optional[Path] = None,
) -> ReviewLeadContext:
    """Build an English-only downstream context from Query Planner output."""

    raw = _read_json(query_plan_path)
    output = raw.get("output", raw)
    if not isinstance(output, dict):
        raise ValueError("query plan output must be an object")
    problem = str(output.get("problem_understanding") or "").strip()
    if not problem:
        raise ValueError("query plan has no English problem_understanding")
    raw_scope = output.get("scope_definition", "")
    if isinstance(raw_scope, dict):
        scope_parts = [str(raw_scope.get("main_scope") or "").strip()]
        scope_parts.extend(
            str(item).strip()
            for item in raw_scope.get("scope_items", [])
            if str(item).strip()
        )
        scope = "\n".join(part for part in scope_parts if part)
    else:
        scope = str(raw_scope).strip()
    topic_identity = build_topic_identity_contract(raw)
    if not topic_identity.get("valid"):
        raise ValueError(
            "query plan cannot produce a valid topic-identity contract"
        )
    return ReviewLeadContext(
        # The normalized English interpretation is the fixed downstream
        # question. The original Chinese query remains only in the stage-1
        # artifact and is not copied into the machine message stream.
        user_question=problem,
        problem_understanding=problem,
        scope_definition=scope,
        work_dir=output_dir,
        kb_sqlite=kb_sqlite,
        query_plan_path=query_plan_path,
        # Keep None as None: M1 guidance is optional.  Substituting the
        # archived outputs/ default here silently loaded {} and degraded
        # blueprint quality; callers pass --m1-library to opt in.
        m1_library_path=m1_library_path,
        topic_identity=topic_identity,
        visual_policy={
            "use_existing_visual_assets": True,
            "conceptual_figures_may_be_generated": True,
            "generated_quantitative_results_forbidden": True,
            "missing_visuals_must_not_delete_valid_text": True,
        },
    )


def run_review_lead(
    ctx: ReviewLeadContext,
    *,
    model_tier: str = "premium_model",
    model_override: Any = None,
    cost_budget_cny: float = 5.0,
) -> ResultManifest:
    provider = ReviewLeadToolProvider(ctx)
    role_prompt = (
        PROJECT_ROOT / "prompts" / "roles" / "Review Lead.txt"
    ).read_text(encoding="utf-8")
    worker = ResearchWorker(
        tool_provider=provider,
        _model_override=model_override,
        _system_prompt_override=role_prompt,
        _work_dir_override=ctx.work_dir,
    )
    stable_suffix = hashlib.sha256(
        str(ctx.work_dir.resolve()).encode("utf-8")
    ).hexdigest()[:8]
    contract = TaskContract(
        run_id="review_lead_" + stable_suffix,
        task_id="review_blueprint",
        goal=(
            "Design and validate the complete intellectual blueprint for the "
            "provided scientific literature review."
        ),
        constraints=[
            "Use English only.",
            "M1 mentor moves are writing instruction, never scientific evidence.",
            "Do not search for papers or mount evidence in this stage.",
            "Plan section-level literature roles and argumentative visuals.",
        ],
        success_criteria=[
            "The blueprint answers the fixed question and scope.",
            "The article has one thesis, one taxonomy principle, and distinct section roles.",
            "Each section has coverage roles, synthesis task, transitions, and visual slots.",
            "The deterministic blueprint validator passes.",
        ],
        allowed_tools=provider.get_allowed_tool_names(),
        skill_ids=["top-review-architecture"],
        model_tier=model_tier,
        # Blueprint validation can return many section-local repairs for a
        # 8-10 section review.  Ten outer iterations was too small: a valid
        # first draft could be produced but the worker was stopped before it
        # had a chance to apply the validator feedback.  Cost and wall-time
        # budgets remain the actual admission controls.
        max_iters=24,
        token_budget=300_000,
        cost_budget_cny=cost_budget_cny,
        next_call_cost_reserve_cny=0.5,
        wall_time_budget_seconds=900.0,
        expected_outputs=[
            "REVIEW_BLUEPRINT.json",
            "REVIEW_BLUEPRINT_VALIDATION.json",
        ],
    )
    return worker.run(contract)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query-plan", type=Path, required=True)
    parser.add_argument("--kb-sqlite", type=Path)
    parser.add_argument("--m1-library", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-tier", default="premium_model")
    parser.add_argument("--cost-budget-cny", type=float, default=5.0)
    args = parser.parse_args()

    ctx = context_from_query_plan(
        args.query_plan,
        args.output_dir,
        args.kb_sqlite,
        args.m1_library,
    )
    result = run_review_lead(
        ctx,
        model_tier=args.model_tier,
        cost_budget_cny=args.cost_budget_cny,
    )
    print(
        json.dumps(
            {
                "status": result.status.value,
                "work_dir": result.output_paths.get("work_dir", ""),
                "input_tokens": result.total_input_tokens,
                "output_tokens": result.total_output_tokens,
                "estimated_cost_cny": result.estimated_cost_cny,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if result.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
