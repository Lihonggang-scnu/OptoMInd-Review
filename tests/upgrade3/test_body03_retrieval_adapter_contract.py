"""WO03 adapter/store regressions; only provider/model boundaries are replaced."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p
from optomind_research.runtime.upgrade3 import planning_supplement as supplement

ARCHIVE = Path(__file__).resolve().parents[2] / 'docs/acceptance/body02-20261003'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def fixture(tmp_path, monkeypatch, *, judgments=None):
    plan = {
        'schema_version': 'research_harness.query_plan.v2', 'question_en': 'How does the mechanism depend on conditions?',
        'research_object': 'mechanism', 'ambiguity': {'is_ambiguous': False, 'default_reading': '', 'needs_user_input': []},
        'facets': [{'id': 'F1', 'ask': 'mechanism conditions', 'keyword_queries': ['mechanism conditions'],
                    'question_queries': ['Which conditions change the mechanism?'],
                    'filters': {'publication_type': [], 'fields_of_study': [], 'text_availability': []}, 'must_exclude': []}],
        'seeds': [], 'criteria': {'must_include_topic': [], 'must_exclude_domain': [], 'synonyms': {}}, 'additional_constraints': [],
    }
    config = p.ProgressivePlannerConfig(topic_id='wo03-adapter', pool_path=tmp_path/'POOL.jsonl',
                plan_path=tmp_path/'PLAN.json', output_dir=tmp_path/'run', reader_workers=1, chapter_workers=1,
                planning_revision_enabled=True)
    p._atomic_json(config.plan_path, plan)
    snapshot = tmp_path/'existing/snapshot'
    snapshot.mkdir(parents=True)
    (snapshot/'READING_VIEW.md').write_text('The mechanism was measured at 25 C. A second experiment measured the boundary at 60 C.')
    p._atomic_json(snapshot/'manifest.json', {'snapshot_id': 'snapshot'})
    card = tmp_path/'existing/card/PAPER_READING_CARD.json'
    p._atomic_json(card, {'material': {'snapshot_id': 'snapshot'}, 'paper_identity': {'canonical_paper_id': 'paper-1', 'title': 'Condition study'},
                         'general_understanding': {'finding': 'The mechanism was measured at 25 C.'},
                         'review_planning': {'planning_summary': 'Compare the measured conditions.'}})
    p._atomic_json(tmp_path/'existing/SOURCE_UNIT.json', {'snapshot_path': str(snapshot)})
    pool = [{'paper_id': 'paper-1', '_paper_id': 'paper-1', '_source_handle': 'P0001', 'card_path': str(card),
             'planning_view': {'paper_identity': {'canonical_paper_id': 'paper-1', 'title': 'Condition study'}},
             '_b_summary': {'declared_content_depth': 'fulltext'}}]
    config.pool_path.write_text(json.dumps(pool[0])+'\n')
    calls = {'queries': [], 'judgments': []}

    class Gateway:
        def search_papers(self, query, **kwargs):
            calls['queries'].append(query)
            return [], SimpleNamespace(status_code=200)

    answers = judgments or [{'status': 'fulfilled', 'useful_material': 'Measured mechanism at 25 C.', 'remaining_gap': ''}]

    def judge(**kwargs):
        calls['judgments'].append(copy.deepcopy(kwargs['gap']))
        answer = answers[min(len(calls['judgments'])-1, len(answers)-1)]
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    monkeypatch.setattr(supplement, 'CompositeS2OpenAlexGateway', lambda *args: Gateway())
    monkeypatch.setattr(supplement, 'run_qwen_fulfillment_judge', lambda **kwargs: judge)
    adapter = p.make_planning_supplement_runner(config, key_file=tmp_path/'absent-key',
                budget_ledger_path=tmp_path/'budget.sqlite', budget_limit_cny=30)
    return config, plan, pool, calls, adapter


def gap(owner='CH01', outputs=('condition',)):
    return {'gap_id': 'G1', 'gap_question': 'Which conditions change the mechanism?', 'chapter_ids': [owner],
            'success_criteria': ['Direct measured conditions'], 'required_outputs': list(outputs),
            'targeted_queries': [{'query_type': 'keyword', 'query_text': 'mechanism conditions', 'facet_id': 'F1'}],
            'known_papers': [{'paper_id': 'paper-1', 'canonical_paper_id': 'paper-1', 'title': 'Condition study'}]}


def run(config, plan, pool, adapter, phase, request, **kwargs):
    runner = p.make_retrieval_loop_runner(config, allow_external=True, supplement_runner=adapter)
    return runner(phase=phase, supplement_requests=[request], pool_rows=pool, plan=plan,
                  source_handle_map={'P0001': 'paper-1'}, **kwargs)


def test_cross_stage_answer_reuses_real_supplement_and_rebinds_after_restart(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    first = run(config, plan, pool, adapter, 'level2', gap())
    assert first['retrieval_loop']['needs'][0]['status'] == 'answered'
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert len(calls['queries']) == len(calls['judgments']) == 1
    assert second['retrieval_loop']['needs'][0]['status'] == 'answered'
    assert first['tool_materials_by_chapter']['CH01'][0]['usable_content'] == second['tool_materials_by_chapter']['CH02'][0]['usable_content']
    assert second['supplement_results']


def test_changed_outputs_only_requests_missing_and_retains_old_material(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        {'status': 'fulfilled', 'useful_material': 'ORIGINAL measured mechanism at 25 C.', 'remaining_gap': ''},
        {'status': 'fulfilled', 'useful_material': 'ADDED boundary at 60 C.', 'remaining_gap': ''},
    ])
    first = run(config, plan, pool, adapter, 'level2', gap())
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02', ('condition', 'boundary')))
    assert len(calls['queries']) == 2
    latest = second['supplement_results'][-1]['results'][0]
    request = read(Path(latest['output_dir'])/'REQUEST.json')
    assert request['required_outputs'] == ['boundary']
    assert request['success_criteria'] == ['boundary']
    material = second['tool_materials_by_chapter']['CH02'][0]
    assert 'ORIGINAL' in material['usable_content'] and 'ADDED' in material['usable_content']
    assert second['retrieval_loop']['needs'][0]['status'] == 'answered'
    old = first['supplement_results'][0]['results'][0]
    assert Path(old['output_dir']).is_dir() and old['output_dir'] != latest['output_dir']


def test_coordinator_archive_feedback_reaches_real_owner_input(tmp_path):
    source = read(ARCHIVE/'owner/final_owner_delta_CH02.json')
    improvement = read(ARCHIVE/'owner/final_whole_plan_improvement.json')['parsed_response']
    packet = copy.deepcopy(source['cache_inputs'])
    packet['shared_outline'] = {'chapters': [packet['chapter']]}
    seen = {}

    def model(stage, payload):
        if stage == 'whole_plan_improvement':
            return copy.deepcopy(improvement)
        assert stage == 'affected_chapter_revision'
        seen.update(copy.deepcopy(payload))
        return {'status': 'no_changes_needed', 'rationale': 'Offline handoff inspection only.'}

    engine = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id='wo03-owner-feedback',
        plan_path=tmp_path/'PLAN.json', pool_path=tmp_path/'POOL.jsonl', output_dir=tmp_path/'run',
        chapter_workers=1, planning_revision_enabled=True), planner=model)
    engine._post_case_review(root=engine.config.output_dir, topic=packet['research_question'],
        harmonized={'shared_outline': packet['shared_outline']}, level1_outline={}, detail_records=[packet],
        baseline_detail_records=[copy.deepcopy(packet)], case_record={}, level1_tool_result={}, level2_tool_result={},
        chapter_tool_result={}, editorial_feedback={}, pool_rows=[], resume=False, state={})
    for row in improvement['chapter_feedback']:
        assert row in seen['chapter_feedback']
    assert {'P0586', 'P0004'} <= {row['source_handle'] for row in seen['source_materials']}
    saved = read(engine.config.output_dir/'stages/affected_chapter_revision/CH02.json')
    assert all(row in saved['cache_inputs']['chapter_feedback'] for row in improvement['chapter_feedback'])


def test_failed_attempt_is_preserved_and_retry_material_reaches_pool_and_owner(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        TimeoutError('offline provider interruption'),
        {'status': 'fulfilled', 'useful_material': 'RETRY measured boundary at 60 C.', 'remaining_gap': ''},
    ])
    result = run(config, plan, pool, adapter, 'retry', gap())
    assert len(calls['queries']) == len(calls['judgments']) == 2
    attempts = sorted((config.output_dir/'retry/external').glob('*/round_1/supplements/G1/attempt-*'))
    assert len(attempts) == 2
    indexes = [read(path/'SUPPLEMENT_INDEX.json') for path in attempts]
    assert {item['fulfillment_judgment']['outcome'] for item in indexes} >= {'retryable_provider_outage'}
    assert result['retrieval_loop']['needs'][0]['status'] == 'answered'
    raw = result['supplement_results']
    actual = raw[-1]['results'][0]
    assert 'RETRY' in result['tool_materials_by_chapter']['CH01'][0]['usable_content']
    target = copy.deepcopy(pool)
    p._merge_supplement_pool_updates(target, result)
    assert 'RETRY' in json.dumps(target)
    assert Path(actual['derived_pool_path']).parent == Path(actual['output_dir'])
    assert read(Path(actual['output_dir'])/'REQUEST.json')['required_outputs'] == ['condition']


def test_partial_reuses_prose_without_closing_and_hands_unresolved_feedback_to_judge(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        {'status': 'partial', 'useful_material': 'Useful observed condition.', 'remaining_gap': 'Need the boundary measurement.'},
        {'status': 'partial', 'useful_material': 'Another useful observation.', 'remaining_gap': 'Boundary remains unmeasured.'},
    ])
    first = run(config, plan, pool, adapter, 'level2', gap())
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert len(calls['judgments']) == 2
    assert calls['judgments'][1]['still_missing'] == 'Need the boundary measurement.'
    assert 'Useful observed condition.' in calls['judgments'][1]['reusable_material']
    assert second['retrieval_loop']['needs'][0]['status'] != 'answered'
    assert 'Useful observed condition.' in second['tool_materials_by_chapter']['CH02'][0]['usable_content']
    assert 'Boundary remains unmeasured.' in second['tool_materials_by_chapter']['CH02'][0]['still_missing']


def test_same_gap_changed_question_and_scope_are_not_completed_by_old_answer(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    first = run(config, plan, pool, adapter, 'level2', gap())
    changed = {**gap('CH02'), 'gap_question': 'Which mechanism controls the second condition?'}
    run(config, plan, pool, adapter, 'changed_question', changed, prior_tool_results=first)
    run(config, {**plan, 'question_en': 'A distinct research object and scope'}, pool, adapter, 'changed_scope', gap())
    assert len(calls['queries']) == 3


def test_covered_subset_has_no_generic_retrieval(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    run(config, plan, pool, adapter, 'level2', gap(outputs=('condition', 'boundary')))
    result = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert len(calls['queries']) == 1
    assert result['retrieval_loop']['needs'][0]['status'] == 'answered'


def test_same_batch_same_question_different_acceptance_keeps_correct_requests(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    requests = [gap('CH01'), gap('CH02', ('boundary',))]
    runner = p.make_retrieval_loop_runner(config, supplement_runner=adapter)
    result = runner(phase='same_batch', supplement_requests=requests, pool_rows=pool, plan=plan)
    assert len(calls['queries']) == 2
    assert [row['required_outputs'] for row in calls['judgments']] == [['condition'], ['boundary']]
    assert set(result['tool_materials_by_chapter']) == {'CH01', 'CH02'}


def test_same_batch_relabelled_outputs_merge_owners_without_losing_request(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    requests = [gap('CH01', ({'output_id': 'O1', 'description': 'Measured condition', 'output_type': 'comparison'},)),
                gap('CH02', ({'output_id': 'O9', 'description': 'Measured condition', 'output_type': 'comparison'},))]
    runner = p.make_retrieval_loop_runner(config, supplement_runner=adapter)
    result = runner(phase='same_batch', supplement_requests=requests, pool_rows=pool, plan=plan)
    assert len(calls['queries']) == 1
    assert set(result['tool_materials_by_chapter']) == {'CH01', 'CH02'}
    assert len(result['retrieval_loop']['needs']) == 1


def test_same_gap_material_history_survives_upgrade_and_stale_replay():
    old = {'paper_id': 'paper-1', 'card_path': 'OLD_CARD', 'planning_view': {'text': 'OLD_B'},
           'supplement_version': 1, 'supplement_source_unit_id': 'old-unit',
           'supplement_gap_material': {'gap_id': 'G1', 'useful_material': 'OLD_CONDITION'}}
    new = {**old, 'card_path': 'NEW_CARD', 'planning_view': {'text': 'NEW_B'}, 'supplement_version': 2,
           'supplement_source_unit_id': 'new-unit', 'supplement_gap_material': {'gap_id': 'G1', 'useful_material': 'NEW_BOUNDARY'}}
    pool = []
    for row in (old, new, old):
        p._merge_supplement_pool_updates(pool, {'supplement_results': [{'candidate_rows': [row]}]})
    assert pool[0]['card_path'] == 'NEW_CARD'
    assert pool[0]['planning_view']['text'] == 'NEW_B'
    assert pool[0]['supplement_gap_material']['useful_material'] == 'NEW_BOUNDARY'
    assert {row['useful_material'] for row in pool[0]['supplement_gap_materials']} == {'OLD_CONDITION', 'NEW_BOUNDARY'}
    current = [{'_paper_id': 'paper-1', 'paper_id': 'paper-1', 'card_path': 'CORRECTED_CARD', 'planning_view': {'text': 'CORRECTED_B'}}]
    p._merge_supplement_pool_updates(current, {'supplement_results': [{'candidate_rows': [old]}]}, preserve_current_material=True)
    assert current[0]['card_path'] == 'CORRECTED_CARD' and current[0]['planning_view']['text'] == 'CORRECTED_B'
    assert 'OLD_CONDITION' in json.dumps(current[0]['supplement_gap_materials'])


@pytest.mark.parametrize('status', ['partial', 'failed', 'unmet'])
def test_metadata_light_feedback_reaches_coordinator_and_chapter_messages(tmp_path, status):
    feedback = {'status': status, 'chapter_ids': ['CH01'], 'usable_content': 'Usable condition from failed attempt.',
                'still_missing': 'Missing boundary.', 'error': 'ProviderIssue' if status == 'failed' else '',
                'derived_pool_path': 'actual-attempt/PLANNING_POOL.jsonl'}
    tools = {'supplement_results': [{'results': [feedback]}]}
    compact = p.ProgressiveReviewPlanner._compact_tool_feedback({'level2': tools})
    assert compact['level2']['supplement_results'][0]['results'][0] == feedback
    messages = []

    def model(stage, payload):
        assert stage == 'chapter_details'
        messages.append(copy.deepcopy(payload))
        return {'chapter_plan': {'thesis': 'Explain condition', 'units': [{'substantive_point': 'Measured condition',
                    'paragraph_briefs': [{'point': 'Condition', 'development': 'Use the observed measurement.'}]}]}}

    engine = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id='feedback', plan_path=tmp_path/'PLAN.json',
        pool_path=tmp_path/'POOL.jsonl', output_dir=tmp_path/'run', chapter_workers=1), planner=model)
    engine._chapter_details(chapters=[{'chapter_id': 'CH01', 'title': 'Conditions', 'source_ids': []}],
        shared_outline={}, topic='Mechanism conditions', candidates={}, level1_tools={}, level2_tools=tools,
        resume=False, state={})
    actual = messages[0]['relevant_tool_feedback'][0]
    for key, value in feedback.items():
        assert actual[key] == value


@pytest.mark.parametrize('change', ['card', 'supplement'])
def test_changed_used_source_invalidates_same_phase_answer_and_journal(tmp_path, monkeypatch, change):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        {'status': 'fulfilled', 'useful_material': 'OLD conclusion.', 'remaining_gap': ''},
        {'status': 'fulfilled', 'useful_material': 'CORRECTED conclusion.', 'remaining_gap': ''},
    ])
    first = run(config, plan, pool, adapter, 'same_phase', gap())
    p._merge_supplement_pool_updates(pool, first)
    if change == 'card':
        card_path = Path(pool[0]['card_path'])
        card = read(card_path)
        card['general_understanding']['finding'] = 'CORRECTED authoritative measurement.'
        p._atomic_json(card_path, card)
    else:
        pool[0]['supplement_gap_material']['useful_material'] = 'CORRECTED current supplemental source.'
        for item in pool[0]['supplement_gap_materials']:
            item['useful_material'] = 'CORRECTED current supplemental source.'
    second = run(config, plan, pool, adapter, 'same_phase', gap('CH02'))
    assert len(calls['judgments']) == 2
    assert not second['retrieval_loop']['needs'][0]['reused_answer']
    current = second['tool_materials_by_chapter']['CH02'][0]['usable_content']
    assert 'CORRECTED conclusion.' in current and 'OLD conclusion.' not in current
    assert first['retrieval_loop']['needs'][0]['need_id'] != second['retrieval_loop']['needs'][0]['need_id']


def test_own_material_augmentation_and_unrelated_pool_addition_do_not_invalidate_answer(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    first = run(config, plan, pool, adapter, 'level2', gap())
    p._merge_supplement_pool_updates(pool, first)
    pool.append({'paper_id': 'unrelated', '_paper_id': 'unrelated', 'planning_view': {'planning_summary': 'Different subject'}})
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert len(calls['judgments']) == 1
    assert second['retrieval_loop']['needs'][0]['reused_answer']


@pytest.mark.parametrize('answer', [
    {'status': 'fulfilled', 'question': 'Which conditions?', 'required_outputs': ['condition']},
    {'status': 'partial', 'useful_material': {'question_id': 'Q1', 'question': 'Which conditions?'}},
])
def test_empty_or_shape_only_material_never_answers(tmp_path, monkeypatch, answer):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[answer])
    first = run(config, plan, pool, adapter, 'level2', gap())
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert first['retrieval_loop']['needs'][0]['status'] != 'answered'
    assert second['retrieval_loop']['needs'][0]['status'] != 'answered'
    assert len(calls['judgments']) == 2


@pytest.mark.parametrize('outputs', ['boundary', {'output_id': 'O1', 'description': 'Boundary conditions', 'output_type': 'comparison'}])
def test_scalar_required_outputs_are_single_requirements(tmp_path, monkeypatch, outputs):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    request = {**gap(), 'required_outputs': outputs}
    result = run(config, plan, pool, adapter, 'scalar', request)
    assert calls['judgments'][0]['required_outputs'] == [outputs]
    saved = read(Path(result['supplement_results'][0]['results'][0]['output_dir'])/'REQUEST.json')
    assert saved['required_outputs'] == [outputs]


def test_changed_outputs_partial_material_survives_real_pool_merge(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        {'status': 'fulfilled', 'useful_material': 'OLD measured condition.', 'remaining_gap': ''},
        {'status': 'partial', 'useful_material': 'NEW useful boundary observation.', 'remaining_gap': 'Boundary still incomplete.'},
    ])
    first = run(config, plan, pool, adapter, 'level2', gap())
    p._merge_supplement_pool_updates(pool, first)
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02', ('condition', 'boundary')))
    p._merge_supplement_pool_updates(pool, second)
    assert calls['judgments'][1]['required_outputs'] == ['boundary']
    assert 'NEW useful boundary observation.' in json.dumps(pool)
    assert 'OLD measured condition.' in json.dumps(pool)
    assert second['retrieval_loop']['needs'][0]['status'] != 'answered'


def test_empty_first_query_refiner_crosses_actual_adapter_with_valid_facet(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.module4 import runtime
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    refinements = []
    monkeypatch.setattr(runtime, 'QwenDirectClient', lambda **kwargs: object())

    def invoke(client, messages, **kwargs):
        refinements.append(copy.deepcopy(messages))
        return {'content': json.dumps({'targeted_queries': [{'query_type': 'keyword', 'query_text': 'specific measured mechanism'}]})}

    monkeypatch.setattr(runtime, 'invoke_client', invoke)
    runner = p.make_retrieval_loop_runner(config, supplement_runner=adapter, key_file=tmp_path/'absent-key',
                budget_ledger_path=tmp_path/'budget.sqlite', budget_limit_cny=30)
    request = {**gap(), 'targeted_queries': []}
    result = runner(phase='initial_query', supplement_requests=[request], pool_rows=pool, plan=plan)
    assert len(refinements) == 1
    assert calls['queries'] == ['specific measured mechanism']
    assert result['retrieval_loop']['needs'][0]['status'] == 'answered'
    path = result['supplement_results'][0]['results'][0]['output_dir']
    actual = read(Path(path)/'REQUEST.json')
    assert actual['targeted_queries'][0]['facet_id'] == 'F1'


def test_returned_attempt_paths_reach_actual_chapter_owner_message(tmp_path, monkeypatch):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch)
    result = run(config, plan, pool, adapter, 'level2', gap())
    actual = result['supplement_results'][0]['results'][0]
    p._merge_supplement_pool_updates(pool, result)
    messages = []

    def model(stage, payload):
        assert stage == 'chapter_details'
        messages.append(copy.deepcopy(payload))
        return {'chapter_plan': {'thesis': 'Measured conditions', 'units': [{'substantive_point': 'Mechanism',
                    'paragraph_briefs': [{'point': 'Condition', 'development': 'Measured mechanism'}]}]}}

    engine = p.ProgressiveReviewPlanner(config, planner=model)
    engine._chapter_details(chapters=[{'chapter_id': 'CH01', 'title': 'Conditions', 'source_ids': ['paper-1']}],
        shared_outline={}, topic=plan['question_en'], candidates={'paper-1': pool[0]}, level1_tools={},
        level2_tools=result, resume=False, state={})
    feedback = next(row for row in messages[0]['relevant_tool_feedback'] if row.get('gap_id') == 'G1')
    assert feedback['output_dir'] == actual['output_dir']
    assert feedback['derived_pool_path'] == actual['derived_pool_path']
    source = next(row for row in messages[0]['source_materials'] if row['source_handle'] == 'P0001')
    assert Path(actual['output_dir']) in Path(source['supplement_material']['judgment_path']).parents


@pytest.mark.parametrize('field', ['summary', 'mechanisms'])
def test_supported_structured_prose_is_answered_and_reused(tmp_path, monkeypatch, field):
    config, plan, pool, calls, adapter = fixture(tmp_path, monkeypatch, judgments=[
        {'status': 'fulfilled', field: 'Measured mechanism at 25 C in a controlled comparison.'},
    ])
    first = run(config, plan, pool, adapter, 'level2', gap())
    second = run(config, plan, pool, adapter, 'chapters', gap('CH02'))
    assert first['retrieval_loop']['needs'][0]['status'] == 'answered'
    assert second['retrieval_loop']['needs'][0]['reused_answer']
    assert len(calls['judgments']) == 1


def test_actual_directed_reader_attempt_is_delivered_once(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import directed_reading as dr
    config, plan, pool, _, _ = fixture(tmp_path, monkeypatch)
    calls = []
    key = tmp_path/'synthetic-placeholder.txt'
    key.write_text('not-a-real-credential')
    card = read(pool[0]['card_path'])
    card['material']['material_scope'] = 'fulltext'
    p._atomic_json(Path(pool[0]['card_path']), card)

    class ModelBoundary:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, **kwargs):
            calls.append(messages)
            return {'content': json.dumps({'question_material': [{'question_id': 'Q01',
                    'explanation': 'Measured mechanism at 25 C.', 'remaining_points': []}]})}

    monkeypatch.setattr(dr, 'QwenDirectClient', ModelBoundary)
    reader = p.make_directed_reading_runner(config, key_file=key, budget_ledger_path=tmp_path/'reader-ledger.sqlite', budget_limit_cny=30)
    runner = p.make_retrieval_loop_runner(config, directed_reader=reader)
    request = {'paper_id': 'paper-1', 'chapter_ids': ['CH01'],
               'questions': [{'question_id': 'Q01', 'question': 'Which condition?', 'purpose': 'Explain conditions', 'required_output_ids': ['O1']}],
               'required_outputs': [{'output_id': 'O1', 'description': 'Measured conditions'}]}
    result = runner(phase='directed', directed_requests=[request], pool_rows=pool, plan=plan)
    assert len(calls) == 1
    assert result['retrieval_loop']['needs'][0]['status'] == 'answered'
    assert len(result['directed_results']) == 1
    assert Path(result['directed_results'][0]['results'][0]['output_dir']).is_dir()
