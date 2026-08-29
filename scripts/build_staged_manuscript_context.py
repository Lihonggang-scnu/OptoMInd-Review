"""CLI for deterministic staged manuscript context preparation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optomind_research.runtime.staged_manuscript_context import (  # noqa: E402
    SCHEMA_VERSION,
    StagedContextError,
    build_staged_manuscript_context,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare deterministic stage-specific context for staged "
            "full-manuscript Qwen calls from the unified handoff package "
            "and the commander work order."
        )
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--handoff-json", required=True, type=Path)
    parser.add_argument("--commander-work-order-json", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        summary = build_staged_manuscript_context(
            project_root=args.project_root,
            handoff_path=args.handoff_json,
            commander_work_order_path=args.commander_work_order_json,
            output_dir=args.output_dir,
        )
    except StagedContextError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    compact = {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": summary["input_fingerprint"],
        "stage_keys": summary["stage_keys"],
        "aggregate_counts": summary["aggregate_counts"],
        "output_paths": summary["output_paths"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA_VERSION", "StagedContextError", "build_arg_parser", "main"]
