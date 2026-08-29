#!/usr/bin/env python3
"""CLI stub for dominant-review expansion (real/live mode).

Accepts a probe JSON containing expansion requests and optionally executes
real OA retrieval when --live is set.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_dominant_review_expansion_real",
        description="Execute a dominant-review expansion probe (real OA retrieval).",
    )
    p.add_argument(
        "--probe-json",
        required=True,
        metavar="JSON_PATH",
        help="Path to a JSON file containing the expansion probe configuration.",
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live network access (default: dry-run).",
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default=None,
        help="Directory for expansion receipts.",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    probe_path = Path(args.probe_json)
    if not probe_path.exists():
        print(f"ERROR: probe file not found: {probe_path}", file=sys.stderr)
        sys.exit(1)

    probe = json.loads(probe_path.read_text(encoding="utf-8"))

    if not args.live:
        print(
            f"DRY-RUN: probe loaded ({len(probe)} top-level key(s)). "
            "Pass --live to execute retrieval.",
            flush=True,
        )
        sys.exit(0)

    print("ERROR: live expansion is not implemented in this stub.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
