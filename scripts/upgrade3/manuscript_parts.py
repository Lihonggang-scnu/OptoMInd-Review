"""Generate independent manuscript parts after the completed BODY, offline.

This entry does not run or modify BODY planning, arrangement, or writing.
It uses a labeled fixture or recorded responses for the real serial chain:
conception -> conclusion -> introduction -> abstract/title.  The runtime's
``provider`` callable is an explicit injection seam for a separately budgeted
driver; this CLI never constructs one and never falls back to a live model.

Example::

    python scripts/upgrade3/manuscript_parts.py --draft BODY.md \
        --research-question "The review's question" --context context.json \
        --fixture parts.json --output outputs/post_body_parts

The optional context is a JSON object with research_question, shared_scope,
review_argument, final_outline, material_records, and/or source_identity_map.
Its file references resolve relative to the context file.  It does not need
an early PartPlan.  A fresh output directory is required for each run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--draft", type=Path, required=True,
                        help="Completed BODY/manuscript Markdown; never modified")
    parser.add_argument("--context", type=Path,
                        help="Optional post-BODY context JSON; no early plan required")
    parser.add_argument("--research-question", default="",
                        help="Review question; overrides context.research_question")
    parser.add_argument("--output", type=Path, required=True,
                        help="Fresh directory for prompts, plan, parts, report and final manuscript")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path,
                        help="Labeled JSON fixture with conception/conclusion/introduction/abstract")
    source.add_argument("--recordings", type=Path,
                        help="Replay JSON with serial_parts:<stage> keys; no live fallback")
    parser.add_argument("--language", default="zh")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from optomind_research.runtime.upgrade3.review_delivery import (
        load_post_body_context,
        load_recordings,
    )
    try:
        context = load_post_body_context(
            args.context.resolve() if args.context else None,
            base_dir=args.context.resolve().parent if args.context else Path.cwd(),
        )
        question = str(args.research_question or context.get("research_question") or "").strip()
        if not question:
            parser.error("provide --research-question or context.research_question")
        if args.research_question:
            context["research_question"] = question
        chapter_roles = context.pop("chapter_roles", [])
        from optomind_research.runtime.upgrade3.serial_manuscript_parts import run_serial_parts

        report = run_serial_parts(
            draft_path=args.draft,
            research_question=question,
            chapter_roles=chapter_roles,
            out_dir=args.output,
            context=context,
            parts_fixture_path=args.fixture,
            recordings=load_recordings(args.recordings) if args.recordings else None,
            language=args.language,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("status") == "generated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
