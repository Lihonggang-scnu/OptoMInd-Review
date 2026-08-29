#!/usr/bin/env python3
"""Research worker smoke script.

Supports --dry-run-fail for CI gate checks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_research_worker_smoke",
        description="Smoke test for the research worker runtime.",
    )
    p.add_argument(
        "--dry-run-fail",
        action="store_true",
        default=False,
        help="Exit immediately with code 1 (used to verify CI gate).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.dry_run_fail:
        print("Exiting with code 1 (--dry-run-fail)", flush=True)
        return 1

    print("Research worker smoke: OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
