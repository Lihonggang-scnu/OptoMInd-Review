"""Offline hashed frozen-baseline/new-route benchmark, no provider calls."""
import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import json
from optomind_research.runtime.upgrade3.coverage_evaluation import evaluate_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(evaluate_manifest(args.manifest), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
