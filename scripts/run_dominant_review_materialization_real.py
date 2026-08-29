#!/usr/bin/env python3
"""CLI stub for dominant-review materialization (real/live mode).

Requires --live and --requests to actually execute network calls.
In tests the module is imported and main() is called directly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_dominant_review_materialization_real",
        description="Materialise dominant-review expansion requests via the OA pipeline.",
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live network access (default: dry-run).",
    )
    p.add_argument(
        "--requests",
        metavar="JSON_PATH",
        help="Path to a JSON file containing the list of materialization requests.",
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default=None,
        help="Directory to write materialization receipts into.",
    )
    p.add_argument(
        "--cache-root",
        metavar="DIR",
        default=None,
        help="Override the central material cache root directory.",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.requests:
        parser.error("--requests is required")

    requests_path = Path(args.requests)
    if not requests_path.exists():
        print(f"ERROR: requests file not found: {requests_path}", file=sys.stderr)
        sys.exit(1)

    requests_data = json.loads(requests_path.read_text(encoding="utf-8"))

    if not args.live:
        print(
            f"DRY-RUN: would materialise {len(requests_data)} request(s). "
            "Pass --live to execute.",
            flush=True,
        )
        sys.exit(0)

    # Live execution is out-of-scope for this stub.
    print("ERROR: live materialization is not implemented in this stub.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
