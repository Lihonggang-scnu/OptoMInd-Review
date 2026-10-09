"""Offline preservation/grouping tests, independent of any research domain."""
from copy import deepcopy

import pytest

from test_guide_maker_contracts import book, guide, response
from optomind_research.runtime.upgrade3.guide_maker_contracts import (
    compile_guide_input, build_maker_payload, parse_maker_response)
from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide
from optomind_research.runtime.upgrade3.guided_content_units import (
    normalize_writing_units, resolve_content_basis, assembly_warnings)
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError


def prepared():
    raw = book()
    task = raw['chapters'][0]['units'][0]['paragraph_tasks'][0]
    task.update(development={'comparison': ['positive', 'negative'], 'conditions': {'sample': 42}},
                source_uses=[{'source_handle': 'P1', 'role': 'counterexample', 'use': 'conditional contrast'}],
                source_brief_details=[{'source_handle': 'P1', 'point': 'independent explanation'}])
    raw['chapters'][0]['units'][0]['table_tasks'][0]['row_tasks'] = [
        {'row_label': 'different condition', 'source_handles': ['P1'], 'unknown': {'keep': True}}]
    bundle = compile_guide_input(raw)
    return raw, bundle, bundle['science_archive']


def unit(ids, **kw):
    return dict(unit_id='merged', title='Compare mechanisms',
                writing_arrangement='Explain the contrast, then interpret the table.', content_task_ids=ids, **kw)


def test_exact_tasks_and_source_roles_survive_grouping_and_do_not_alias():
    raw, bundle, pack = prepared()
    saved = deepcopy(raw)
    ids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == 'optics']
    grouped = normalize_writing_units([unit(ids[::-1])], 'optics', pack)
    basis = resolve_content_basis(grouped[0], pack)
    assert [row['task_id'] for row in basis] == ids[::-1]
    assert [row['task'] for row in basis] == [pack['tasks'][tid]['task'] for tid in ids[::-1]]
    assert 'source_brief_details' in basis[1]['task']
    assert basis[0]['task']['row_tasks'][0]['unknown'] == {'keep': True}
    basis[1]['task']['source_brief_details'].clear()
    assert raw == saved and pack['tasks'][ids[0]]['task']['source_brief_details']


def test_catalog_addresses_point_into_full_outline_without_duplicate_task_text():
    raw, bundle, pack = prepared()
    payload = build_maker_payload(bundle)
    from optomind_research.runtime.upgrade3.guided_content_units import content_task_aliases
    aliases = content_task_aliases(pack)
    assert {r['task_id'] for r in payload['content_task_catalog']} == set(aliases)
    assert set(aliases.values()) == set(pack['tasks'])
    assert all('task' not in row and 'point' not in row for row in payload['content_task_catalog'])
    assert payload['full_outline']['chapters'][0]['units'] == raw['chapters'][0]['units']


def test_missing_content_is_retained_in_visible_original_unit_fallback():
    _, _, pack = prepared()
    ids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == 'optics']
    units = normalize_writing_units([unit(ids[:1])], 'optics', pack)
    assert units[0]['content_task_ids'] == ids[:1]
    assert units[1]['content_task_ids'] == ids[1:]
    assert units[1]['assembly_note'] and units[1]['unit_id'] == 'retained_001'
    assert normalize_writing_units(units, 'optics', pack) == units


def test_new_maker_legacy_shaped_return_recovers_units_with_warning_but_legacy_direct_guide_stays_legacy():
    raw, bundle, pack = prepared()
    old = guide()
    assert all('writing_units' not in chapter for chapter in validate_guide(old, raw)['chapters'])
    parsed = parse_maker_response(response(complete=True), raw, bundle)
    assert parsed['complete']
    assert parsed['assembly_warnings'] == assembly_warnings(parsed['guide'])
    assert len(parsed['assembly_warnings']) == 2
    allocated = [tid for chapter in parsed['guide']['chapters']
                 for u in chapter['writing_units'] for tid in u['content_task_ids']]
    assert set(allocated) == set(pack['tasks']) and len(allocated) == len(pack['tasks'])


@pytest.mark.parametrize('mode', ['unknown', 'cross_chapter', 'duplicate_assignment'])
def test_ambiguous_connections_fail_explicitly(mode):
    _, _, pack = prepared()
    own = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == 'optics']
    other = next(tid for tid, row in pack['tasks'].items() if row['chapter_id'] != 'optics')
    units = [unit(['absent'] if mode == 'unknown' else [other] if mode == 'cross_chapter' else own)]
    if mode == 'duplicate_assignment':
        units.append({**unit(own), 'unit_id': 'second'})
    with pytest.raises(CandidateError):
        normalize_writing_units(units, 'optics', pack)


def test_model_grouped_guide_is_complete_without_fallback_warning():
    raw, bundle, pack = prepared()
    output = response(complete=True)
    for chapter in output['guide']['chapters']:
        ids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == chapter['chapter_id']]
        chapter['writing_units'] = [unit(ids)]
    parsed = parse_maker_response(output, raw, bundle)
    assert parsed['complete'] and parsed['assembly_warnings'] == []
    assert parsed['guide']['chapters'][0]['writing_units'] == output['guide']['chapters'][0]['writing_units']


def test_original_unit_context_is_complete_once_and_immutable():
    from optomind_research.runtime.upgrade3.guided_content_units import resolve_content_contexts
    raw, _, pack = prepared()
    ids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == 'optics']
    context = resolve_content_contexts(unit(ids), pack)
    assert set(context) == {'U1'}
    assert context['U1']['focus'] == raw['chapters'][0]['units'][0]['focus']
    assert context['U1']['owner_unit_context'] == raw['chapters'][0]['units'][0]['owner_unit_context']
    context['U1']['owner_unit_context'].clear()
    assert pack['unit_contexts']['optics']['U1']['owner_unit_context']


def test_unknown_added_unit_source_rejected_before_guide_delivery():
    raw, bundle, pack = prepared()
    output = response(complete=True)
    for chapter in output['guide']['chapters']:
        ids = [tid for tid, row in pack['tasks'].items() if row['chapter_id'] == chapter['chapter_id']]
        chapter['writing_units'] = [unit(ids, source_handles=['unknown'])]
    with pytest.raises(CandidateError, match='guided_source_unknown'):
        parse_maker_response(output, raw, bundle)


def test_render_guide_includes_unit_added_sources_and_requirements():
    from optomind_research.runtime.upgrade3.guide_maker import render_guide
    value = guide()
    value['chapters'][0]['writing_units'] = [unit([], source_handles=['P1'], required_content=['Add boundary comparison'])]
    rendered = render_guide(value)
    assert 'Add boundary comparison' in rendered and 'Sources: P1' in rendered


def test_short_aliases_resolve_long_unicode_punctuation_ids_without_content_changes():
    from optomind_research.runtime.upgrade3.guided_content_units import content_task_aliases
    raw = book()
    raw['chapters'][0]['units'][0]['paragraph_tasks'][0]['paragraph_id'] = '独立解释：比较/条件%与反例' * 10
    pack = compile_guide_input(raw)['science_archive']
    aliases = content_task_aliases(pack)
    assert len(aliases['C0001']) > 179
    row = normalize_writing_units([unit(['C0001', aliases['C0001']])], 'optics', pack)[0]
    assert row['content_task_ids'] == [aliases['C0001']]
    assert resolve_content_basis(row, pack)[0]['task'] == raw['chapters'][0]['units'][0]['paragraph_tasks'][0]
    # Alias/canonical alternate spellings cannot create two writing owners.
    with pytest.raises(CandidateError, match='multiple_units'):
        normalize_writing_units([unit(['C0001']), {**unit([aliases['C0001']]), 'unit_id': 'second'}], 'optics', pack)
    with pytest.raises(CandidateError, match='unknown'):
        normalize_writing_units([unit(['c0001'])], 'optics', pack)


def test_prior_guide_view_uses_aliases_but_saved_guide_stays_canonical():
    from optomind_research.runtime.upgrade3.guided_content_units import content_task_aliases
    raw, bundle, pack = prepared()
    parsed = parse_maker_response(response(complete=True), raw, bundle)
    saved = deepcopy(parsed['guide'])
    payload = build_maker_payload(bundle, prior_guide=parsed['guide'])
    aliases = content_task_aliases(pack)
    prior_ids = [tid for c in payload['prior_guide']['chapters'] for u in c['writing_units'] for tid in u['content_task_ids']]
    assert set(prior_ids) == set(aliases)
    assert parsed['guide'] == saved
    repeated = parse_maker_response(response(guide=payload['prior_guide'], complete=True), raw, bundle)
    assert repeated['guide'] == saved
