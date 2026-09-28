"""Materialize a saved visual editorial plan with a bounded cost budget."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.visual_editor_tool_provider import (  # noqa: E402
    MAX_CONCEPTUAL_FIGURE_REQUESTS,
)
from optomind_research.runtime.visual_evidence_factory import (  # noqa: E402
    derive_visual_cache_namespace,
    run_visual_evidence_factory,
    scoped_visual_cache_dir,
    validate_final_visual_package_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--visual-plan", type=Path, required=True)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--review-work-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budget-cny", type=float, default=5.0)
    parser.add_argument("--real-visual-audit", action="store_true")
    parser.add_argument("--real-image-generation", action="store_true")
    parser.add_argument(
        "--image-model",
        default="qwen-image-2.0-pro",
    )
    # Matches MAX_CONCEPTUAL_FIGURE_REQUESTS and the factory config default;
    # a stale 2 here would cap a manual materialization below what the
    # editor is allowed to request.
    parser.add_argument(
        "--max-generated-images",
        type=int,
        default=MAX_CONCEPTUAL_FIGURE_REQUESTS,
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--shared-cache-dir",
        type=Path,
        default=(
            PROJECT_ROOT
            / "literature_workspace"
            / "visual_evidence_cache"
        ),
    )
    parser.add_argument(
        "--cache-namespace",
        default="",
        help=(
            "Optional safe visual-cache namespace; defaults to a hash of "
            "the supplied blueprint topic identity."
        ),
    )
    parser.add_argument("--production-review-policy", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    budget = min(50.0, max(0.0, float(args.budget_cny)))
    blueprint = json.loads(
        args.blueprint.read_text(encoding="utf-8")
    )
    cache_namespace = derive_visual_cache_namespace(
        blueprint,
        str(args.cache_namespace or ""),
    )
    package = run_visual_evidence_factory(
        visual_plan_path=args.visual_plan,
        blueprint=blueprint,
        review_work_dir=args.review_work_dir,
        output_dir=args.output_dir,
        cost_budget_cny=budget,
        real_visual_audit=bool(args.real_visual_audit),
        real_image_generation=bool(args.real_image_generation),
        test_mode=not bool(args.production_review_policy),
        image_model=str(args.image_model),
        max_generated_images=max(
            0,
            int(args.max_generated_images),
        ),
        run_id=str(args.run_id),
        shared_cache_dir=scoped_visual_cache_dir(
            args.shared_cache_dir,
            blueprint,
            namespace=cache_namespace,
        ),
        cache_namespace=cache_namespace,
    )
    validation = validate_final_visual_package_file(
        args.output_dir / "FINAL_VISUAL_PACKAGE.json"
    )
    print(
        json.dumps(
            {
                "validation": validation,
                "figure_count": len(package.get("figures", []) or []),
                "unfilled_count": len(
                    package.get(
                        "unfilled_visual_opportunities",
                        [],
                    )
                    or []
                ),
                "estimated_cost_cny": package.get(
                    "visual_cost_report",
                    {},
                ).get("estimated_cost_cny", 0.0),
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if validation.startswith("VALIDATION_PASSED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
