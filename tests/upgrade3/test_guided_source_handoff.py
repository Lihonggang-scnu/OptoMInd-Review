"""Offline chapter-local source supply, not semantic role/citation validation."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.guided_body_contracts import (
    build_author_payload, compile_guided_materials, validate_guide,
)
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError
from test_guided_body_contracts import no_network  # noqa: F401
from test_guided_body_writer import Factory, book, config, execute, guide  # noqa: F401


@pytest.fixture
def inputs():
    chapters = []
    for cid, handle in [('light', 'Photon-A7'), ('reaction', 'Catalyst.v2'),
                        ('climate', 'Dataset:R3'), ('review', 'Review/Z9')]:
        chapters.append({
            'chapter_id': cid, 'chapter_frame': {'title': cid},
            'units': [{'unit_id': 'U', 'paragraph_tasks': [
                {'paragraph_id': 'T', 'point': 'Explain the finding', 'source_handles': [handle]}],
                'table_tasks': [], 'owner_unit_context': {}}],
            'sources': [{'source_handle': handle, 'title': cid + ' study',
                'study_summary_A': {'key_findings': [{
                    'finding': 'conditional observation for ' + cid,
                    'conditions': {'setting': cid, 'duration': 'short exposure'},
                    'comparison': 'matched baseline', 'limits': ['one apparatus']} ]}}],
            'chapter_tool_materials': [{'unit_key': cid + ':U', 'finding': cid + ' tool',
                                        'conditions': {'setting': cid}}],
        })
    chapters[1]['sources'][0]['aliases'] = ['Legacy-C8']
    chapters[1]['sources'][0]['review_source_handle'] = 'Review/Z9'
    book = {'chapters': chapters}
    guide = validate_guide({'manuscript_guide': 'Preserve the conditional explanations.',
        'chapters': [{'chapter_id': row['chapter_id'], 'title': row['chapter_id'],
                      'writing_arrangement': 'Explain the local result.'} for row in chapters]}, book)
    return compile_guided_materials(book), guide


def current_payload(inputs):
    pack, guide = inputs
    return build_author_payload(pack, guide, 'light', 'Accepted actual prose.')


@pytest.mark.parametrize('field', ['writing_arrangement', 'required_content'])
def test_local_known_mentions_supply_complete_objects_and_review_dependencies(inputs, field):
    pack, guide = inputs
    text = '比较Catalyst.v2的条件；保留[Dataset:R3]的独立对照。'
    guide['chapters'][0][field] = [text] if field == 'required_content' else text
    before = deepcopy(inputs)
    payload = current_payload(inputs)
    materials = payload['materials']
    assert set(payload) == {'manuscript_guide', 'chapter_assignment', 'materials', 'accepted_body_markdown'}
    assert set(materials['source_identities']) == {'Photon-A7', 'Catalyst.v2', 'Dataset:R3', 'Review/Z9'}
    for cid in ['reaction', 'climate', 'review']:
        assert {'finding': 'conditional observation for ' + cid,
                'conditions': {'setting': cid, 'duration': 'short exposure'},
                'comparison': 'matched baseline', 'limits': ['one apparatus']} in [
                    atom['value'] for atom in materials['evidence_atoms']]
    assert materials['tool_materials'] == [{'finding': 'light tool', 'conditions': {'setting': 'light'}}]
    assert inputs == before


def test_original_explicit_alias_and_repeated_text_are_canonicalized_once(inputs):
    pack, guide = inputs
    row = guide['chapters'][0]
    row['source_handles'] = ['Legacy-C8', 'Catalyst.v2', 'Photon-A7']
    row['writing_arrangement'] = 'Compare Legacy-C8, Catalyst.v2 and Legacy-C8.'
    row['required_content'] = ['Retain Catalyst.v2 and Photon-A7.']
    materials = current_payload(inputs)['materials']
    assert set(materials['source_identities']) == {'Photon-A7', 'Catalyst.v2', 'Review/Z9'}
    ids = [atom['atom_id'] for atom in materials['evidence_atoms']]
    assert len(ids) == len(set(ids))
    assert materials['source_aliases']['Legacy-C8'] == 'Catalyst.v2'
    del row['source_handles']
    row['writing_arrangement'] = 'Use [Legacy-C8].'
    del row['required_content']
    assert current_payload(inputs)['materials'] == materials


@pytest.mark.parametrize('text', [
    'XCatalyst.v2 Catalyst.v20 Catalyst.v2_extra',
    'ns:Catalyst.v2 prefix-Catalyst.v2 Catalyst.v2-suffix',
    'folder/Catalyst.v2 Catalyst.v2/path',
    'prefix.Catalyst.v2 Catalyst.v2.extra',
    'catalyst.v2 CATALYST.V2 Legacy-c8',
    'CatalystXv2 Dataset:R30 2 3 R3',
    'Unknown-A9 P9999 made-up-handle',
])
def test_decorative_unknowns_case_variants_and_longer_identifiers_do_not_supply_sources(inputs, text):
    inputs[1]['chapters'][0]['writing_arrangement'] = text
    assert set(current_payload(inputs)['materials']['source_identities']) == {'Photon-A7'}


def test_global_guide_other_chapters_titles_and_prefix_are_not_material_selectors(inputs):
    pack, guide = inputs
    guide['manuscript_guide'] = 'Cross-chapter context uses Catalyst.v2 and Dataset:R3.'
    guide['chapters'][1]['writing_arrangement'] = 'Compare Catalyst.v2 and Dataset:R3.'
    guide['chapters'][1]['required_content'] = ['Keep Dataset:R3.']
    guide['chapters'][0]['title'] = 'Catalyst.v2'
    payload = build_author_payload(pack, guide, 'light', 'Earlier discussion of Dataset:R3.')
    assert 'Catalyst.v2' in payload['manuscript_guide']
    assert set(payload['materials']['source_identities']) == {'Photon-A7'}
    # The same address is selected when it actually belongs to the current chapter.
    guide['chapters'][0]['required_content'] = ['Explain Dataset:R3.']
    assert set(current_payload(inputs)['materials']['source_identities']) == {'Photon-A7', 'Dataset:R3'}


def test_negative_mention_supplies_material_without_claiming_semantic_support(inputs):
    inputs[1]['chapters'][0]['writing_arrangement'] = 'Do not use Catalyst.v2 as proof of this claim.'
    payload = current_payload(inputs)
    assert 'Catalyst.v2' in payload['materials']['source_identities']
    assert payload['materials']['read_protocol']['scientific_fidelity_verified'] is False
    assert payload['chapter_assignment'] == inputs[1]['chapters'][0]


def test_unknown_explicit_handles_still_error(inputs):
    inputs[1]['chapters'][0]['source_handles'] = ['Unknown-A9']
    with pytest.raises(CandidateError, match='guided_source_unknown:Unknown-A9'):
        current_payload(inputs)


def test_supplemented_materials_still_hit_existing_capacity_guard(tmp_path, book, guide, config):
    guide['chapters'][0]['writing_arrangement'] = 'Compare P0002 and P0003.'
    config['max_input_tokens'] = 1
    factory = Factory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result['status'] == 'capacity_blocked' and not factory.calls
    assert 'No truncation' in result['required_action']
    messages = json.loads(Path(result['stages'][0]['messages_path']).read_text())
    materials = json.loads(messages[-1]['content'])['materials']
    assert set(materials['source_identities']) == {'P0001', 'P0002', 'P0003'}
    assert all('INTACT_EVIDENCE_' + str(i) in json.dumps(materials['evidence_atoms']) for i in (1, 2, 3))
