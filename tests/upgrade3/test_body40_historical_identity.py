"""Offline stable-identity handoff regressions; no scientific or BODY rewrite."""
import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planner
from test_body03_local_lookup_reading import index_with, capture_model


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('BODY40 identity regressions forbid network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.mark.parametrize('discipline,body', [
    ('coatings', 'Thermal fatigue: a porous interlayer delayed crack initiation.'),
    ('education', 'Retrieval practice: spaced quizzes improved delayed recall, not grammar transfer.'),
])
@pytest.mark.parametrize('current', [{}, {'P0001': 'current-unrelated'}, {'P0593': 'current-unrelated'}, {'P0601': 'historical-study'}])
def test_unoccupied_old_label_never_enters_current_messages(tmp_path, monkeypatch, discipline, body, current):
    path = index_with(tmp_path, [('historical-study', 'P0593', body)])
    def answer(payload):
        source = payload['material_found'][0]['source_handle']
        return {'decision': 'direct_use', 'answers_requested_question': True,
                'usable_content': body + ' (' + source + ')', 'still_missing': ''}
    judge, calls = capture_model(tmp_path, monkeypatch, answer)
    result = supplement.run_gap_local_triage(
        {'gap_id': discipline, 'question': body.split(':')[0], 'known_papers': [{'paper_id': 'historical-study'}]},
        index_path=path, judge=judge, source_handle_map=current, read_local=False)
    expected = 'P0601' if 'P0601' in current else 'historical-study'
    assert calls[0]['material_found'][0]['source_handle'] == expected
    assert calls[0]['paper_context'][0]['source_handle'] == expected
    assert body in calls[0]['material_found'][0]['text']
    source = result['writer_material']['sources'][0]
    assert source['paper_id'] == 'historical-study' and source['source_handle'] == expected
    assert expected in result['usable_content']
    # A later ordinary pool addition may safely occupy the old label.
    rows = [{'paper_id': 'current-unrelated', '_paper_id': 'current-unrelated', '_source_handle': 'P0593'}]
    owner = SimpleNamespace(tool_materials_by_chapter={'CH01': [copy.deepcopy(result['writer_material'])]})
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, rows)
    bound = owner.tool_materials_by_chapter['CH01'][0]
    assert bound['sources'][0]['paper_id'] == 'historical-study'
    assert bound['sources'][0]['source_handle'] == 'historical-study'
    assert bound['usable_content'] == result['usable_content']
    # Existing registration assigns a separate current handle, without its own A/B.
    planner._merge_supplement_pool_updates(rows, {'results': [{'candidate_rows': [
        {'paper_id': 'historical-study', 'title': discipline, 'doi': '10.9999/historical'}]}]})
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, rows)
    identity = {r['_paper_id']: r['_source_handle'] for r in rows}
    assert identity['current-unrelated'] == 'P0593'
    assert bound['sources'][0]['source_handle'] == identity['historical-study'] != 'P0593'
    assert bound['usable_content'] == result['usable_content']


def test_tool_binding_does_not_retain_unoccupied_historical_handle():
    material = {'sources': [{'paper_id': 'historical-study', 'source_handle': 'P0593', 'title': 'Useful study'}],
                'usable_content': 'Useful study material remains available.'}
    owner = SimpleNamespace(tool_materials_by_chapter={'CH01': [material]})
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, [])
    assert material['sources'][0]['source_handle'] == 'historical-study'
    assert material['usable_content'] == 'Useful study material remains available.'


def test_archived_tacito_passage_uses_stable_id_before_future_registration():
    root = Path(__file__).resolve().parents[2]
    path = root / 'docs/acceptance/body40-20261005/planning/local_lookup/fc2b477ab461b3d1f9f9f1ea75d990150d85e8c937365d05df803ac5bcea91a3/attempt-79wz2q6_/REQUEST.json'
    request = json.loads(path.read_text())
    source = next(s for s in request['sources'] if s['source_handle'] == 'P0593')
    passage = next(p for p in request['payload']['material_found'] if p['source_handle'] == 'P0593')
    hit = SimpleNamespace(**source, title='TACITO', year='2025', doi='10.1038/s41591-025-04189-2',
        segment_kind='document_block', material_depth=passage['material_depth'], best_sentence='',
        section_path=tuple(passage['section']), match_terms=('FMT',))
    gap = triage.LocalGap(gap_id='archive', question=request['payload']['question'])
    rebuilt = triage._reading_passage(None, gap, hit, text=passage['text'])
    assert rebuilt.source_handle == source['paper_id'] == '69b4c5f84be199181be6ab5d3124f306462234f3'
    assert rebuilt.text == passage['text']


def test_conflicting_source_unit_cannot_override_stable_paper_id():
    source = {'paper_id': 'historical-study', 'source_handle': 'P0593', 'source_unit_id': 'reused-unit'}
    owner = SimpleNamespace(tool_materials_by_chapter={'CH01': [{'sources': [source]}]})
    rows = [{'paper_id': 'different-study', '_paper_id': 'different-study', '_source_handle': 'P0593',
             'supplement_source_unit_id': 'reused-unit'}]
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, rows)
    assert source['source_handle'] == 'historical-study'


def test_successful_old_label_checkpoint_is_not_reused(tmp_path):
    from optomind_research.runtime.upgrade3.planning_material_search import SearchHit
    hit = SearchHit('stable-study', 'P0593', 'Synthetic study', '2026', '', 'document_block',
                    ('Results',), 'Retrieval practice improves recall.', 1, ('recall',), 'fulltext', '', '', '')
    gap = triage.LocalGap(gap_id='cache', question='recall')
    safe = triage._reading_passage(None, gap, hit, text=hit.text)
    legacy = copy.deepcopy(safe)
    legacy.source_handle = 'P0593'
    calls = []
    def judge(gap, bundle):
        calls.append(bundle.passages[0].source_handle)
        return {'decision': 'direct_use', 'answers_requested_question': True,
                'usable_content': 'Recall result (' + calls[-1] + ')', 'still_missing': ''}
    checkpoint = supplement._LocalJudgeCheckpoint(judge, tmp_path / 'cache')
    old = checkpoint(gap, triage.LocalReadingBundle(gap=gap, passages=[legacy]))
    safe_bundle = triage.LocalReadingBundle(gap=gap, passages=[safe])
    new = checkpoint(gap, safe_bundle)
    assert calls == ['P0593', 'stable-study']
    assert old['usable_content'].endswith('(P0593)')
    assert new['usable_content'].endswith('(stable-study)')
    assert checkpoint(gap, safe_bundle) == new
    assert len(calls) == 2
    assert len(list((tmp_path / 'cache').glob('*/attempt-*/RESULT.json'))) == 2


@pytest.mark.parametrize('source,expected', [
    ({'paper_id': 'current-study', 'source_handle': 'P0888', 'source_unit_id': 'unit'}, 'P0001'),
    ({'source_unit_id': 'unit'}, 'P0001'),
    ({'source_handle': 'P0001'}, ''),
    ({'source_handle': 'P0593'}, ''),
])
def test_only_current_identity_or_unit_can_supply_a_label(source, expected):
    owner = SimpleNamespace(tool_materials_by_chapter={'CH01': [{'sources': [source]}]})
    rows = [{'_paper_id': 'current-study', '_source_handle': 'P0001', 'supplement_source_unit_id': 'unit'}]
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, rows)
    assert source['source_handle'] == expected
    if not expected:
        assert source['unresolved_source_handle'] in {'P0001', 'P0593'}
    if source.get('source_unit_id') == 'unit':
        assert source['paper_id'] == 'current-study'


def test_unknown_handle_only_claim_stays_chapter_level_after_real_merge():
    material = {'chapter_id': 'CH01', 'need_id': 'historical-result',
                'usable_content': 'Historical useful finding; attribution still needs resolution.',
                'sources': [{'source_handle': 'P0593'}]}
    pool = [{'_paper_id': 'different-study', '_source_handle': 'P0593'}]
    owner = SimpleNamespace(tool_materials_by_chapter={'CH01': [material]})
    planner.ProgressiveReviewPlanner._bind_tool_material_source_handles(owner, pool)
    packet = {'chapter_id': 'CH01', 'source_materials': [
        {'paper_id': 'different-study', 'source_handle': 'P0593', 'title': 'Current unrelated study',
         'study_summary_A': {'finding': 'Current finding'}}]}
    merged = planner.merge_tool_materials_into_packets([packet], [material])[0]
    current = merged['source_materials'][0]
    assert current['paper_id'] == 'different-study'
    assert current['study_summary_A'] == {'finding': 'Current finding'}
    assert not current.get('tool_supplement_materials')
    assert merged['tool_materials'][0]['usable_content'] == material['usable_content']
    unresolved = merged['tool_materials'][0]['sources'][0]
    assert not unresolved.get('source_handle')
    assert not unresolved.get('paper_id')
    assert unresolved['unresolved_source_handle'] == 'P0593'
    assert unresolved['identity_status'] == 'unresolved_identity'
    prompt_material = planner._tool_material_for_prompt(merged['tool_materials'][0])
    assert prompt_material['usable_content'] == material['usable_content']
    assert prompt_material['sources'][0]['unresolved_source_handle'] == 'P0593'
