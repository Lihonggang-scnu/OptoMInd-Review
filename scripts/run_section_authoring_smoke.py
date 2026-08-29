#!/usr/bin/env python3
"""Section authoring smoke script.

Supports --dry-run-fail for CI gate checks and exposes _section_data_from_real
for loading material packages from the real Phase-2/Phase-3 output layout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Real Phase-2 adapter
# ---------------------------------------------------------------------------

def _section_data_from_real(package_path: Path) -> dict:
    """Load section data from a SECTION_MATERIAL_PACKAGE.json file.

    Also reads the sibling SECTION_CONTEXT.json to merge writing-contract
    fields (section_contract, word_budget, paragraph_functions).
    """
    package_path = Path(package_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))

    context_path = package_path.parent / "SECTION_CONTEXT.json"
    if context_path.exists():
        context = json.loads(context_path.read_text(encoding="utf-8"))
        # Merge section_contract from context into package
        if "section_contract" in context:
            package["section_contract"] = context["section_contract"]
        elif "section_id" in context:
            # context might embed the contract at top level
            for field in ("word_budget", "paragraph_functions", "writing_goal"):
                if field in context:
                    package.setdefault("section_contract", {})[field] = context[field]

    return package


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_section_authoring_smoke",
        description="Smoke test for the section authoring worker.",
    )
    p.add_argument(
        "--dry-run-fail",
        action="store_true",
        default=False,
        help="Exit immediately with code 1 (used to verify CI gate).",
    )
    p.add_argument(
        "--section-package",
        metavar="JSON_PATH",
        default=None,
        help="Path to SECTION_MATERIAL_PACKAGE.json for a real-data smoke run.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.dry_run_fail:
        print("Exiting with code 1 (--dry-run-fail)", flush=True)
        return 1

    if args.section_package:
        data = _section_data_from_real(Path(args.section_package))
        print(
            f"Section package loaded: section_id={data.get('section_id')} "
            f"word_budget={data.get('section_contract', {}).get('word_budget')}",
            flush=True,
        )

    print("Section authoring smoke: OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
