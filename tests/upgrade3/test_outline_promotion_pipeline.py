"""Formal optional-module orchestration with only model clients replaced."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import pytest
from scripts.upgrade3 import outline_strengthening_pipeline as pipeline
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening


def fixture_root(tmp_path):
    units = [{'unit_id': f'CH01_U{i}', 'substantive_point': f'Original duty {i}',
              'paragraph_briefs': [{'paragraph_id': f'CH01_U{i}_P1', 'point': f'Task {i}',
                                   'development': 'Preserve the experiment condition', 'source_handles': ['P0001']}],
              'supporting_studies': [{'source_handle': 'P0001', 'use': 'Original useful comparison'}]}
             for i in (1, 2, 3)]
    source = {'source_handle': 'P0001', 'paper_id': 'optical-1', 'title': 'Synthetic optical study',
              'doi': '10.1000/optical', 'study_summary_A': {'finding': 'Polarization-dependent response', 'conditions': 'Oblique incidence'},
              'review_planning_B': {'planning_summary': 'Compare response at matched angles'}}
    packet = strengthening.build_strengthening_payload(research_question='Which optical response generalizes?',
        chapter_id='CH01', chapter={'chapter_id': 'CH01', 'title': 'Mechanisms'},
        chapter_plan={'chapter_id': 'CH01', 'thesis': 'Preserve conditional reasoning', 'units': units},
        source_materials=[source], shared_outline=[{'chapter_id': 'CH01', 'title': 'Mechanisms'}])
    root = tmp_path / 'original'
    pipeline._dump(root / 'writer_packets/CH01.json', packet)
    pipeline._dump(root / 'DETAILED_REVIEW_PLAN.json', {
        'research_question': packet['research_question'], 'shared_outline': packet['shared_outline'],
        'review_argument': 'Geometry and angle jointly bound response', 'chapters': [packet],
        'writer_packets': [{'chapter_id': 'CH01', 'json_path': 'writer_packets/CH01.json'}],
        'planning_tool_results': {'unchanged': 'full upstream record'}})
    return root, packet


def args_for(root, out):
    return argparse.Namespace(packet_root=str(root), output=str(out), run=True,
        selection_profile='outline_selection', access_profile='autonomous_outline', profile='strong_outline',
        tokenizer='', budget_ledger=str(out / 'offline.sqlite'), budget_limit=30., key_file='unused-test-boundary',
        include_selection_material_index=False)


def fake_factory(monkeypatch, packet, *, empty=False, fail_owner=False, two_groups=False):
    calls = []
    def create_client(*, role, **kwargs):
        def client(messages, **effective):
            calls.append((role, copy.deepcopy(messages), effective))
            if role == 'outline_selection':
                ids = ['CH01_U1', 'CH01_U3'] if two_groups else ['CH01_U1']
                parsed = {'status': 'no_change', 'groups': []} if empty else {
                    'status': 'selected', 'groups': [
                        {'unit_ids': [uid], 'selection_reason': 'Necessary local depth',
                         'improvement_focus': ['Explain the relation rather than list names']} for uid in ids]}
            elif role == 'autonomous_outline':
                parsed = {'status': 'access_plan', 'material_requests': [{'source_handle': 'P0001'}]}
            else:
                if fail_owner:
                    raise RuntimeError('controlled_owner_interruption')
                # Derive the actual editable identity from the real owner payload.
                user = messages[-1]['content']; decoder = json.JSONDecoder()
                data = decoder.raw_decode(user[user.index('{'):])[0]
                uid = data['modifiable_unit_ids'][0]
                unit = copy.deepcopy(next(u for u in packet['chapter_plan']['units'] if u['unit_id'] == uid))
                unit['paragraph_briefs'][0]['development'] = 'Explain relation at matched angle; retain oblique-incidence boundary'
                parsed = {'status': 'updated', 'chapter_updates': [{'chapter_id': 'CH01',
                           'updated_plan': {'chapter_id': 'CH01', 'units': [unit]}}], 'unit_id_remap': {}}
            return {'content': json.dumps(parsed), 'complete': True, 'finish_reason': 'stop',
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 8}}
        return client
    monkeypatch.setattr(pipeline.cli.strengthening, 'make_strengthening_client', create_client)
    return calls


def test_formal_pipeline_retains_full_chapter_and_arrangement_cli_consumes(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    calls = fake_factory(monkeypatch, packet)
    report = pipeline.run_pipeline(args_for(root, out))
    assert report['status'] == 'complete', report
    final_root = Path(report['packet_root'])
    final = pipeline._load(final_root / 'writer_packets/CH01.json')
    assert [u['unit_id'] for u in final['chapter_plan']['units']] == ['CH01_U1', 'CH01_U2', 'CH01_U3']
    assert final['chapter_plan']['units'][1:] == packet['chapter_plan']['units'][1:]
    assert final['source_materials'][0]['study_summary_A'] == packet['source_materials'][0]['study_summary_A']
    assert pipeline._load(root / 'writer_packets/CH01.json') == packet
    assert arrangement_cli.main(['--packet-root', str(final_root), '--output-root', str(tmp_path / 'arrangement'), '--chapter', 'CH01']) == 0
    actual = (tmp_path / 'arrangement/CH01/ARRANGEMENT_INPUT.json').read_text()
    assert 'matched angle' in actual and 'Original duty 2' in actual
    before = len(calls)
    again = pipeline.run_pipeline(args_for(root, out))
    assert again['status'] == 'complete'
    assert len(calls) == before  # all existing production stage checkpoints reused


def test_no_selection_exports_unchanged_complete_plan(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    calls = fake_factory(monkeypatch, packet, empty=True)
    report = pipeline.run_pipeline(args_for(root, out))
    assert report['status'] == 'complete'
    final = pipeline._load(Path(report['packet_root']) / 'writer_packets/CH01.json')
    assert final['chapter_plan'] == packet['chapter_plan']
    assert [c[0] for c in calls] == ['outline_selection']


def test_failed_owner_preserves_complete_base_and_reuses_access(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    first_calls = fake_factory(monkeypatch, packet, fail_owner=True)
    failed = pipeline.run_pipeline(args_for(root, out))
    assert failed['status'] == 'partial'
    unchanged = pipeline._load(Path(failed['packet_root']) / 'writer_packets/CH01.json')
    assert unchanged['chapter_plan'] == packet['chapter_plan']
    second_calls = fake_factory(monkeypatch, packet)
    recovered = pipeline.run_pipeline(args_for(root, out))
    assert recovered['status'] == 'complete'
    assert [c[0] for c in second_calls] == ['strong_outline']


def test_mutated_original_input_requires_separate_output(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    fake_factory(monkeypatch, packet, empty=True)
    pipeline.run_pipeline(args_for(root, out))
    packet['chapter_plan']['units'][0]['substantive_point'] = 'New scientific task'
    packet['input_integrity'] = strengthening._input_integrity(packet)
    pipeline._dump(root / 'writer_packets/CH01.json', packet)
    with pytest.raises(ValueError, match='pipeline_input_changed'):
        pipeline.run_pipeline(args_for(root, out))


def test_multiple_groups_use_latest_readonly_roles(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    calls = fake_factory(monkeypatch, packet, two_groups=True)
    report = pipeline.run_pipeline(args_for(root, out))
    assert report['status'] == 'complete', report
    owners = [c for c in calls if c[0] == 'strong_outline']
    assert len(owners) == 2
    final = pipeline._load(Path(report['packet_root']) / 'writer_packets/CH01.json')
    assert 'matched angle' in final['chapter_plan']['units'][0]['paragraph_briefs'][0]['development']
    assert 'matched angle' in final['chapter_plan']['units'][2]['paragraph_briefs'][0]['development']
    assert final['chapter_plan']['units'][1] == packet['chapter_plan']['units'][1]


def test_later_readonly_reference_follows_previous_split_without_editing_it():
    packets = {'CH01': {'chapter_plan': {'units': [{'unit_id': 'U1a'}, {'unit_id': 'U1b'}, {'unit_id': 'U2'}]},
                       'unit_id_remap': {'U1a': ['U1'], 'U1b': ['U1']}}}
    group = {'unit_ids': ['U2'], 'related_read_only_unit_ids': ['U1']}
    current = pipeline._current_group(group, packets)
    assert current['unit_ids'] == ['U2']
    assert current['related_read_only_unit_ids'] == ['U1a', 'U1b']
    assert group['related_read_only_unit_ids'] == ['U1']


def test_readonly_lineage_includes_split_siblings_when_old_id_survives():
    packets = {'CH01': {'chapter_plan': {'units': [{'unit_id': 'U1'}, {'unit_id': 'U1a'}]},
                       'unit_id_remap': {'U1a': ['U1']}},
               'CH02': {'chapter_plan': {'units': [{'unit_id': 'U2'}]}}}
    result = pipeline._current_group({'unit_ids': ['U2'], 'related_read_only_unit_ids': ['U1']}, packets)
    assert result['related_read_only_unit_ids'] == ['U1', 'U1a']


def test_recorded_owner_return_through_formal_pipeline_and_arranger_writer(tmp_path, monkeypatch):
    """Real saved OWNER answer; controlled selector/access/arrangement boundaries.

    The 18 public selected records are complete within the sanitized export.
    One synthetic unselected unit tests retention. This is not reconstruction
    of the unavailable full 367-record pool or historical complete chapter.
    """
    from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
    from scripts.upgrade3 import review_unit_writer as writer_cli
    archive = Path(__file__).resolve().parents[2] / 'docs/verification/on-demand-efficiency-20261007'
    text = pipeline._load(archive / 'integration_handoff/OWNER_MESSAGES.json')['messages'][1]['content']
    recorded = strengthening.expand_material_projection(json.JSONDecoder().raw_decode(text[text.index('{'):])[0])
    original = recorded['chapter_plan']
    response = pipeline._load(archive / 'integration_handoff/OWNER_RESPONSE.json')['model_output']['parsed_output']
    untouched = {'unit_id': 'Ch2_CONTROL', 'substantive_point': 'Synthetic retention control',
                 'paragraph_briefs': [{'paragraph_id': 'CONTROL_P1', 'point': 'Keep fixture control',
                                       'development': 'Structural fixture only', 'source_handles': []}]}
    packet = strengthening.build_strengthening_payload(
        research_question=recorded['research_question'], chapter_id='Ch2', chapter=recorded['chapter'],
        chapter_plan={**copy.deepcopy(original), 'units': [original['units'][0], untouched, original['units'][1]]},
        source_materials=recorded['source_materials'], source_identity_map=recorded['source_identity_map'],
        shared_outline=[{'chapter_id': 'Ch2', 'title': 'Recorded chapter fixture'}],
        full_chapter_context=recorded['full_chapter_context'],
        readonly_neighbor_unit_roles=recorded['readonly_neighbor_unit_roles'],
        shared_scope=recorded['shared_scope'], review_argument=recorded['review_argument'])
    root = tmp_path / 'original'; out = tmp_path / 'optional'
    pipeline._dump(root / 'writer_packets/Ch2.json', packet)
    pipeline._dump(root / 'DETAILED_REVIEW_PLAN.json', {'shared_outline': packet['shared_outline'],
        'chapters': [packet], 'writer_packets': [{'chapter_id': 'Ch2', 'json_path': 'writer_packets/Ch2.json'}]})
    captured = []
    def factory(*, role, **kwargs):
        def client(messages, **effective):
            captured.append({'role': role, 'messages': copy.deepcopy(messages), 'effective': effective})
            if role == 'outline_selection':
                parsed = {'status': 'selected', 'groups': [{'unit_ids': ['Ch2_U01', 'Ch2_U03'],
                    'selection_reason': 'Controlled selection for consumer replay',
                    'improvement_focus': ['Preserve explicit conditional explanation']} ]}
            elif role == 'autonomous_outline':
                parsed = {'status': 'access_plan', 'material_requests': [
                    {'source_handle': row['source_handle']} for row in recorded['source_materials']]}
            else:
                parsed = response  # exactly the saved parsed real OWNER response
            return {'content': json.dumps(parsed, ensure_ascii=False), 'complete': True, 'finish_reason': 'stop'}
        return client
    monkeypatch.setattr(pipeline.cli.strengthening, 'make_strengthening_client', factory)
    report = pipeline.run_pipeline(args_for(root, out))
    assert report['status'] == 'complete', report
    final_root = Path(report['packet_root'])
    final_packet = pipeline._load(final_root / 'writer_packets/Ch2.json')
    assert final_packet['chapter_plan']['units'][1] == untouched
    assert [u for u in final_packet['chapter_plan']['units'] if u['unit_id'] != 'Ch2_CONTROL'] == response['chapter_updates'][0]['updated_plan']['units']
    view = arranging.build_chapter_view(final_root / 'writer_packets/Ch2.json')
    controlled = {'chapter_id': 'Ch2', 'units': [
        {'unit_id': u.unit_id, 'paragraph_tasks': [
            {'paragraph_id': b.paragraph_id, 'source_briefs': [b.paragraph_id], 'point': b.point, 'development': b.development,
             'source_uses': [{'source_handle': h, 'role': 'support', 'use': 'Recorded owner task'} for h in b.source_handles]}
            for b in u.paragraph_briefs]} for u in view.units]}
    arranged_response = tmp_path / 'CONTROLLED_ARRANGEMENT.json'; pipeline._dump(arranged_response, controlled)
    arrangement_out = tmp_path / 'arrangement'
    assert arrangement_cli.main(['--packet-root', str(final_root), '--output-root', str(arrangement_out),
        '--chapter', 'Ch2', '--reexport-from', str(arranged_response)]) == 0
    files = list((arrangement_out / 'Ch2').glob('*ARRANGEMENT*.json'))
    arrangement_file = next(p for p in files if p.name == 'CHAPTER_ARRANGEMENT.json')
    for unit in response['chapter_updates'][0]['updated_plan']['units']:
        writer_out = tmp_path / ('writer_' + unit['unit_id'])
        assert writer_cli.main(['--arrangement', str(arrangement_file), '--unit', unit['unit_id'],
                               '--output-root', str(writer_out)]) == 0
        messages_file = next(writer_out.rglob('UNIT_MESSAGES.json'))
        messages = pipeline._load(messages_file)
        payload = json.loads(messages[-1]['content'])
        for key in ('case_objects', 'supporting_studies', 'synthesis_and_transition', 'argument_relations'):
            assert payload['owner_unit_context'][key] == unit[key]
    pipeline._dump(tmp_path / 'REPLAY_ACTUAL_REQUESTS.json', captured)


def test_projection_replaces_stale_neighbor_roles_after_other_chapter_split(tmp_path):
    _, packet = fixture_root(tmp_path)
    packet['readonly_neighbor_unit_roles'] = [{'unit_id': 'CH02_U1', 'chapter_id': 'CH02', 'role': 'Obsolete duty'}]
    packet['read_only_unit_ids'] = ['CH02_U1']
    other = strengthening.build_strengthening_payload(research_question=packet['research_question'], chapter_id='CH02',
        chapter_plan={'units': [{'unit_id': 'CH02_U1A', 'substantive_point': 'Current duty A'},
                                {'unit_id': 'CH02_U1B', 'substantive_point': 'Current duty B'}]},
        source_materials=packet['source_materials'])
    other['unit_id_remap'] = {'CH02_U1A': ['CH02_U1'], 'CH02_U1B': ['CH02_U1']}
    projected = pipeline.selection.project_selection_group({'CH01': packet, 'CH02': other},
        {'unit_ids': ['CH01_U1'], 'selection_reason': 'Local explanation', 'improvement_focus': ['Conditional comparison']})[0]['payload']
    text = json.dumps(projected['readonly_neighbor_unit_roles'])
    assert 'Obsolete duty' not in text and 'Current duty A' in text and 'Current duty B' in text
    assert 'CH02_U1' not in projected['read_only_unit_ids']
    assert set(projected['read_only_unit_ids']) >= {'CH02_U1A', 'CH02_U1B'}


def test_failed_new_attempt_keeps_published_success_and_default_consumer_root(tmp_path, monkeypatch):
    root, packet = fixture_root(tmp_path); out = tmp_path / 'optional'
    fake_factory(monkeypatch, packet)
    first = pipeline.run_pipeline(args_for(root, out))
    pointer = (out / 'CURRENT_PLAN.json').read_bytes()
    owner_messages = pipeline.cli.on_demand.owner_messages
    def changed_contract(*pos, **kw):
        messages = owner_messages(*pos, **kw)
        messages[0]['content'] += '\nChanged contract fixture'
        return messages
    monkeypatch.setattr(pipeline.cli.on_demand, 'owner_messages', changed_contract)
    fake_factory(monkeypatch, packet, fail_owner=True)
    failed = pipeline.run_pipeline(args_for(root, out))
    assert failed['status'] == 'partial'
    assert failed['previous_valid_plan_preserved'] is True
    assert failed['packet_root'] == failed['adopted_packet_root'] == first['packet_root']
    assert failed['candidate_packet_root'] != failed['packet_root']
    assert (out / 'CURRENT_PLAN.json').read_bytes() == pointer
