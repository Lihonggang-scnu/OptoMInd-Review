"""Offline committed-card adoption; never acquires material or calls a model."""
from __future__ import annotations
import argparse
import json
from optomind_research.runtime.upgrade3.planning_pool_adapter import publish_planning_pool


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-state', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--select-work-key', action='append', default=None)
    parser.add_argument('--legacy-pool')
    args = parser.parse_args(argv)
    report = publish_planning_pool(batch_state_path=args.batch_state, output_dir=args.output_dir,
                                  selected_work_keys=args.select_work_key, legacy_pool_path=args.legacy_pool)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
