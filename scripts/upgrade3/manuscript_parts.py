"""Generate independent manuscript parts after the completed BODY.

This entry does not run or modify BODY planning, arrangement, or writing.
It uses a labeled fixture, exact recordings, or the explicit --live production
adapter for conception -> conclusion -> introduction -> abstract/title.
--live previews exact inputs; --live --run enables budgeted physical requests.
The core provider seam and atomic assembly remain unchanged.

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
    source.add_argument("--live", action="store_true",
                        help="Budgeted production adapter; preview unless --run is also set")
    source.add_argument("--fixture", type=Path,
                        help="Labeled JSON fixture with conception/conclusion/introduction/abstract")
    source.add_argument("--recordings", type=Path,
                        help="Replay JSON with serial_parts:<stage> keys; no live fallback")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--run", action="store_true", help="Allow live physical calls; requires --live")
    parser.add_argument("--repair-invalid", action="store_true",
                        help="Explicitly allow one cached model format repair per invalid stage")
    parser.add_argument("--body-report", type=Path, help="Accepted DELIVERY_REPORT.json; validates selected draft and carries diagnostics")
    parser.add_argument("--identity-catalog", type=Path, action="append", default=[])
    parser.add_argument("--ledger", type=Path, help="Existing project ledger; never reset or initialized")
    parser.add_argument("--budget-limit", type=float, help="Original project lifetime cap")
    parser.add_argument("--account-ledger", type=Path, help="Existing shared account ledger")
    parser.add_argument("--account-budget-limit", type=float, help="Original account lifetime cap")
    parser.add_argument("--account-mapping-dir", type=Path, help="Existing shared dual receipt directory")
    parser.add_argument("--incremental-budget", type=float, default=5.0,
                        help="Additional cap fixed against the first preview's project balance")
    parser.add_argument("--thinking-budget", type=int, default=16384)
    parser.add_argument("--output-tokens", type=int, default=32768)
    parser.add_argument("--tokenizer", type=Path, help="Existing local tokenizer, no download")
    parser.add_argument("--key-file", type=Path, help="Private credential path; never read in preview")
    parser.add_argument("--key-index", type=int, default=1)
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
        args.research_question = question
        if args.run and not args.live:
            parser.error("--run requires --live")
        if args.live:
            required = ("ledger", "budget_limit", "account_ledger", "account_budget_limit", "account_mapping_dir", "tokenizer")
            if any(getattr(args, field) is None for field in required):
                parser.error("--live requires existing project/account ledgers and caps, mapping directory and local tokenizer")
            if args.key_index < 1 or (args.run and not args.key_file):
                parser.error("live --run requires --key-file and a positive --key-index")
            from scripts.upgrade3.writer_candidates import tokenizer_counter
            from optomind_research.runtime.upgrade3.serial_parts_live import run_live_parts
            counter, tokenizer_info = tokenizer_counter(args.tokenizer)
            report = run_live_parts(args=args, context=context, chapter_roles=chapter_roles,
                                    token_counter=counter, tokenizer_info=tokenizer_info)
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0 if report.get("status") in {"preview", "generated"} else 1
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
