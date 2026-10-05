"""Prepare blinded local-agent packets, then decode validated assessments offline."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from optomind_research.runtime.upgrade3.post_body_revision_evaluation import prepare, report, read


def write_report_output(path, result, evaluation_dir, judgments):
    """UTF-8 report only; never mutate inputs or replace a different artifact."""
    target = Path(path).resolve()
    directory = Path(evaluation_dir).resolve()
    judgment_path = Path(judgments).resolve()
    manifest = read(directory / "manifest.json")
    protected = {(directory / name).resolve() for name in manifest["files"]}
    protected.add(directory / "manifest.json")
    packets = directory / "packets"
    if (target in protected or target == judgment_path or target == packets
            or packets in target.parents
            or (judgment_path.is_dir() and judgment_path in target.parents)):
        raise ValueError("report_output_overlaps_evaluation_or_judgment_input")
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if target.exists():
        if not target.is_file() or target.read_bytes() != data:
            raise ValueError("report_output_exists_with_different_content")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the check/write race; no overwrite flag by design.
    with target.open("xb") as handle:
        handle.write(data)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("prepare")
    a.add_argument("--case", required=True)
    a.add_argument("--runs", nargs=3, required=True)
    a.add_argument("--output-dir", required=True)
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--swap-fraction", type=float, default=0)
    a.add_argument("--resume", action="store_true")
    b = sub.add_parser("report")
    b.add_argument("--evaluation-dir", required=True)
    b.add_argument("--judgments", required=True)
    b.add_argument("--evaluation-cost-cny", type=float)
    b.add_argument("--output", help="Write UTF-8 JSON; an existing identical report is accepted, different content is never overwritten")
    args = p.parse_args(argv)
    try:
        result = prepare(args.case, args.runs, args.output_dir, args.seed, args.swap_fraction, args.resume) if args.command == "prepare" else report(args.evaluation_dir, args.judgments, args.evaluation_cost_cny)
        if args.command == "report" and args.output:
            write_report_output(args.output, result, args.evaluation_dir, args.judgments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
