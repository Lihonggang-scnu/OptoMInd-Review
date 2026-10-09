"""Offline continuous guided units retain duties and exact completed prefix."""
from copy import deepcopy
import json
from test_guided_body_writer import book, guide, config, offline, execute, Factory
from optomind_research.runtime.upgrade3.guided_body_contracts import compile_guided_materials, validate_guide, build_author_payload


def unit_guide(book, guide):
    # Two independent content tasks in the first chapter, different source uses.
    first = book['chapters'][0]['units'][0]['paragraph_tasks'][0]
    first.update(development='Explain the causal control.', source_uses=[{'source_handle': 'P0001', 'use': 'causal contrast'}])
    second = deepcopy(first)
    second.update(paragraph_id='T2', point='Independent negative result', development='Explain the null boundary.')
    book['chapters'][0]['units'][0]['paragraph_tasks'].append(second)
    pack = compile_guided_materials(book)
    tids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == 'C1']
    guide['chapters'][0]['writing_units'] = [dict(unit_id=f'W{i}', title=f'Argument {i}',
        writing_arrangement='Develop one independent explanation.', content_task_ids=[tid]) for i, tid in enumerate(tids, 1)]
    return guide


class UnitFactory(Factory):
    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            self.calls.append(payload)
            a = payload['chapter_assignment']
            if 'draft_body_markdown' in a:
                obj = {'insertions': [{'after_anchor': '', 'text': '\nCompleted contrast.'}], 'complete': True, 'remaining_content': []}
            else:
                gap = a.get('unit_id') == 'W1'
                obj = {'body_markdown': '# Chapter 1\n\n' + a.get('unit_id', a['chapter_id']),
                    'complete': not gap, 'remaining_content': ['Causal contrast'] if gap else []}
            return {'content': json.dumps(obj), 'complete': True, 'finish_reason': 'stop'}
        return call


def test_units_continuous_prefix_supplement_and_cache(tmp_path, book, guide, config):
    guide = unit_guide(book, guide)
    factory = UnitFactory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['complete'], result
    assert len(factory.calls) == 5
    assert [s['stage_id'] for s in result['stages']][:3] == ['author_001_unit_001', 'complete_001_unit_001', 'author_001_unit_002']
    second = factory.calls[2]
    assert second['accepted_body_markdown'] == '# Chapter 1\n\nW1\nCompleted contrast.'
    assert second['chapter_assignment']['content_basis'][0]['task']['development'] == 'Explain the null boundary.'
    assert result['segments'][1]['repeated_chapter_heading_removed']
    assert factory.calls[3]['accepted_body_markdown'] == '# Chapter 1\n\nW1\nCompleted contrast.\n\nW2'
    assert result['completed_chapter_ids'] == ['C1', 'C2', 'C3']
    replay = execute(tmp_path, book, guide, config, factory)
    assert replay['complete'] and replay['model_calls'] == 0 and len(factory.calls) == 5


def test_unit_materials_keep_guide_added_source_and_full_exact_duties(book, guide):
    guide = unit_guide(book, guide)
    guide['chapters'][0]['source_handles'] = ['P0003']
    normalized = validate_guide(guide, book)
    unit = {**normalized['chapters'][0]['writing_units'][0], 'chapter_id': 'C1'}
    payload = build_author_payload(compile_guided_materials(book), normalized, unit, '')
    assert 'P0003' in payload['materials']['source_identities']
    assert payload['chapter_assignment']['content_basis'][0]['task']['source_uses'][0]['use'] == 'causal contrast'
    assert payload['chapter_assignment']['chapter_required_content'] == guide['chapters'][0]['required_content']


def test_unit_call_limit_stops_without_claiming_chapter_complete(tmp_path, book, guide, config):
    guide = unit_guide(book, guide)
    result = execute(tmp_path, book, guide, {**config, 'max_author_calls': 2}, UnitFactory())
    assert result['status'] == 'author_call_limit'
    assert result['completed_chapter_ids'] == []
    assert result['body_markdown'].endswith('Completed contrast.')


def test_future_unit_gap_recovery_is_local_and_does_not_trigger_completion(tmp_path, book, guide, config):
    from test_guided_metadata_recovery import RecoveryFactory, auto_config
    guide = unit_guide(book, guide)
    factory = RecoveryFactory({'body_markdown': 'Current unit finished.', 'complete': True,
        'remaining_content': ['The later unit W2 still needs its independent negative result']})
    result = execute(tmp_path, book, guide, auto_config(config), factory)
    assert result['complete'], result
    assert not any(stage['stage_id'].startswith('complete_') for stage in result['stages'])
    decisions = [payload for role, payload in factory.calls if role == 'metadata']
    assert [p['chapter_assignment']['unit_position']['index'] for p in decisions] == [1, 2]
    assert decisions[0]['chapter_assignment']['content_basis'][0]['task']['development'] == 'Explain the causal control.'
    assert result['segments'][0]['body_markdown'] == 'Current unit finished.'


def test_unknown_later_unit_source_blocks_before_any_dispatch(tmp_path, book, guide, config):
    guide = unit_guide(book, guide)
    guide['chapters'][0]['writing_units'][1]['source_handles'] = ['unknown-source']
    factory = UnitFactory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'blocked' and not factory.calls
    assert any('guided_source_unknown' in str(issue) for issue in result['issues'])


def test_split_unit_context_known_sources_receive_semantic_material(book, guide):
    guide = unit_guide(book, guide)
    context = {'supporting_studies': [{'source_handle': 'P0003',
        'use': 'Independent context for the original unit', 'extra': {'preserved': True}}]}
    book['chapters'][0]['units'][0]['owner_unit_context'] = deepcopy(context)
    normalized = validate_guide(guide, book)
    unit = {**normalized['chapters'][0]['writing_units'][0], 'chapter_id': 'C1'}
    payload = build_author_payload(compile_guided_materials(book), normalized, unit, '')
    assert payload['chapter_assignment']['content_unit_contexts']['U']['owner_unit_context'] == context
    assert 'P0003' in payload['materials']['source_identities']
    assert any(atom['source_handle'] == 'P0003' and 'INTACT_EVIDENCE_3' in str(atom['value'])
               for atom in payload['materials']['evidence_atoms'])
