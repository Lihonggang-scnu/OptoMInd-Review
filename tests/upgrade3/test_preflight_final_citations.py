"""Archived writer citation consumption, with no model or material retrieval."""
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from scripts.upgrade3 import full_review_draft as assembler

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/acceptance/preflight-materials-local-20261004/live_writer_qwen37_single_v2/CH02_U3'
EXPECTED = ['P0564', 'P0576', 'P0602', 'P0561', 'P0388', 'P0190', 'P0582']


def archived_view():
    saved = json.loads((ARCHIVE / 'UNIT_INPUT.json').read_text(encoding='utf-8'))
    messages = json.loads((ARCHIVE / 'UNIT_MESSAGES.json').read_text(encoding='utf-8'))
    payload = json.loads(messages[1]['content'])
    return writer.UnitWritingView(
        chapter_id=saved['chapter_id'], unit_id=saved['unit_id'], focus='',
        unit_index=0, sibling_units=[], unit_count=1,
        chapter_frame=payload['chapter_frame'], other_chapters=[],
        paragraph_tasks=payload['paragraph_tasks'], table_tasks=payload['table_tasks'],
        materials=saved['materials'], material_notes=saved['material_notes'],
        warnings=saved['warnings'],
        sources={row['source_handle']: row for row in saved['materials']},
    ), messages


@pytest.mark.parametrize('body,expected', [
    ('x[参考单元论证][P0602]', ['P0602']),
    ('x[explanation][P0602][P0564][P0602]', ['P0602', 'P0564']),
    ('x[P0564][P0576]', ['P0564', 'P0576']),
    ('x[参考单元论证][P9999]', ['P9999']),
    ('[explanation][P0602, P0564]', ['P0602', 'P0564']),
])
def test_explanatory_brackets_do_not_hide_canonical_citations(body, expected):
    assert writer.citations_in(body) == expected


@pytest.mark.parametrize('body', [
    '[label][P0602]\n\n[P0602]: https://example.invalid',
    '[P0602][link]\n\n[link]: https://example.invalid',
    '[P0602][]\n\n[P0602]: https://example.invalid',
    '[P0602]\n\n[P0602]: https://example.invalid',
    '![figure][P0602]\n\n[P0602]: image.png',
    '![P0602](image.png)',
    '![figure][P0602]',
    '![P0602][P0564]',
    '[P0602](https://example.invalid)',
    '`lookup("[label][P0602]")`',
    '```text\n[label][P0602]\n```',
    '    [label][P0602]',
    r'\[P0602]',
])
def test_real_links_images_and_code_stay_protected(body):
    assert writer.citations_in(body) == []


def test_numeric_repairs_remain_conservative():
    body = '[label][1] and [1][P0602]'
    repaired, _ = writer._repair_numeric_citations(body, {'1': 'P0564'}, ['P0564', 'P0602'])
    assert repaired == '[label][1] and [P0564][P0602]'


def test_archived_body_report_and_downstream_numbering(tmp_path):
    saved = json.loads((ARCHIVE / 'UNIT_RESULT.json').read_text(encoding='utf-8'))
    body = saved['body_markdown']
    view, messages = archived_view()
    assert saved['used_source_handles'] == ['P0576']
    assert (body.rstrip() + '\n').encode() == (ARCHIVE / 'UNIT_BODY.md').read_bytes()
    report = writer.write_unit_output(
        view, body, tmp_path, model=saved['model'], language='zh',
        mode='archive-replay', used_messages=messages, estimate={},
    )
    assert report['used_source_handles'] == EXPECTED
    assert report['unused_source_handles'] == []
    assert report['unknown_citations'] == []
    assert report['body_markdown'] == body
    assert (tmp_path / 'UNIT_BODY.md').read_bytes() == (ARCHIVE / 'UNIT_BODY.md').read_bytes()
    assert json.loads((tmp_path / 'UNIT_RESULT.json').read_text()) == report
    identity = assembler.IdentityIndex([view.sources])
    order, unknown = assembler.scan_citations(body, identity)
    assert order == EXPECTED
    assert unknown == []
    numbered = assembler.replace_citations_numbered(body, identity, dict(zip(order, range(1, 8))))
    assert '[参考单元论证][1][2]' in numbered
    assert '[参考单元论证][3]' in numbered
    assert assembler.citation_handles(numbered) == []


def test_unknown_canonical_handle_stays_reported(tmp_path):
    view, messages = archived_view()
    body = 'Claim[参考单元论证][P9999]'
    report = writer.write_unit_output(
        view, body, tmp_path, model='offline', language='zh',
        mode='archive-replay', used_messages=messages, estimate={},
    )
    assert report['used_source_handles'] == ['P9999']
    assert report['unknown_citations'] == ['P9999']
    assert report['body_markdown'] == body
