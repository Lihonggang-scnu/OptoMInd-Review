"""Synthetic offline producer/export/consumer coverage for BODY40's second stop."""
import copy
import json
import socket

import pytest

from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view, build_source_catalog
from optomind_research.runtime.upgrade3.review_unit_writer import (
    UnitWritingError, build_unit_view, build_completion_payload, _known_unit_handles, _output_diagnostics,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('BODY40 alias tests forbid network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def export_fixture(tmp_path, handles=('P0049', 'P0605')):
    packet = {
        'chapter': {'chapter_id': 'Ch6', 'title': 'Synthetic alias fixture'},
        'chapter_plan': {'units': [{'unit_id': 'Ch6_U2', 'substantive_point': 'Synthetic comparison',
            'paragraph_briefs': [{'paragraph_id': 'BRIEF_ORIGINAL', 'point': 'Synthetic point',
                'development': 'Synthetic boundary', 'source_handles': ['P0049', 'P0605']}]}]},
        'source_materials': [{'source_handle': handle, 'paper_id': 'synthetic-' + handle,
            'title': 'Synthetic duplicate study', 'doi': '10.9999/synthetic-alias',
            'study_summary_A': {'work_summary': 'SYNTHETIC_A_ONLY_ONCE'},
            'review_planning_B': {'possible_use': 'SYNTHETIC_B_ONLY_ONCE'}}
            for handle in ('P0049', 'P0605')],
    }
    packet_path = tmp_path / 'packet.json'
    packet_path.write_text(json.dumps(packet))
    chapter = build_chapter_view(packet_path)
    catalog = build_source_catalog(chapter)
    assert list(catalog) == ['P0049']
    assert catalog['P0049']['aliases'] == ['P0605']
    arrangement = {'chapter_id': 'Ch6', 'source_catalog': catalog, 'units': [{
        'unit_id': 'Ch6_U2', 'paragraph_tasks': [{'paragraph_id': 'TASK_ORIGINAL',
            'source_uses': [{'source_handle': h} for h in handles]}], 'table_tasks': []}]}
    (tmp_path / 'ARRANGEMENT_INPUT.json').write_text(json.dumps(chapter.to_dict()))
    path = tmp_path / 'ARRANGEMENT.json'
    path.write_text(json.dumps(arrangement))
    return path, arrangement, packet_path


@pytest.mark.parametrize('handles', [('P0049', 'P0605'), ('P0605',), ('P0605', 'P0049')])
def test_real_producer_consumer_shares_one_material_and_preserves_tasks(tmp_path, handles):
    path, arrangement, _ = export_fixture(tmp_path, handles)
    original = copy.deepcopy(arrangement['units'][0]['paragraph_tasks'])
    view = build_unit_view(path, 'Ch6_U2')
    assert view.paragraph_tasks == original
    assert len(view.materials) == 1
    assert view.materials[0]['source_handle'] == 'P0049'
    assert view.materials[0]['aliases'] == ['P0605']
    assert not any(m.get('missing_material') for m in view.materials)
    assert not view.warnings
    assert view.materials[0]['study_summary_A']['work_summary'] == 'SYNTHETIC_A_ONLY_ONCE'
    assert set(_known_unit_handles(view)) == {'P0049', 'P0605'}
    assert _output_diagnostics('[P0049] and [P0605]', _known_unit_handles(view))['unknown_citations'] == []
    payload = build_completion_payload(view, 'Existing synthetic body.', ['TASK_ORIGINAL'])
    assert payload['requested_source_handles'] == list(handles)
    assert payload['paragraph_tasks'] == original
    assert len(payload['sources']) == 1
    assert payload['sources'][0]['source_handle'] == 'P0049'


def test_no_explicit_alias_never_infers_same_title_or_doi(tmp_path):
    path, arrangement, _ = export_fixture(tmp_path, ('P0605',))
    arrangement['source_catalog']['P0049']['aliases'] = []
    path.write_text(json.dumps(arrangement))
    view = build_unit_view(path, 'Ch6_U2')
    assert view.materials == [{'source_handle': 'P0605', 'missing_material': True}]


@pytest.mark.parametrize('conflict', ['duplicate_owner', 'canonical_collision', 'self_alias', 'alias_string'])
def test_invalid_explicit_alias_declarations_fail_closed(tmp_path, conflict):
    path, arrangement, _ = export_fixture(tmp_path)
    catalog = arrangement['source_catalog']
    if conflict == 'duplicate_owner':
        catalog['P0002'] = dict(catalog['P0049'], source_handle='P0002')
    elif conflict == 'canonical_collision':
        catalog['P0605'] = dict(catalog['P0049'], source_handle='P0605', aliases=[], doi='10.9999/foreign')
    elif conflict == 'self_alias':
        catalog['P0049']['aliases'] = ['P0049']
    else:
        catalog['P0049']['aliases'] = 'P0605'
    path.write_text(json.dumps(arrangement))
    with pytest.raises(UnitWritingError, match='source_alias_'):
        build_unit_view(path, 'Ch6_U2')


def test_packet_alias_identity_conflict_fails_closed(tmp_path):
    path, _, packet_path = export_fixture(tmp_path)
    packet = json.loads(packet_path.read_text())
    packet['source_materials'][1]['doi'] = '10.9999/foreign'
    packet_path.write_text(json.dumps(packet))
    with pytest.raises(UnitWritingError, match='source_alias_identity_conflict'):
        build_unit_view(path, 'Ch6_U2')


def test_cached_export_without_packet_still_consumes_explicit_alias(tmp_path):
    path, _, packet_path = export_fixture(tmp_path, ('P0605',))
    packet_path.unlink()
    view = build_unit_view(path, 'Ch6_U2')
    assert len(view.materials) == 1 and view.materials[0]['source_handle'] == 'P0049'
    assert not view.materials[0].get('missing_material')


class Replay:
    def __init__(self, body):
        self.body = body

    def __call__(self, messages, **kwargs):
        return {'content': json.dumps(self.body), 'complete': True, 'finish_reason': 'stop'}


@pytest.mark.parametrize('citation', ['P0049', 'P0605'])
def test_writer_and_completion_citations_and_unused_share_identity(tmp_path, citation):
    from optomind_research.runtime.upgrade3.review_unit_writer import (
        run_unit_writing, run_unit_completion, write_unit_output,
    )
    path, _, _ = export_fixture(tmp_path, ('P0605',))
    view = build_unit_view(path, 'Ch6_U2')
    body = 'Synthetic citation [' + citation + '].'
    result = run_unit_writing(view, client=Replay({'body_markdown': body}), prompt='Synthetic only.')
    assert result['unknown_citations'] == []
    saved = write_unit_output(view, result['body_markdown'], tmp_path / 'output', model='offline',
        language='en', mode='fake', used_messages=result['messages'], estimate={}, citation_diagnostics=result)
    assert saved['unused_source_handles'] == []
    assert saved['source_count'] == saved['material_summary']['sources'] == 1
    completion = run_unit_completion(view, existing_body='Original prefix.  ', task_ids=['TASK_ORIGINAL'],
        client=Replay({'body_markdown': body, 'status': 'appended', 'covered_task_ids': ['TASK_ORIGINAL']}),
        prompt='Synthetic only.', simulated=True)
    assert completion['unknown_citations'] == []
    assert completion['completion_fragment'].endswith(body)
    assert completion['body_markdown'].startswith('Original prefix.  ')
    # Explicit numeric mappings keep working for either declared identity;
    # generated number suffix guesses still need first-stop confirmation.
    completion = run_unit_completion(view, existing_body='Original prefix.', task_ids=['TASK_ORIGINAL'],
        client=Replay({'body_markdown': 'Synthetic [1].', 'status': 'appended', 'covered_task_ids': ['TASK_ORIGINAL']}),
        prompt='Synthetic only.', simulated=True, citation_number_map={'1': citation})
    assert completion['completion_fragment'].endswith(body.replace('citation ', ''))
    assert completion['unknown_citations'] == []


def test_tool_only_review_reported_original_remains_eligible(tmp_path):
    path, arrangement, packet_path = export_fixture(tmp_path, ('P0605',))
    entry = arrangement['source_catalog']['P0049']
    entry.pop('study_summary_A', None)
    entry.pop('review_planning_B', None)
    entry['material_status'] = 'no_material'
    entry['tool_supplement_materials'] = [{'need_id': 'SYNTHETIC_REVIEW_ORIGINAL',
        'usable_content': 'Synthetic original result reported by a review; direct reading not performed.',
        'sources': [{'source_handle': 'P0049', 'paper_id': entry['paper_id'], 'doi': entry['doi']}]}]
    packet_path.unlink()  # persisted arrangement remains the material authority
    path.write_text(json.dumps(arrangement))
    view = build_unit_view(path, 'Ch6_U2')
    assert len(view.materials) == 1
    material = view.materials[0]
    assert not material.get('missing_material')
    assert not material.get('study_summary_A') and not material.get('review_planning_B')
    assert material['tool_supplement_materials'] == entry['tool_supplement_materials']
    assert build_completion_payload(view, '', ['TASK_ORIGINAL'])['sources'] == view.materials
    assert not any(w['code'] == 'sources_without_any_material' for w in view.warnings)


@pytest.mark.parametrize('usable', [False, True])
def test_chapter_tool_link_requires_actual_content(tmp_path, usable):
    path, arrangement, packet_path = export_fixture(tmp_path, ('P0605',))
    entry = arrangement['source_catalog']['P0049']
    entry.pop('study_summary_A', None)
    entry.pop('review_planning_B', None)
    entry['tool_supplement_materials'] = [{'title': 'Metadata only', 'paper_id': entry['paper_id']}]
    arrangement['chapter_tool_materials'] = [{'need_id': 'SYNTHETIC', 'sources': [{'source_handle': 'P0605'}],
        'usable_content': 'Synthetic reported original result.' if usable else ''}]
    packet_path.unlink()
    path.write_text(json.dumps(arrangement))
    view = build_unit_view(path, 'Ch6_U2')
    warned = any(w['code'] == 'sources_without_any_material' for w in view.warnings)
    assert warned is not usable
    assert view.materials[0]['source_handle'] == 'P0049'


def test_completion_tool_alias_link_is_symmetric(tmp_path):
    path, arrangement, _ = export_fixture(tmp_path, ('P0049',))
    tool = {'sources': [{'source_handle': 'P0605'}], 'usable_content': 'SYNTHETIC_TOOL_SENTINEL'}
    arrangement['chapter_tool_materials'] = [tool]
    path.write_text(json.dumps(arrangement))
    view = build_unit_view(path, 'Ch6_U2')
    payload = build_completion_payload(view, '', ['TASK_ORIGINAL'])
    assert payload['chapter_tool_materials'] == [tool]
    assert len(payload['sources']) == 1


def test_alias_only_real_locator_card_reads_once(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import review_unit_writer as writer
    path, arrangement, _ = export_fixture(tmp_path, ('P0605', 'P0049'))
    card = tmp_path / 'synthetic_card.json'
    entry = arrangement['source_catalog']['P0049']
    card.write_text(json.dumps({'paper_id': entry['paper_id'], 'doi': entry['doi'],
        'general_understanding': {'work_summary': 'SYNTHETIC_A_ONLY_ONCE'}}))
    entry['locator']['card_path'] = str(card)
    path.write_text(json.dumps(arrangement))
    original = writer._read_card_material
    calls = []
    def count_reads(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(writer, '_read_card_material', count_reads)
    view = build_unit_view(path, 'Ch6_U2')
    assert calls == [str(card)]
    assert len(view.materials) == 1
    assert view.materials[0]['material_read_from_disk'] == str(card)
    assert view.materials[0]['study_summary_A']['work_summary'] == 'SYNTHETIC_A_ONLY_ONCE'


def test_restored_owner_brief_alias_is_consumed_without_rewriting(tmp_path):
    # Synthetic edge only: actual archived Ch6 alias occurs in source_uses.
    path, arrangement, _ = export_fixture(tmp_path, ('P0049',))
    task = arrangement['units'][0]['paragraph_tasks'][0]
    task['source_briefs'] = ['BRIEF_ORIGINAL']
    path.write_text(json.dumps(arrangement))
    view = build_unit_view(path, 'Ch6_U2')
    restored = view.paragraph_tasks[0]
    assert restored['paragraph_id'] == 'TASK_ORIGINAL'
    assert restored['source_briefs'] == ['BRIEF_ORIGINAL']
    assert restored['source_brief_details'][0]['paragraph_id'] == 'BRIEF_ORIGINAL'
    assert restored['source_brief_details'][0]['source_handles'] == ['P0049', 'P0605']
    assert [use['source_handle'] for use in restored['source_uses']] == ['P0049', 'P0605']
    assert len(view.materials) == 1 and view.materials[0]['source_handle'] == 'P0049'
