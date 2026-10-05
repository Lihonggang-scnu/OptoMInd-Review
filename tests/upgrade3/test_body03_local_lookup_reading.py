"""Bounded local-reading controls using real SQLite/FTS and production messages.

Synthetic material is deliberately unrelated to the archived scientific question.
Only the model/client boundary is replaced. No real index or quality claim.
"""
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planner
from optomind_research.runtime.upgrade3.planning_material_search import PaperRecord, PlanningMaterialIndex, search


def index_with(tmp_path, rows):
    path = tmp_path / 'local.sqlite'
    with PlanningMaterialIndex(path) as index:
        for paper_id, handle, body in rows:
            index.upsert_paper(PaperRecord(paper_id, handle, paper_id, '2025', '', material_depth='fulltext'))
            index.add_segments(paper_id, [{'segment_kind': 'document_block', 'ordinal': 0,
                                         'section_path': ['Measured conditions'], 'text': body}])
        index.record_term_document_frequency()
        index.commit()
    return path


def capture_model(tmp_path, monkeypatch, answer):
    from optomind_research.runtime.upgrade3.module4 import runtime
    calls = []
    monkeypatch.setattr(runtime, 'QwenDirectClient', lambda **kwargs: object())
    def invoke(client, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        response = answer(calls[-1]) if callable(answer) else answer
        return {'content': json.dumps(response)}
    monkeypatch.setattr(runtime, 'invoke_client', invoke)
    judge = triage.QwenLocalTriageJudge(key_file=tmp_path/'never-read-key',
        budget_ledger_path=tmp_path/'budget.sqlite', budget_limit_cny=1)
    return judge, calls


PARTIAL = {'decision': 'external_research', 'answers_requested_question': False,
           'usable_content': 'An observed boundary is useful background.', 'still_missing': 'Need the comparison.'}


def test_restricted_search_applies_pin_before_bm25_limit(tmp_path):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity thermal conductivity'),
                                  ('z', 'P9', 'Thermal conductivity differs under the measured boundary.')])
    with PlanningMaterialIndex(path, readonly=True) as index:
        assert {hit.paper_id for hit in search(index, 'thermal conductivity', max_candidates=1).hits} == {'a'}
        restricted = search(index, 'thermal conductivity', max_candidates=1, paper_ids=['z'])
        assert [hit.paper_id for hit in restricted.hits] == ['z']


def test_long_intact_pinned_segment_gets_spare_budget(tmp_path, monkeypatch):
    long = 'Thermal conductivity ' + 'measured boundary observation ' * 245 + 'ONLY_WITHIN_THE_RECORDED_SETTING.'
    rows = [('a', 'P1', 'thermal conductivity observed baseline.')]
    rows.extend((f'long{i}', f'P{i+2}', long + f' Study {i}.') for i in range(2))
    rows.extend((f'short{i}', f'P{i+4}', 'Thermal conductivity measured in a bounded laboratory setting.') for i in range(8))
    path = index_with(tmp_path, rows)
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    result = supplement.run_gap_local_triage({'gap_id': 'G', 'question': 'thermal conductivity',
        'known_papers': [{'paper_id': row[0]} for row in rows[1:]]}, index_path=path, judge=judge,
        top_papers=1, max_passages=1, read_local=False)
    actual = calls[0]['material_found']
    assert all(any(text['source_handle'] == handle and 'ONLY_WITHIN_THE_RECORDED_SETTING' in text['text']
                   for text in actual) for handle in ('long0', 'long1'))
    assert sum(len(text['text']) for text in actual[1:]) <= triage.LOCAL_INCREMENT_CHARS
    assert result['answers_requested_question'] is False


def test_over_budget_nomination_has_explicit_omission(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity observed baseline.'),
        ('z', 'P9', 'Thermal conductivity ' + 'reported boundary setting ' * 1300)])
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    result = supplement.run_gap_local_triage({'gap_id': 'G', 'question': 'thermal conductivity',
        'known_papers': [{'paper_id': 'z'}]}, index_path=path, judge=judge,
        top_papers=1, max_passages=1, read_local=False)
    assert result['local_reading']['bounded_read_omissions'] == ['z']
    assert [row['source_handle'] for row in calls[0]['material_found']] == ['a']


def test_current_identity_beats_stale_handle_before_adapter(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity baseline.'),
                                ('z', 'P2', 'Thermal conductivity observed special condition.')])
    row = planner._resolve_planner_handles({'supplement_requests': [{'question': 'thermal conductivity',
        'known_papers': [{'paper_id': 'z', 'source_handle': 'P1'}]}]}, {'P1': 'a', 'P7': 'z'})['supplement_requests'][0]
    assert row['known_papers'][0]['paper_id'] == 'z'
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    result = supplement.run_gap_local_triage(row, index_path=path, judge=judge,
        source_handle_map={'P1': 'a', 'P7': 'z'}, read_local=False)
    assert any(item['source_handle'] == 'P7' and 'special condition' in item['text']
               for item in calls[0]['material_found'])
    assert any(item['paper_id'] == 'z' and item['source_handle'] == 'P7' for item in result['writer_material']['sources'])


def test_same_material_cache_survives_owner_and_gap_relabel(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity observed baseline.')])
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    first = supplement.run_gap_local_triage({'gap_id': 'G1', 'question': 'thermal conductivity', 'chapter_ids': ['C1']},
        index_path=path, judge=judge, cache_dir=tmp_path/'cache')
    second = supplement.run_gap_local_triage({'gap_id': 'G2', 'question': 'thermal conductivity', 'chapter_ids': ['C2']},
        index_path=path, judge=judge, cache_dir=tmp_path/'cache')
    assert len(calls) == 1 and second['model_calls'] == 0
    assert first['usable_content'] == second['usable_content']
    assert second['gap']['chapter_ids'] == ['C2']


def test_wal_visible_text_change_changes_actual_message_cache(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity OLD observed baseline.')])
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    kwargs = {'index_path': path, 'judge': judge, 'cache_dir': tmp_path/'cache', 'read_local': False}
    gap = {'gap_id': 'G', 'question': 'thermal conductivity'}
    supplement.run_gap_local_triage(gap, **kwargs)
    with PlanningMaterialIndex(path) as index:
        index.bulk_replace_paper('a', [{'segment_kind': 'document_block', 'section_path': ['Measured conditions'],
                                      'text': 'Thermal conductivity NEW measured boundary.'}])
        index.commit()
        supplement.run_gap_local_triage(gap, **kwargs)
    assert len(calls) == 2
    assert 'NEW measured boundary' in calls[-1]['material_found'][0]['text']


def test_index_addition_reopens_lookup_and_keeps_prior_partial(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity OLD_CONDITION observed baseline.')])
    def answer(payload):
        new = any('NEW_BOUNDARY' in item['text'] for item in payload['material_found'])
        return {**PARTIAL, 'usable_content': 'NEW useful boundary.' if new else 'OLD useful condition.'}
    judge, calls = capture_model(tmp_path, monkeypatch, answer)
    # The production runner creates the production judge itself; the same
    # invoke/client boundary replacements above capture its real messages.
    config = planner.ProgressivePlannerConfig(topic_id='thermal', pool_path=tmp_path/'pool.jsonl',
        plan_path=tmp_path/'plan.json', output_dir=tmp_path/'run', reader_workers=1, chapter_workers=1)
    runner = planner.make_retrieval_loop_runner(config, key_file=tmp_path/'never-read-key',
        budget_ledger_path=tmp_path/'budget.sqlite', budget_limit_cny=1,
        local_index_path=path, allow_external=False)
    request = {'gap_id': 'G', 'gap_question': 'thermal conductivity', 'chapter_ids': ['CH1'],
               'targeted_queries': [{'query_text': 'thermal conductivity'}]}
    first = runner(phase='same_phase', supplement_requests=[request])
    assert 'OLD useful condition.' in first['tool_materials_by_chapter']['CH1'][0]['usable_content']
    with PlanningMaterialIndex(path) as index:
        index.upsert_paper(PaperRecord('b', 'P2', 'Boundary', '2026', '', material_depth='fulltext'))
        index.add_segments('b', [{'segment_kind': 'document_block', 'section_path': ['Boundary'],
                                  'text': 'Thermal conductivity NEW_BOUNDARY measured condition.'}])
        index.commit()
    second = runner(phase='same_phase', supplement_requests=[{**request, 'chapter_ids': ['CH2']}])
    assert len(calls) == 2
    material = second['tool_materials_by_chapter']['CH2'][0]
    assert 'OLD useful condition.' in material['usable_content']
    assert 'NEW useful boundary.' in material['usable_content']
    assert second['retrieval_loop']['needs'][0]['status'] != 'answered'


def test_direct_output_change_reaches_judge_and_retains_supplied_partial(tmp_path, monkeypatch):
    path = index_with(tmp_path, [('a', 'P1', 'thermal conductivity condition and boundary were observed.')])
    judge, calls = capture_model(tmp_path, monkeypatch, lambda payload: {
        **PARTIAL, 'usable_content': 'NEW boundary observation.' if 'boundary' in payload['success_criteria']
        else 'OLD condition observation.'})
    common = {'index_path': path, 'judge': judge, 'cache_dir': tmp_path/'cache'}
    request = {'gap_id': 'G', 'question': 'thermal conductivity', 'required_outputs': ['condition']}
    first = supplement.run_gap_local_triage(request, **common)
    changed = {**request, 'required_outputs': ['boundary'], 'reusable_material': first['usable_content']}
    second = supplement.run_gap_local_triage(changed, **common)
    resumed = supplement.run_gap_local_triage(changed, **common)
    assert len(calls) == 2
    assert calls[0]['success_criteria'] == ['condition']
    assert calls[1]['success_criteria'] == ['boundary']
    assert 'OLD condition observation.' in calls[1]['reusable_material']
    assert all(text in second['writer_material']['usable_content'] for text in
               ('OLD condition observation.', 'NEW boundary observation.'))
    assert second['answers_requested_question'] is False
    assert resumed['model_calls'] == 0 and resumed['usable_content'] == second['usable_content']


@pytest.mark.parametrize('outputs,expected', [
    ('boundary condition', 'boundary condition'),
    ({'output_id': 'O1', 'description': 'boundary condition'}, '{"description": "boundary condition"}'),
])
def test_direct_scalar_output_shapes_are_single_requirements(tmp_path, monkeypatch, outputs, expected):
    path = index_with(tmp_path, [('a', 'P1', 'Thermal conductivity boundary condition was observed.')])
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    supplement.run_gap_local_triage({'gap_id': 'G', 'question': 'thermal conductivity',
                                    'required_outputs': outputs}, index_path=path, judge=judge)
    assert calls[0]['success_criteria'] == [expected]
