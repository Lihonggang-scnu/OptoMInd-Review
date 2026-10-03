"""Bounded offline provenance checks, using real persistence and fake model only."""
import copy
import json

from optomind_research.runtime.upgrade3 import progressive_review_plan as p


class Model:
    model = 'planner-a'
    chapter_model = 'chapter-a'
    output_tokens = 18000
    thinking_budget = 8192

    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == 'chapter_proposals':
            return {'chapter_proposals': [{'chapter_id': payload['chapter']['chapter_id'], 'scope': payload['research_question']}]}
        if stage == 'source_routing':
            return {'source_routes': [{'source_handle': row['source_handle'], 'chapter_ids': ['CH01'], 'specific_usable_material': row['B_review_planning'].get('finding', 'use')} for row in payload['candidate_batch']]}
        return {'answer': len(self.calls)}


def make(tmp_path):
    model = Model()
    return p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig('T', tmp_path/'pool', tmp_path/'plan', tmp_path, chapter_workers=1), planner=model), model


def test_stage_requires_provenance_and_freezes_inputs(tmp_path):
    planner, model = make(tmp_path)
    state = {}
    payload = {'research_question': 'Q', 'material': {'conditions': '25 C'}}
    def call():
        answer = p._call_record(model, 'provisional_scope', payload)
        payload['material']['conditions'] = 'mutated during call'
        return answer
    planner._stage('provisional_scope', call, resume=True, state=state, cache_inputs=payload)
    saved = json.loads((tmp_path/'RUN_STATE.json').read_text())
    assert saved['stage_inputs']['provisional_scope']['material']['conditions'] == '25 C'
    payload['material']['conditions'] = '25 C'
    planner._stage('provisional_scope', call, resume=True, state=state, cache_inputs=payload)
    assert len(model.calls) == 1
    planner._stage('provisional_scope', call, resume=True, state=state)
    assert len(model.calls) == 2
    assert json.loads((tmp_path/'stages/provisional_scope.json').read_text())['response']['answer'] == 2


def test_stage_changes_and_legacy_provenance(tmp_path, monkeypatch):
    planner, model = make(tmp_path)
    state = {}
    payload = {'research_question': 'Q', 'scope': 'S', 'material': {'conditions': '25 C', 'year': 2020, 'time': '24 h', 'card_path': '/old', 'updated_at': 'old'}}
    def run():
        return planner._stage('harmonized_scope', lambda: p._call_record(model, 'harmonize_scope', payload), resume=True, state=state, cache_inputs=payload)
    run(); run()
    assert len(model.calls) == 1
    payload['material'].update(card_path='/new', updated_at='new')
    run()
    assert len(model.calls) == 1
    for key, val in [('research_question','Q2'), ('scope','S2')]:
        payload[key] = val; run()
    for key, val in [('conditions','80 C'), ('year',2021), ('time','48 h')]:
        payload['material'][key] = val; run()
    assert len(model.calls) == 6
    model.model = 'planner-b'; run()
    model.output_tokens = 17000; run()
    model.thinking_budget = 4096; run()
    original = p._messages_for
    monkeypatch.setattr(p, '_messages_for', lambda stage, data: original(stage, data) + [{'role':'system','content':'new prompt'}])
    run(); run()
    assert len(model.calls) == 10
    state.pop('stage_cache_contracts'); run()
    assert len(model.calls) == 11
    assert json.loads((tmp_path/'RUN_STATE.json').read_text())['stage_cache_contracts']['harmonized_scope']


def test_proposals_reuse_only_compatible_effective_inputs(tmp_path):
    planner, model = make(tmp_path)
    outline = {'shared_outline': {'chapters':[{'chapter_id':'CH01','title':'One'}]}}
    routing = {'source_routes': [], 'pool_sources': 1}
    def run(topic='Q'):
        return planner._propose_chapters(topic=topic, outline=outline, routing=routing, resume=True)
    run(); run(); assert len(model.calls) == 1
    run('Q2'); assert len(model.calls) == 2
    planner.tool_materials_by_chapter['CH01'] = [{'usable_content':'corrected'}]
    run('Q2'); assert len(model.calls) == 3
    saved = json.loads((tmp_path/'stages/chapter_proposals/CH01.json').read_text())
    assert saved['cache_contract']


def test_route_batches_reuse_only_compatible_effective_inputs(tmp_path):
    planner, model = make(tmp_path)
    state = {'topic':'Q'}
    pool = [{'_source_handle':'P0001', '_b_summary': {'title':'paper', 'finding':'old', 'year':2020}}]
    outline = {'chapters':[{'chapter_id':'CH01','title':'One'}]}
    def run():
        return planner._route_sources(pool, shared_outline=outline, resume=True, state=state)
    run(); run(); assert len(model.calls) == 1
    pool[0]['_b_summary']['finding'] = 'corrected'; run()
    state['topic'] = 'Q2'; run()
    model.model = 'new'; run()
    assert len(model.calls) == 4
    saved = json.loads((tmp_path/'stages/source_routing/batch_001.json').read_text())
    assert saved['cache_contract']
    assert saved['source_routes'][0]['specific_usable_material'] == 'corrected'


def test_scientific_timestamps_preserved_and_unrelated_stage_reuses(tmp_path):
    planner, model = make(tmp_path)
    state = {}
    payload = {'research_question':'Q', 'source_materials':[{'result_path':'/old', 'fetched_at':'old', 'study_summary_A':{'updated_at':'study time 1'}}]}
    planner._model_stage('level1_outline', {'research_question':'unrelated'}, resume=True, state=state)
    def run():
        return planner._model_stage('provisional_scope', payload, resume=True, state=state)
    run()
    payload['source_materials'][0].update(result_path='/new', fetched_at='new'); run()
    assert len(model.calls) == 2
    payload['source_materials'][0]['study_summary_A']['updated_at'] = 'study time 2'; run()
    planner._model_stage('level1_outline', {'research_question':'unrelated'}, resume=True, state=state)
    assert len(model.calls) == 3
    saved = json.loads((tmp_path/'RUN_STATE.json').read_text())
    assert saved['stage_inputs']['provisional_scope']['source_materials'][0]['study_summary_A']['updated_at'] == 'study time 2'
    assert len(saved['stage_cache_contracts']) == 2


def test_tool_cycle_outer_provenance_tracks_plan_and_pool(tmp_path):
    planner, model = make(tmp_path)
    state = {}
    plan = {'question':'Q'}
    pool = [{'_paper_id':'one', '_b_summary':{'finding':'original'}}]
    def run():
        return planner._tool_cycle(phase='level1', supplement_requests=[], directed_requests=[], pool_rows=pool, plan=plan, prior_directed=None, prior_tool_results=None, source_handle_map={'P0001':'one'}, resume=True, state=state)
    run()
    first = json.loads((tmp_path/'RUN_STATE.json').read_text())['stage_cache_contracts']['level1_tools']
    run()
    assert state['stage_cache_contracts']['level1_tools'] == first
    pool[0]['_b_summary']['finding'] = 'corrected'; run()
    second = state['stage_cache_contracts']['level1_tools']
    assert second != first
    plan['question'] = 'new question'; run()
    assert state['stage_cache_contracts']['level1_tools'] != second
    assert json.loads((tmp_path/'stages/level1_tools.json').read_text())['phase'] == 'level1'
    assert model.calls == []


def test_legacy_proposal_and_route_records_refresh_locally(tmp_path):
    planner, model = make(tmp_path)
    outline = {'shared_outline': {'chapters':[{'chapter_id':'CH01','title':'One'}]}}
    routing = {'source_routes': [], 'pool_sources':1}
    planner._propose_chapters(topic='Q', outline=outline, routing=routing, resume=True)
    path = tmp_path/'stages/chapter_proposals/CH01.json'
    old = json.loads(path.read_text()); old.pop('cache_contract'); path.write_text(json.dumps(old))
    planner._propose_chapters(topic='Q', outline=outline, routing=routing, resume=True)
    assert len(model.calls) == 2
    pool = [{'_source_handle':'P0001','_b_summary':{'title':'paper'}}]
    planner._route_sources(pool, shared_outline=outline['shared_outline'], resume=True, state={'topic':'Q'})
    path = tmp_path/'stages/source_routing/batch_001.json'
    old = json.loads(path.read_text()); old.pop('cache_contract'); path.write_text(json.dumps(old))
    planner._route_sources(pool, shared_outline=outline['shared_outline'], resume=True, state={'topic':'Q'})
    assert len(model.calls) == 4
    # The independent compatible proposal remains reusable after route refresh.
    planner._propose_chapters(topic='Q', outline=outline, routing=routing, resume=True)
    assert len(model.calls) == 4
    assert json.loads(path.read_text())['cache_contract']


def test_material_envelope_metadata_reuses_but_scientific_time_invalidates(tmp_path):
    planner, model = make(tmp_path)
    state = {}
    material = {'result_path':'/old/result', 'fetched_at':'old fetch',
                'question_material':{'content':'finding', 'updated_at':'study time 1', 'time':'24 h'}}
    payload = {'research_question':'Q', 'source_materials':[{'supplement_gap_material': material}]}
    def run():
        return planner._model_stage('provisional_scope', payload, resume=True, state=state)
    run()
    material.update(result_path='/new/result', fetched_at='new fetch'); run()
    assert len(model.calls) == 1
    material['question_material']['updated_at'] = 'study time 2'; run()
    assert len(model.calls) == 2
    material['question_material']['time'] = '48 h'; run()
    assert len(model.calls) == 3
    saved = json.loads((tmp_path/'RUN_STATE.json').read_text())
    saved_material = saved['stage_inputs']['provisional_scope']['source_materials'][0]['supplement_gap_material']
    assert saved_material['result_path'] == '/new/result'
    assert saved_material['question_material']['updated_at'] == 'study time 2'
    assert saved_material['question_material']['time'] == '48 h'
    assert json.loads((tmp_path/'stages/provisional_scope.json').read_text())['response']['answer'] == 3
