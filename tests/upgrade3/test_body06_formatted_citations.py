"""Formatted paper citations are references, not general code literals."""
import importlib.util
from pathlib import Path

import pytest
from optomind_research.runtime.upgrade3 import review_unit_writer as w

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'body06_formatted_fixture',
    ROOT / 'docs/workorders/body-chain-20261003/records/body06/writer/capture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.mark.parametrize('body,expected', [
    ('Finding `[P0001]`.', ['P0001']),
    ('Finding ``[P0001][P0002]``.', ['P0001', 'P0002']),
    ('Finding `[P0001, P0002]` and [P0001].', ['P0001', 'P0002']),
    ('| Study | Result |\n|---|---|\n| `[P0001]` | 8 ms |', ['P0001']),
    ('Finding ` [P0001] `.', ['P0001']),
    ('Finding `[P9999]`.', ['P9999']),
])
def test_pure_formatted_paper_references_are_counted(body, expected):
    assert w.citations_in(body) == expected
    assert w.parse_unit_body({'body_markdown': body}) == body


@pytest.mark.parametrize('body', [
    'Code `lookup("[P0001]")`.',
    'Code `[1]`.',
    '```python\n[P0001]\n```',
    'Example:\n    `[P0001]`',
    '[label](https://example.invalid/`[P0001]`)',
    '![P0001](images/figure.png)',
    'Escaped `\\[P0001]`.',
    '```markdown\n`[P0001]`\n```',
])
def test_actual_code_links_and_escaped_references_stay_protected(body):
    assert w.citations_in(body) == []
    repaired, changes = w._repair_numeric_citations(
        body, {'1': 'P0001'}, ['P0001'])
    assert repaired == body
    assert changes == []


def test_ordinary_written_report_keeps_formatted_sources(tmp_path):
    body = 'Finding `[P0001]` and `[P9999]`.\n\n' + fixture.TABLE
    result = w.run_unit_writing(fixture.view(), client=fixture.Fake(
        {'body_markdown': body}), model='offline-synthetic')
    report = w.write_unit_output(fixture.view(), result['body_markdown'], tmp_path,
        model='offline-synthetic', language='en', mode='live',
        used_messages=result['messages'], estimate={}, issues=result['issues'])
    assert result['used_source_handles'] == report['used_source_handles']
    assert 'P0001' in report['used_source_handles']
    assert report['unknown_citations'] == ['P9999']
    assert (tmp_path / 'UNIT_BODY.md').read_text(encoding='utf-8') == body + '\n'


def test_completion_written_report_keeps_formatted_sources(tmp_path):
    body = 'New finding `[P0002]` and `[P9999]`.\n\n' + fixture.TABLE
    result = w.run_unit_completion(fixture.view(), existing_body=fixture.PREFIX,
        task_ids=['T1'], client=fixture.Fake({'body_markdown': body,
            'status': 'appended', 'complete': True}), model='offline-synthetic')
    report = w.write_unit_completion(fixture.view(), result, tmp_path,
        estimate={}, language='en')
    assert not result['pending']
    assert result['used_source_handles'] == report['used_source_handles']
    assert report['unknown_citations'] == ['P9999']
    assert (tmp_path / 'COMPLETED_BODY.md').read_bytes().startswith(fixture.PREFIX.encode())
    assert result['completion_fragment'] == body
