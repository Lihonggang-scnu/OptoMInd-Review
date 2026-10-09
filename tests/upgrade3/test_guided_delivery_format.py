"""Delivery-only wrapper repair: deterministic, conservative, raw-preserving."""
import json
from pathlib import Path
import re

import pytest

from optomind_research.runtime.upgrade3.guided_body_contracts import (
    adapt_delivery_chapter, normalize_citation_handles, parse_guided_response,
)
from test_guided_body_writer import book, guide, config, offline, execute, Factory


PROSE = 'The observations support a conditional association, with important limits (P0001).'


@pytest.mark.parametrize('language', ['markdown', 'md'])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_exact_wrapper_only_removed_and_idempotent(language, newline):
    inside = '# Chapter 1\n\n' + PROSE + '\n\n'
    original = '\n```' + language + '\n' + inside + '```\n\n'
    original = original.replace('\n', newline)
    adapted, audit = adapt_delivery_chapter(original, 'Chapter 1')
    assert adapted == ('\n' + inside + '\n').replace('\n', newline)
    assert audit == {'outer_markdown_fence_removed': True, 'diagnostics': []}
    assert adapt_delivery_chapter(adapted, 'Chapter 1')[0] == adapted
    assert re.findall(r'P\d{4}', original) == re.findall(r'P\d{4}', adapted)


@pytest.mark.parametrize('original', [
    '# Chapter 1\n\n' + PROSE + '\n```markdown\n# Example\n```\n',
    '```python\n# Chapter 1\n\n' + PROSE + '\n```',
    '```\n# Chapter 1\n\n' + PROSE + '\n```',
    '```markdown\n# Example\n\n' + PROSE + '\n```',
    '```markdown\n# Chapter 1\n\nShort example.\n```',
    '```markdown\n# Chapter 1\n\n' + PROSE,
    '```markdown\n# Chapter 1\n\n' + PROSE + '\n```\nOutside prose.',
    '```markdown\n# Chapter 1\n\n' + PROSE + '\n```python\nP0001\n```\n```',
    '    ```markdown\n# Chapter 1\n\n' + PROSE + '\n    ```',
    '\t```markdown\n# Chapter 1\n\n' + PROSE + '\n\t```',
])
def test_code_examples_ambiguous_and_partial_fences_preserved(original):
    adapted, audit = adapt_delivery_chapter(original, 'Chapter 1')
    assert adapted == original
    assert not audit['outer_markdown_fence_removed']
    if original.startswith('```markdown'):
        assert audit['diagnostics']


def test_archived_ch7_metadata_outside_fence_preserved():
    root = Path(__file__).resolve().parents[2]
    archive = root / 'docs/acceptance/guided-body-b-repair-20261009/routes/B_repair'
    attempt = next((archive / 'stages/author_007').glob('*/attempt_001'))
    manifest = json.loads((attempt / 'RAW_RESPONSE.json.parts.json').read_text())
    raw = json.loads(''.join((attempt / part['path']).read_text() for part in manifest['parts']))
    raw_before = json.dumps(raw, ensure_ascii=False)
    parsed = parse_guided_response(raw)
    assert parsed['complete'] and not parsed['errors']
    assert parsed['body_markdown'].startswith('```markdown\n')
    assert 'guide_writer_metadata' not in parsed['body_markdown']
    full = (archive / 'FULL_BODY.md').read_text()
    assert parsed['body_markdown'].strip() in full
    title = json.loads((archive / 'GUIDE.json').read_text())['chapters'][-1]['title']
    adapted, audit = adapt_delivery_chapter(parsed['body_markdown'], title)
    assert audit['outer_markdown_fence_removed']
    assert adapted.startswith('# ' + title)
    assert '```' not in adapted
    assert re.findall(r'P\d{4}', adapted) == re.findall(r'P\d{4}', parsed['body_markdown'])
    assert json.dumps(raw, ensure_ascii=False) == raw_before
    known = list(set(re.findall(r'P\d{4}', full)))
    assert '[P0478]' in normalize_citation_handles(adapted, known)
    renderer = pytest.importorskip('markdown_it').MarkdownIt()
    assert '<pre><code class="language-markdown">' in renderer.render(parsed['body_markdown'])
    rendered = renderer.render(adapted)
    assert '<h1>' + title + '</h1>' in rendered
    assert '<pre>' not in rendered
    assert '<p>' in rendered


class WrappedFactory(Factory):
    def __init__(self, wrapped):
        super().__init__()
        self.wrapped = wrapped
        self.raw_responses = []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            self.calls.append(payload)
            number = int(payload['chapter_assignment']['chapter_id'][1:])
            prose = f'# Chapter {number}\n\n' + PROSE + '\nUnknown P9999 remains.\n'
            if number == self.wrapped:
                prose = '```markdown\n' + prose + '```\n'
            else:
                prose += '\n```python\nP0001\n```\n'
            response = {'content': prose + '\n```guide_writer_metadata\n'
                        + json.dumps({'complete': True, 'remaining_content': []}) + '\n```',
                        'complete': True, 'finish_reason': 'stop'}
            self.raw_responses.append(response)
            return response
        return call


@pytest.mark.parametrize('wrapped', [1, 2, 3])
def test_runtime_each_position_delivery_only_raw_prefix_and_unknown_preserved(
        tmp_path, book, guide, config, wrapped):
    factory = WrappedFactory(wrapped)
    result = execute(tmp_path, book, guide, config, factory)
    assert result['complete'], result
    assert result['unknown_citation_handles'] == ['P9999']
    raw_body = result['body_markdown']
    assert raw_body == '\n\n'.join(row['body_markdown'] for row in result['segments'])
    assert raw_body.count('```markdown') == 1
    delivery = Path(result['delivery_body_path']).read_text()
    assert '```markdown' not in delivery
    assert delivery.count('```python\nP0001\n```') == 2
    assert delivery.count('[P0001]') == 3
    assert delivery.count('P9999') == 3
    assert (tmp_path / 'out/FULL_BODY.md').read_text() == raw_body
    for i, segment in enumerate(result['segments']):
        assert Path(segment['full_text_path']).read_text() == segment['body_markdown']
        assert factory.calls[i]['accepted_body_markdown'] == '\n\n'.join(
            row['body_markdown'] for row in result['segments'][:i])
        attempt = Path(result['stages'][i]['attempt_dir'])
        assert (attempt / 'BODY.md').read_text() == segment['body_markdown']
        assert json.loads((attempt / 'RAW_RESPONSE.json').read_text()) == factory.raw_responses[i]
    audit = result['delivery_format_adaptation']
    assert audit['outer_markdown_fences_removed'] == 1
    assert audit['citation_token_sequence_unchanged'] and audit['raw_body_preserved']
    assert not result['delivery_citation_format_only'] and result['delivery_format_only']


def test_partial_delivery_does_not_change_status_or_raw_assembly(tmp_path, book, guide, config):
    class MissingMetadata(WrappedFactory):
        def __call__(self, role, directory, profile):
            original = super().__call__(role, directory, profile)
            def call(messages, **kwargs):
                response = original(messages, **kwargs)
                response['content'] = response['content'].split('\n```guide_writer_metadata')[0]
                return response
            return call
    factory = MissingMetadata(1)
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'metadata_unresolved'
    assert not result['complete'] and not result['body_complete']
    assert len(result['segments']) == 1 and len(factory.calls) == 1
    assert result['body_markdown'] == '\n\n'.join(row['body_markdown'] for row in result['segments'])
    assert '```markdown' in result['body_markdown']
    assert '```markdown' not in Path(result['delivery_body_path']).read_text()
    assert result['delivery_format_adaptation']['citation_token_sequence_unchanged']


def test_empty_preview_delivery_stays_empty(tmp_path, book, guide, config):
    result = execute(tmp_path, book, guide, config, run=False)
    assert result['body_markdown'] == ''
    assert Path(result['delivery_body_path']).read_text() == ''
    assert result['delivery_format_adaptation']['outer_markdown_fences_removed'] == 0
    assert result['delivery_citation_format_only']
