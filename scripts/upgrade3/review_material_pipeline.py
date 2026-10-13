"""Explicit navigation/material entry; preview is always the CLI default."""
import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import json
from pathlib import Path
from optomind_research.runtime.upgrade3.review_material_pipeline import run_material_pipeline


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--replay', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    path = Path(args.config).resolve()
    config = json.loads(path.read_text())
    config['mode'] = 'run' if args.run else 'replay' if args.replay else 'preview'
    print(json.dumps(run_material_pipeline(config, root=path.parent), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
