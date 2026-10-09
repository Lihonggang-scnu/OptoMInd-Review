"""Free cross-domain wiring tests, not a model prose-quality evaluation."""
from copy import deepcopy
import json

import pytest

from test_guided_body_writer import config, offline, meter
from optomind_research.runtime.upgrade3.guide_maker_contracts import compile_guide_input, build_maker_payload, parse_maker_response
from optomind_research.runtime.upgrade3.guided_body_writer import run_guided_body


def synthetic_book(domain, chapter_count):
    chapters = []
    for i in range(chapter_count):
        cid, source = f'{domain}-{i}', f'{domain}-source-{i}'
        units = []
        for j in range(2):
            units.append({'unit_id': f'original-{j}', 'focus': f'{domain} question {j}',
                'unit_notes': {'domain_specific_extension': [domain, j]},
                'owner_unit_context': {'conditions': ['setting-dependent', 'negative observation']},
                'paragraph_tasks': [{'paragraph_id': f'p-{j}-{k}',
                    'point': f'{domain} independent explanation {j}-{k}',
                    'development': {'argument': 'Compare conditions before interpreting outcomes',
                        'negative_condition': {'threshold': j + k, 'result': 'no improvement'}},
                    'source_handles': [source],
                    'source_uses': [{'source_handle': source, 'role': 'comparison' if k else 'explanation',
                        'use': f'different role {j}-{k}'}],
                    'source_brief_details': [{'source_handle': source, 'independent_claim': f'claim-{j}-{k}',
                        'extra_science': {'nested': [domain, {'preserve': True}]}}]}
                    for k in range(2)],
                'table_tasks': ([{'table_id': 'comparison-table', 'purpose': f'{domain} condition comparison',
                    'source_handles': [source], 'columns': ['condition', 'outcome', 'negative boundary'],
                    'row_tasks': [{'label': 'boundary case', 'source_handles': [source],
                        'extra_science': {'unit': 'domain-defined', 'null_result': True}}]}] if j else [])})
        chapters.append({'chapter_id': cid, 'chapter_frame': {'title': cid, 'purpose': f'Explain {domain}'},
            'units': units, 'sources': [{'source_handle': source, 'title': f'{domain} study {i}',
                'study_summary_A': {'finding': 'Conditional improvement',
                    'negative_condition': 'No improvement outside the tested setting',
                    'extra_science': {'preserve_unknown': [1, 2, {'domain': domain}]}}}]})
    return {'chapters': chapters, 'research_question': f'Compare approaches in {domain}'}


@pytest.mark.parametrize('domain,chapter_count,units_per_chapter', [
    ('material-design', 2, 3), ('education-methods', 3, 4)])
def test_cross_domain_grouped_guide_to_continuous_body(tmp_path, config, domain, chapter_count, units_per_chapter):
    book = synthetic_book(domain, chapter_count)
    original = deepcopy(book)
    bundle = compile_guide_input(book)
    maker_input = build_maker_payload(bundle)
    pack = bundle['science_archive']
    guide = {'manuscript_guide': 'Develop independent explanations and compare their applicable conditions.', 'chapters': []}
    expected = []
    for chapter in book['chapters']:
        cid = chapter['chapter_id']
        ids = [tid for tid, task in pack['tasks'].items() if task['chapter_id'] == cid]
        # Merge tasks across original units while splitting both original units.
        groups = [[ids[0], ids[2]], [ids[1], ids[4]], [ids[3]]] if units_per_chapter == 3 else [
            [ids[0], ids[2]], [ids[1]], [ids[3]], [ids[4]]]
        writing_units = [{'unit_id': f'argument-{j}', 'title': f'Argument {j}',
            'writing_arrangement': 'Relate the assigned explanation and comparison to actual preceding text.',
            'content_task_ids': group} for j, group in enumerate(groups)]
        guide['chapters'].append({'chapter_id': cid, 'title': cid,
            'writing_arrangement': 'Explain the contrast, then interpret boundaries.', 'writing_units': writing_units})
        expected.extend(groups)
    parsed = parse_maker_response({'guide': guide, 'reading_needs': [], 'complete': True, 'changes': []}, book, bundle)
    assert parsed['complete'] and not parsed['assembly_warnings']
    assert len(maker_input['content_task_catalog']) == chapter_count * 5
    assert len({pack['tasks'][tid]['unit_id'] for tid in expected[0]}) == 2
    for original_unit in ('original-0', 'original-1'):
        assert sum(any(pack['tasks'][tid]['unit_id'] == original_unit for tid in group)
                   for group in expected[:units_per_chapter]) >= 2

    class Writer:
        execution_mode = 'recording'
        fixture_sha256 = 'cross-domain-free-v1'
        def __init__(self):
            self.calls, self.prose = [], []
        def __call__(self, role, directory, profile):
            def call(messages, **kwargs):
                payload = json.loads(messages[-1]['content'])
                index = len(self.calls)
                assert payload['accepted_body_markdown'] == '\n\n'.join(self.prose)
                basis = payload['chapter_assignment']['content_basis']
                assert [row['task_id'] for row in basis] == expected[index]
                for row in basis:
                    assert row == {'task_id': row['task_id'], **pack['tasks'][row['task_id']]}
                assert domain in json.dumps(payload['materials'])
                assert 'preserve_unknown' in json.dumps(payload['materials'])
                self.calls.append(payload)
                body = f'Accepted explanation {index} for {domain}.'
                self.prose.append(body)
                return {'body_markdown': body, 'complete': True, 'remaining_content': []}
            return call
    writer = Writer()
    result = run_guided_body(book, parsed['guide'], tmp_path / domain, config,
        client_factory=writer, run=True, token_counter=meter)
    assert result['complete'], result
    assert len(writer.calls) == chapter_count * units_per_chapter
    assert result['body_markdown'] == '\n\n'.join(writer.prose)
    assert result['completed_chapter_ids'] == [c['chapter_id'] for c in book['chapters']]
    assert book == original
