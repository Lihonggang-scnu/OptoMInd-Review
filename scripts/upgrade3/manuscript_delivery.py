"""Finish a saved 03 manuscript through the checker and the real 04/05 stages.

Default is an offline message preview. --responses replays saved model content;
--live --run explicitly enables the existing budgeted provider. Repeat the same
command/output to resume; --retry-failed permits a recorded no-output failure
retry without resetting the two logical rounds or any budget.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parts-report", required=True, type=Path)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--responses", type=Path, help="Offline checker round_1/round_2 response mapping")
    modes.add_argument("--live", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--no-manuscript-checker", action="store_true")
    p.add_argument("--compile-pdf", action="store_true")
    p.add_argument("--ledger", type=Path)
    p.add_argument("--budget-limit", type=float)
    p.add_argument("--account-ledger", type=Path)
    p.add_argument("--account-budget-limit", type=float)
    p.add_argument("--account-mapping-dir", type=Path)
    p.add_argument("--incremental-budget", type=float, default=5.0)
    p.add_argument("--tokenizer", type=Path)
    p.add_argument("--key-file", type=Path)
    p.add_argument("--key-index", type=int, default=1)
    p.add_argument("--allow-max", action="store_true", help="Only for an explicitly authorized Max profile")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    from optomind_research.runtime.upgrade3.review_delivery import load_delivery_config
    from optomind_research.runtime.upgrade3.manuscript_delivery import run_manuscript_delivery, run_live_delivery
    from optomind_research.runtime.upgrade3.dual_budget import read_json, write_json
    try:
        if args.run and not args.live:
            p.error("--run requires --live")
        if args.retry_failed and not (args.live and args.run):
            p.error("--retry-failed requires --live --run")
        config = load_delivery_config(args.config)
        config["manuscript_checker"] = dict(config.get("manuscript_checker") or {})
        if args.no_manuscript_checker:
            config["manuscript_checker"]["enabled"] = False
        if args.compile_pdf:
            config["compile_pdf"] = True
        parts = read_json(args.parts_report)
        r3 = parts.get("runtime_report") or parts
        # Relative report paths are resolved against the supplied report, never
        # guessed by latest-file globbing. Archived absolute paths need mapping.
        if r3.get("final_manuscript"):
            source = Path(r3["final_manuscript"])
            if not source.is_absolute():
                r3["final_manuscript"] = str((args.parts_report.resolve().parent / source).resolve())
        if args.responses:
            if config["manuscript_checker"].get("fixture") or config["manuscript_checker"].get("recordings"):
                p.error("--responses conflicts with configured checker fixture/recordings")
            response = read_json(args.responses)
            config["manuscript_checker"]["recordings"] = response.get("responses", response)
        if args.live and config["manuscript_checker"].get("enabled", config.get("front_back_mode") == "post_body"):
            required = ("ledger", "budget_limit", "account_ledger", "account_budget_limit", "account_mapping_dir", "tokenizer")
            if any(getattr(args, key) is None for key in required):
                p.error("--live requires existing project/account ledgers, caps, mapping directory and local tokenizer")
            if args.key_index < 1 or (args.run and not args.key_file):
                p.error("live --run requires --key-file and positive --key-index")
            if config["manuscript_checker"].get("fixture") or config["manuscript_checker"].get("recordings"):
                p.error("--live conflicts with offline checker fixture/recordings")
            from scripts.upgrade3.writer_candidates import tokenizer_counter
            counter, info = tokenizer_counter(args.tokenizer)
            result = run_live_delivery(args=args, config=config, front_back_report=parts,
                token_counter=counter, tokenizer_info=info)
        else:
            result = run_manuscript_delivery(config=config, front_back_report=parts, out_dir=args.output)
        write_json(args.output / "MANUSCRIPT_DELIVERY_REPORT.json", result)
    except (OSError, UnicodeError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    preview = not args.run and not args.responses and not config["manuscript_checker"].get("fixture") and not config["manuscript_checker"].get("recordings")
    return 0 if result.get("downstream_status") == "complete" or (preview and result.get("halt_reasons") == ["03_manuscript_checker"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
