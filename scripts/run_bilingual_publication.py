"""Build paired English and Chinese PDFs from a Review Harness package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.bilingual_publication import (
    build_bilingual_publication,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-package", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("--translation-model-tier", default="c2_model")
    parser.add_argument("--translation-fallback-model-tier", default="c_model")
    parser.add_argument("--translation-workers", type=int, default=3)
    parser.add_argument("--translation-cost-budget-cny", type=float, default=3.0)
    parser.add_argument(
        "--translation-fail-open",
        action="store_true",
        help=(
            "Deprecated and now the default: validated units are always kept. "
            "Retained so existing commands keep working."
        ),
    )
    parser.add_argument(
        "--translation-strict",
        action="store_true",
        help=(
            "Fail the Chinese stage when any translation unit fails instead "
            "of keeping the validated units as a degraded deliverable."
        ),
    )
    parser.add_argument(
        "--no-crossref-enrichment",
        action="store_true",
    )
    parser.add_argument("--no-previews", action="store_true")
    args = parser.parse_args()
    report = build_bilingual_publication(
        content_package_path=args.content_package,
        output_dir=args.output_dir,
        metadata_path=args.metadata,
        translation_model_tier=args.translation_model_tier,
        translation_fallback_model_tier=args.translation_fallback_model_tier,
        translation_workers=args.translation_workers,
        translation_cost_budget_cny=args.translation_cost_budget_cny,
        translation_fail_open=not bool(args.translation_strict),
        enrich_crossref=not args.no_crossref_enrichment,
        render_previews=not args.no_previews,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
