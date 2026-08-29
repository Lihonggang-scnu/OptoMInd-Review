#!/usr/bin/env python3
"""CLI for building a dominant-review trigger manifest for one blueprint section.

Usage:
    python scripts/run_dominant_review_trigger_manifest.py \
        --blueprint path/to/blueprint.json \
        --section-index 0 \
        --source-metadata path/to/metadata.json \
        --bibliography path/to/bibliography.json \
        --output path/to/manifest.json
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
        prog="run_dominant_review_trigger_manifest",
        description="Build a dominant-review trigger manifest for one blueprint section.",
    )
    p.add_argument(
        "--blueprint",
        required=True,
        metavar="JSON_PATH",
        help="Path to the review blueprint JSON file.",
    )
    p.add_argument(
        "--section-index",
        type=int,
        default=0,
        metavar="N",
        help="Zero-based index of the section to process (default: 0).",
    )
    p.add_argument(
        "--source-metadata",
        metavar="JSON_PATH",
        default=None,
        help="Path to a JSON file mapping paper_id -> source metadata.",
    )
    p.add_argument(
        "--bibliography",
        metavar="JSON_PATH",
        default=None,
        help="Path to a JSON file mapping paper_id -> numbered bibliography entries.",
    )
    p.add_argument(
        "--output",
        required=True,
        metavar="JSON_PATH",
        help="Destination path for the manifest JSON output.",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    blueprint_path = Path(args.blueprint)
    if not blueprint_path.exists():
        print(f"ERROR: blueprint not found: {blueprint_path}", file=sys.stderr)
        sys.exit(1)

    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))

    source_metadata = {}
    if args.source_metadata:
        sm_path = Path(args.source_metadata)
        if sm_path.exists():
            source_metadata = json.loads(sm_path.read_text(encoding="utf-8"))

    bibliography_by_source: dict = {}
    if args.bibliography:
        bib_path = Path(args.bibliography)
        if bib_path.exists():
            raw = json.loads(bib_path.read_text(encoding="utf-8"))
            # Keys inside each paper-entry may be ints in JSON (they become str after
            # round-trip); re-convert back to int so the downstream logic works.
            for pid, entries in raw.items():
                bibliography_by_source[pid] = {
                    int(k): v for k, v in entries.items()
                }

    from optomind_research.dominant_review_expansion import (
        build_dominant_review_trigger_manifest,
    )

    manifest = build_dominant_review_trigger_manifest(
        blueprint,
        section_index=args.section_index,
        source_metadata=source_metadata,
        bibliography_by_source=bibliography_by_source,
        blueprint_path=blueprint_path,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Manifest written to {output_path}", flush=True)


if __name__ == "__main__":
    main()
