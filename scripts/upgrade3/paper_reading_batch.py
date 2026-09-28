"""CLI for the resumable per-paper reading-card batch driver."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.paper_reading_batch import (  # noqa: E402
    DEFAULT_BUDGET_LIMIT_CNY,
    DEFAULT_INITIAL_WORKERS,
    DEFAULT_MAX_WORKERS,
    PaperReadingBatchError,
    run_paper_reading_batch,
)
from optomind_research.runtime.upgrade3.paper_reading_card import (  # noqa: E402
    DEFAULT_CONCISE_OUTPUT,
    DEFAULT_INPUT_PROFILE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_BUDGET,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or preflight a resumable paper-reading-card batch.")
    parser.add_argument("--manifest", required=True, help="JSON manifest (records/rows plus optional finalized flag)")
    parser.add_argument("--output-root", required=True, help="Immutable per-batch output directory")
    parser.add_argument("--run", action="store_true", help="Explicitly permit live model calls; default is preflight")
    parser.add_argument("--input-profile", choices=("standard", "economy"), default=DEFAULT_INPUT_PROFILE)
    parser.add_argument("--no-thinking", action="store_true", default=not DEFAULT_THINKING)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET)
    parser.add_argument("--concise-output", action="store_true", default=DEFAULT_CONCISE_OUTPUT)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--key-file", default="", help="Required with --run; file contents are never printed")
    parser.add_argument("--budget-ledger", default="", help="Shared sqlite ledger, required with --run")
    parser.add_argument("--budget-limit-cny", type=float, default=DEFAULT_BUDGET_LIMIT_CNY)
    parser.add_argument("--initial-workers", type=int, default=DEFAULT_INITIAL_WORKERS)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_paper_reading_batch(
            manifest_path=args.manifest,
            output_root=args.output_root,
            mode="run" if args.run else "preflight",
            input_profile=args.input_profile,
            thinking=not args.no_thinking,
            thinking_budget=args.thinking_budget,
            concise_output=args.concise_output,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
            key_file=args.key_file or None,
            budget_ledger_path=args.budget_ledger or None,
            budget_limit_cny=args.budget_limit_cny,
            initial_workers=args.initial_workers,
            max_workers=args.max_workers,
            retry_delay_seconds=args.retry_delay_seconds,
            poll_seconds=args.poll_seconds,
        )
        print(json.dumps({"output_root": result["output_root"], "counts": result["counts"], "cards_index": result["cards_index"], "planning_views": result["planning_views"]}, ensure_ascii=False, indent=2))
        return 0
    except (PaperReadingBatchError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

