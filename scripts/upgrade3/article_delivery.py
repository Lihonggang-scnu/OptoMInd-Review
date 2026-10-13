"""Offline full-Article delivery preview or explicit saved-response replay."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from optomind_research.runtime.upgrade3.article_delivery import run_article_delivery


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--article-report', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    source = Path(args.article_report).resolve()
    article = json.loads(source.read_text())
    selected = article.get('selected_manuscript') or {}
    if selected.get('path') and not Path(selected['path']).is_absolute():
        selected['path'] = str(source.parent / selected['path'])
    report = run_article_delivery(article_report=article, config=json.loads(Path(args.config).read_text()), out_dir=args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
