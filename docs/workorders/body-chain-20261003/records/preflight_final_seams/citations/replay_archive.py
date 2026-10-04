"""Provider-free citation bookkeeping replay; never calls a writer model.

Run from repository root with PYTHONPATH=/tmp/optomind-stage2-deps:.
Use --baseline to load the frozen source from the archive commit, not to
modify the working tree. Original archived evidence is never rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import types


def deny_network(*args, **kwargs):
    raise AssertionError('network forbidden in offline citation replay')


socket.socket.connect = deny_network
socket.create_connection = deny_network

from optomind_research.runtime.upgrade3 import review_unit_writer as current_writer
from scripts.upgrade3 import full_review_draft as assembler

ROOT = Path(__file__).resolve().parents[6]
ARCHIVE = ROOT / 'docs/acceptance/preflight-materials-local-20261004/live_writer_qwen37_single_v2/CH02_U3'
BASELINE = '4b115e25b900c7832996b4c8213dc657109151d5'
SOURCE = 'optomind_research/runtime/upgrade3/review_unit_writer.py'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--test', action='store_true', help='Run targeted tests with the selected writer source')
    args = parser.parse_args()
    writer = current_writer
    if args.baseline:
        source = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT, text=True)
        writer = types.ModuleType('optomind_research.runtime.upgrade3._citation_replay_baseline')
        writer.__file__ = str(ROOT / SOURCE)
        writer.__package__ = 'optomind_research.runtime.upgrade3'
        sys.modules[writer.__name__] = writer
        exec(compile(source, writer.__file__, 'exec'), writer.__dict__)
    else:
        source = (ROOT / SOURCE).read_text(encoding='utf-8')
    if args.test:
        import pytest
        from optomind_research.runtime import upgrade3
        upgrade3.review_unit_writer = writer
        raise SystemExit(pytest.main(['-q', 'tests/upgrade3/test_preflight_final_citations.py']))
    saved = json.loads((ARCHIVE / 'UNIT_RESULT.json').read_text(encoding='utf-8'))
    inputs = json.loads((ARCHIVE / 'UNIT_INPUT.json').read_text(encoding='utf-8'))
    messages = json.loads((ARCHIVE / 'UNIT_MESSAGES.json').read_text(encoding='utf-8'))
    payload = json.loads(messages[1]['content'])
    view = writer.UnitWritingView(
        chapter_id=inputs['chapter_id'], unit_id=inputs['unit_id'], focus='',
        unit_index=0, sibling_units=[], unit_count=1,
        chapter_frame=payload['chapter_frame'], other_chapters=[],
        paragraph_tasks=payload['paragraph_tasks'], table_tasks=payload['table_tasks'],
        materials=inputs['materials'], material_notes=inputs['material_notes'],
        warnings=inputs['warnings'],
        sources={row['source_handle']: row for row in inputs['materials']},
    )
    out = Path(__file__).resolve().parent / ('before' if args.baseline else 'after')
    body = saved['body_markdown']
    report = writer.write_unit_output(
        view, body, out.relative_to(ROOT), model=saved['model'], language='zh',
        mode='archive-replay', used_messages=messages, estimate={},
    )
    body_bytes = (ARCHIVE / 'UNIT_BODY.md').read_bytes()
    identity = assembler.IdentityIndex([view.sources])
    order, unknown = assembler.scan_citations(body, identity)
    numbered = assembler.replace_citations_numbered(body, identity, dict(zip(order, range(1, len(order) + 1))))
    summary = {
        'provider_calls': 0, 'retrieval_calls': 0, 'network_guard': 'socket connections denied',
        'baseline_commit': BASELINE, 'baseline_source_loaded': args.baseline,
        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'source_artifact_hashes': {name: hashlib.sha256((ARCHIVE / name).read_bytes()).hexdigest()
            for name in ['UNIT_BODY.md', 'UNIT_RESULT.json', 'UNIT_INPUT.json', 'UNIT_MESSAGES.json']},
        'minimal_example': writer.citations_in('x[参考单元论证][P0602]'),
        'archived_report_used': saved['used_source_handles'],
        'literal_canonical_handles': sorted(set(re.findall(r'\[(P\d{3,})\]', body))),
        'recomputed_used': report['used_source_handles'],
        'recomputed_unused': report['unused_source_handles'],
        'recomputed_unknown': report['unknown_citations'],
        'saved_body_bytes_unchanged': (out / 'UNIT_BODY.md').read_bytes() == body_bytes,
        'report_body_unchanged': report['body_markdown'] == body,
        'downstream_first_appearance_order': order,
        'downstream_unknown': unknown,
        'downstream_numbering_preserves_explanatory_brackets': '[参考单元论证][1][2]' in numbered,
        'downstream_remaining_handles': assembler.citation_handles(numbered),
        'scope': 'Bookkeeping-only reconstruction from archived messages/input; no model rerun, no prompt or body edits. Scientific correctness and explanatory placeholders are not repaired.',
    }
    assert summary['saved_body_bytes_unchanged'] and summary['report_body_unchanged']
    (out / 'SUMMARY.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Body duplication is unnecessary: its archive hash and equality checks
    # above are retained alongside the actual persisted result report.
    (out / 'UNIT_BODY.md').unlink()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
