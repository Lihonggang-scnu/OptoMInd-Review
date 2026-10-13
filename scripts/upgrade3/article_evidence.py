"""Configured source-bound Article evidence service; no calls without --replay/--run."""
import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import json
from pathlib import Path
from optomind_research.runtime.upgrade3.evidence_service import create_evidence_service
from optomind_research.runtime.upgrade3.article_evidence import EvidenceRequest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--replay', action='store_true')
    modes.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    path = Path(args.config).resolve()
    config = json.loads(path.read_text())
    config['mode'] = 'run' if args.run else 'replay' if args.replay else 'preview'
    request = EvidenceRequest.from_mapping(json.loads(Path(args.request).read_text()))
    service = create_evidence_service(config, root=path.parent)
    result = service.request(request).to_dict() if args.run or args.replay else service.preview(request)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
