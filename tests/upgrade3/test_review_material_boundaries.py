"""R03/R04/R06 offline real consumers; only model transport is substituted."""
import json
from dataclasses import replace
from pathlib import Path
import pytest
from optomind_research.runtime.upgrade3 import planning_pool_adapter as pool
from optomind_research.runtime.upgrade3 import article_evidence as evidence
from optomind_research.runtime.upgrade3.article_delivery import run_article_delivery
from optomind_research.runtime.upgrade3.paper_reading_card import run_paper_reading_card
from optomind_research.runtime.upgrade3.progressive_review_plan import load_planning_pool, _card_for_candidate, _snapshot_for_candidate
from test_reader_helper_token_budgets import card_inputs, CardModelBoundary
from test_manuscript_delivery import source, selected, stub_checker


def batch_fixture(tmp_path):
    inputs = card_inputs(tmp_path)
    card_dir = tmp_path / 'card'
    run_paper_reading_card(**inputs, output_dir=card_dir, client=CardModelBoundary())
    item = {'work_key': 'version-1', 'paper_id': 'legacy-id', 'status': 'success', 'attempt': 1,
            'output_dir': str(card_dir), **{k: str(v) for k,v in inputs.items()}}
    state = tmp_path / 'BATCH_STATE.json'
    state.write_text(json.dumps({'items': {'version-1': item}, 'manifest_finalized': True}))
    return state, item


def test_batch_pool_real_planner_gets_full_card_snapshot(tmp_path):
    state, item = batch_fixture(tmp_path)
    report = pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    rows = load_planning_pool(report['pool_path'])
    assert _card_for_candidate(rows[0])['general_understanding']
    assert _card_for_candidate(rows[0])['review_planning']
    assert _snapshot_for_candidate(rows[0]) == Path(item['snapshot_dir'])
    assert report['model_calls'] == 0
    assert rows[0]['material_depth_label'] == _card_for_candidate(rows[0])['material']['material_scope']


def test_batch_pool_rejects_bad_commit_and_changed_topic(tmp_path):
    state, item = batch_fixture(tmp_path)
    plan = Path(item['plan_path'])
    original = plan.read_text()
    value = json.loads(original)
    value['question_en'] = 'A different scientific question'
    plan.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='input_version_mismatch'):
        pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    assert not (tmp_path/'pool').exists()
    plan.write_text(original)
    card = Path(item['output_dir'])/'PAPER_READING_CARD.json'
    card.write_text(card.read_text() + ' ')
    with pytest.raises(ValueError, match='commit_invalid'):
        pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')


def test_selection_explicit_and_consumer_rejects_tampering(tmp_path):
    state, item = batch_fixture(tmp_path)
    state.write_text(json.dumps({'items': {'v1': item, 'v2': {**item,'work_key':'version-2','attempt':2}}}))
    with pytest.raises(ValueError, match='ambiguous_card_versions'):
        pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    report = pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool', selected_work_keys=['version-2'])
    assert report['selected_cards'][0]['attempt'] == 2
    card = Path(item['output_dir'])/'PAPER_READING_CARD.json'
    card.unlink()
    with pytest.raises(ValueError, match='selected_card_missing'):
        load_planning_pool(report['pool_path'])


def request():
    return evidence.EvidenceRequest('What coefficient applies?', {'temperature':'10 K'},
                                    ('coefficient',), ('parameter_prior','counterexample_mismatch'))


def unit():
    return {'canonical_paper_id':'doi:10.x/test','snapshot_id':'snap-1','source_hash':'a'*64,
            'material_depth':'abstract_only','roles':['parameter_prior','counterexample_mismatch'],
            'anchors':[{'source_block_id':'source-block-1','quote':'A mechanism is proposed.'}],
            'conditions':{'temperature':'unspecified'},'counter_evidence':['Adjacent method failed.']}


def test_paper_presence_is_not_output_fulfillment_and_preview_keeps_counterevidence():
    bundle = evidence.build_evidence_bundle(request(), [unit()])
    assert bundle.material_ready and not bundle.gap_fulfilled
    assert bundle.unmet_outputs == ('coefficient',) and bundle.status == 'partial'
    preview = evidence.research_brief_input(bundle)
    assert preview['evidence_bundle']['source_units'][0]['counter_evidence']
    assert not preview['ranking_is_support_strength']
    assert evidence.EvidenceBundle.from_mapping(json.loads(json.dumps(bundle.to_dict()))).task_id == bundle.task_id
    assert evidence.build_evidence_bundle(replace(request(), required_outputs=('conditions',)),[unit()]).task_id != bundle.task_id
    assert evidence.build_evidence_bundle(request(),[{**unit(),'source_hash':'b'*64}]).task_id != bundle.task_id


def test_answers_require_request_binding_and_navigation_never_suffices():
    u = {**unit(),'fulfilled_outputs':['coefficient']}
    with pytest.raises(ValueError, match='request_binding'):
        evidence.build_evidence_bundle(request(),[u])
    u['request_binding'] = evidence.request_binding(request())
    assert evidence.build_evidence_bundle(request(),[u]).gap_fulfilled
    with pytest.raises(ValueError, match='navigation_is_not_evidence'):
        evidence.build_evidence_bundle(request(),[{**u,'material_depth':'navigation'}])


def article(tmp_path):
    path, config, _ = source(tmp_path)
    config['identity_catalogs'] = []
    return path, config, {'status':'complete','selected_manuscript':selected(path),'citation_namespace':'article',
        'source_identity_map':{'P0001':{'canonical_paper_id':'doi:10.x/test','title':'Synthetic source'}}}


def test_article_full_manuscript_bypasses_front_back_and_flows_selected(tmp_path, monkeypatch):
    path, config, report = article(tmp_path)
    chosen = tmp_path/'chosen.md'
    chosen.write_text(path.read_text().replace('原始摘要。','选用摘要。'))
    seen = {}
    def checker(**kwargs):
        seen.update(kwargs)
        return {'status':'complete_with_diagnostics','selected_manuscript':selected(chosen),'pending':[{'edit_id':'E2'}]}
    stub_checker(monkeypatch,checker)
    out = run_article_delivery(article_report=report, config=config, out_dir=tmp_path/'out')
    assert seen['draft_path'] == path
    assert out['downstream_status'] == 'complete'
    assert '03_front_back' not in out['stages']
    assert '选用摘要。' in Path(out['stages']['05_publication']['published_markdown']).read_text()
    assert not out['front_back_generated'] and not out['pdf_validated']


def test_article_namespace_collision_and_hash_tampering_fail_before_checker(tmp_path):
    path, config, report = article(tmp_path)
    config['identity_catalogs'] = [{'namespace':'other','entries':[{'source_handle':'P0001','paper_id':'other-paper'}]}]
    with pytest.raises(ValueError, match='ambiguous_bare_citation'):
        run_article_delivery(article_report=report, config=config,out_dir=tmp_path/'out')
    path.write_text(path.read_text() + '\nChanged')
    with pytest.raises(ValueError,match='hash_mismatch'):
        run_article_delivery(article_report=report,config=config,out_dir=tmp_path/'out')


def test_trusted_source_actual_reader_reuses_and_required_output_changes_task(tmp_path):
    from optomind_research.runtime.upgrade3.directed_reading import DirectedReadingStore
    inputs = card_inputs(tmp_path)
    u = evidence.source_unit_from_snapshot(snapshot_dir=inputs['snapshot_dir'], roles=['parameter_prior'])
    assert u['anchors'] and all(a['source_block_id'] for a in u['anchors'])
    store = DirectedReadingStore(tmp_path/'readings.sqlite')
    calls = []
    class Client:
        def complete(self, messages, **kwargs):
            calls.append(messages)
            return {'content':json.dumps({'question_material':[{'question_id':'Q01','explanation':'A supplied conditional observation.','remaining_points':'Coefficient not reported.'}]})}
    kwargs = {'source_unit':u,'store':store,'output_dir':tmp_path/'read','client':Client()}
    first = evidence.read_evidence_source(request=request(), **kwargs)
    second = evidence.read_evidence_source(request=request(), **kwargs)
    assert len(calls) == 1 and second['reader_retained'] and not second['reader_network_call']
    changed = evidence.read_evidence_source(request=replace(request(),required_outputs=('conditions',)), **kwargs)
    assert len(calls) == 2
    assert first['reader_interpretation']['task_id'] != changed['reader_interpretation']['task_id']
    assert not evidence.build_evidence_bundle(request(),[second]).gap_fulfilled


def test_card_required_outputs_are_topic_bound_and_legacy_order_preserved(tmp_path):
    state, item = batch_fixture(tmp_path)
    card = json.loads((Path(item['output_dir'])/'PAPER_READING_CARD.json').read_text())
    old = tmp_path/'legacy.jsonl'
    old.write_text(json.dumps({'paper_id':'original-585-id','card_path':'historical-machine/path',
                              'planning_view':card['planning_view']})+'\n')
    report = pool.publish_planning_pool(batch_state_path=state,output_dir=tmp_path/'pool',legacy_pool_path=old)
    assert load_planning_pool(report['pool_path'])[0]['paper_id'] == 'original-585-id'
    assert 'historical-machine/path' in old.read_text()
    plan = Path(item['plan_path'])
    value = json.loads(plan.read_text()); value['required_outputs']=['new quantitative output']
    plan.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='input_version_mismatch'):
        pool.publish_planning_pool(batch_state_path=state,output_dir=tmp_path/'pool2')


def test_article_preview_and_disabled_are_distinct(tmp_path):
    path, config, report = article(tmp_path)
    preview = run_article_delivery(article_report=report,config=config,out_dir=tmp_path/'preview')
    assert preview['halt_reasons'] == ['03_manuscript_checker']
    assert '04_figures_citations' not in preview['stages']
    config['manuscript_checker'] = {'enabled':False}
    disabled = run_article_delivery(article_report=report,config=config,out_dir=tmp_path/'disabled')
    assert disabled['stages']['03_manuscript_checker']['status'] == 'disabled'
    assert disabled['selected_manuscript']['sha256'] == selected(path)['sha256']
    assert disabled['downstream_status'] == 'complete'


def test_local_search_real_index_resolves_sources_without_claiming_answer(tmp_path):
    from optomind_research.runtime.upgrade3.planning_material_search import PlanningMaterialIndex, PaperRecord
    index = PlanningMaterialIndex(tmp_path/'index.sqlite')
    index.upsert_paper(PaperRecord('p1','P0001','Optical coefficient experiment','','',material_depth='abstract_only'))
    index.add_segments('p1',[{'segment_kind':'abstract','text':'The optical coefficient response was measured at different temperatures.','section_path':['Abstract']}])
    calls=[]
    result = evidence.collect_evidence(request(),index=index,source_resolver=lambda hit: calls.append(hit.paper_id) or unit())
    assert calls == ['p1'] and result.material_ready and not result.gap_fulfilled


def test_acquisition_manifest_relative_path_mapping(tmp_path):
    state,item=batch_fixture(tmp_path)
    from optomind_research.runtime.upgrade3.paper_reading_batch import _manifest_records, _normalise_row, _snapshot_material_state
    manifest=pool.acquisition_to_reading_manifest({'canonical_paper_id':'fixture-paper','snapshot_path':item['snapshot_dir']},plan_path=item['plan_path'],bundle_root=tmp_path)
    path=tmp_path/'reading.json'; path.write_text(json.dumps(manifest))
    rows,finalized=_manifest_records(path)
    assert not finalized
    row=_normalise_row(rows[0],path)
    assert _snapshot_material_state(row) == 'ready'
    assert row['snapshot_dir'] == item['snapshot_dir']


def test_reader_failure_retains_source_and_gap_diagnostics(tmp_path):
    from optomind_research.runtime.upgrade3.planning_material_search import PlanningMaterialIndex, PaperRecord
    index = PlanningMaterialIndex(tmp_path/'index.sqlite')
    index.upsert_paper(PaperRecord('p1','P0001','Optical coefficient experiment','','',material_depth='abstract_only'))
    index.add_segments('p1',[{'segment_kind':'abstract','text':'The optical coefficient response was measured at different temperatures.','section_path':['Abstract']}])
    def failed_reader(**kwargs):
        raise TimeoutError('injected source timeout')
    result = evidence.collect_evidence(request(),index=index,source_resolver=lambda hit: unit(),directed_reader=failed_reader)
    assert result.material_ready and result.unmet_outputs == ('coefficient',)
    assert result.diagnostics == ({'stage':'directed_reader','paper_id':'p1','error':'TimeoutError'},)
    assert evidence.EvidenceBundle.from_mapping(json.loads(json.dumps(result.to_dict()))).diagnostics == result.diagnostics


def test_article_selected_result_lineage_reaches_actual_shared_sidecar(tmp_path):
    path,config,report=article(tmp_path)
    report['adopted_result_ref']={'object_id':'selected-result','revision':2,'content_hash':'a'*64}
    config['manuscript_checker']={'enabled':False}
    result=run_article_delivery(article_report=report,config=config,out_dir=tmp_path/'out')
    assert result['stages']['article_complete_input']['adopted_result_ref'] == report['adopted_result_ref']
    sidecar=json.loads(Path(result['stages']['05_publication']['manuscript_selection_path']).read_text())
    assert sidecar['adopted_result_ref'] == report['adopted_result_ref']
    assert not sidecar['scientific_acceptance']


def test_foreign_archive_card_locator_keeps_known_identity_without_live_cache():
    from scripts.upgrade3.full_review_draft import IdentityIndex
    index=IdentityIndex([{'P0001':{'paper_id':'canonical-paper','title':'Known title',
                                'card_path':'F:\\archive\\'+'x'*300+'\\PAPER_READING_CARD.json'}}])
    reference=index.reference('P0001',1,['P0001'])
    assert reference['title'] == 'Known title'
    assert reference['card_locator_status'] == 'unavailable_on_this_machine'
    assert 'authors' not in reference
