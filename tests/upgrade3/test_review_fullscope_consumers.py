"""Real offline entry/consumer chains. Only external model/provider transports fake."""
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import pytest
from test_review_material_boundaries import batch_fixture
from test_reader_helper_token_budgets import CardModelBoundary, card_inputs
from optomind_research.runtime.upgrade3.planning_pool_adapter import publish_planning_pool, digest, reading_manifest_from_navigation
from optomind_research.runtime.upgrade3.review_material_pipeline import run_material_pipeline
from optomind_research.runtime.upgrade3.article_evidence import EvidenceRequest, source_unit_from_snapshot, request_binding
from optomind_research.runtime.upgrade3.evidence_service import create_evidence_service
from optomind_research.runtime.upgrade3.evidence_assessment import apply_output_assessments
from optomind_research.runtime.upgrade3.plan_contract import load_plan
from optomind_research.runtime.upgrade3.scholarly_skeleton import _verified_pool_reading, _coverage_with_reading
from optomind_research.runtime.upgrade3.offline_replay import canonical_hash, model_request_hash


def navigation(tmp_path, plan):
    _, receipt = load_plan(plan)
    cards = tmp_path / 'CARDS.json'; portfolio = tmp_path / 'PORTFOLIO.json'
    cards.write_text(json.dumps({'fixture-paper': {'paper_identity': {'paper_id': 'fixture-paper'}}}))
    portfolio.write_text(json.dumps({'deep_analysis_corpus': ['fixture-paper'], 'priority_portfolio': []}))
    path = tmp_path / 'NAV.json'
    path.write_text(json.dumps({'schema_version': 'optomind.reading_navigation.v1', 'run_id': 'fixture-run',
        'plan_hash': receipt['plan_sha256'], **{key: {'path': str(file), 'sha256': digest(file),
            'run_id': 'fixture-run', 'plan_hash': receipt['plan_sha256']} for key,file in [('cards',cards),('portfolio',portfolio)]}}))
    return path


def service_fixture(tmp_path, **options):
    state, item = batch_fixture(tmp_path)
    pool = publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    config = {'schema_version': 'review.evidence_service.v1', 'mode': 'replay',
        'planning_pool_path': pool['pool_path'], 'output_dir': str(tmp_path/'service'), **options}
    return config, item


def request():
    return EvidenceRequest('synthetic device response', {}, ('response',), ('model_mechanism',),
                           budget={'max_papers': 4, 'max_supplementary_calls': 1})


class Reader:
    def __init__(self, unit, *, status='fulfilled', quote=None):
        self.unit, self.status, self.quote, self.calls = unit, status, quote, []
    def complete(self, messages, **kwargs):
        self.calls.append((messages,kwargs))
        anchor = next(a for a in self.unit['anchors'] if 'doubles' in a['quote'])
        return {'content': json.dumps({'question_material': [{'question_id': 'Q01',
            'explanation': 'The synthetic response doubles.', 'remaining_points': ''}],
            'output_assessments': [{'output_id': 'O01', 'status': self.status,
                'answer': 'The response doubles.', 'reason': 'Exact observation in source.',
                'applicability': 'direct', 'conditions': {},
                'source_refs': [{'source_block_id': anchor['source_block_id'], 'quote': self.quote or anchor['quote']}]}]})}


def test_navigation_to_existing_batch_pool_and_planner(tmp_path):
    inputs = card_inputs(tmp_path)
    nav = navigation(tmp_path, inputs['plan_path'])
    checkpoint = tmp_path/'CHECKPOINT.json'
    checkpoint.write_text(json.dumps({'items': [{'paper_id':'fixture-paper','snapshot_path':str(inputs['snapshot_dir'])},
                                              {'paper_id':'unselected','snapshot_path':'bad'}]}))
    config = {'navigation_manifest_path': str(nav), 'plan_path': str(inputs['plan_path']),
              'checkpoint_path': str(checkpoint), 'output_dir': str(tmp_path/'pipeline'),
              'mode': 'preview', 'batch_options': {'initial_workers':1,'max_workers':1}}
    preview = run_material_pipeline(config)
    assert preview['selected_ids'] == ['fixture-paper'] and preview['excluded_checkpoint_ids'] == ['unselected']
    assert not (tmp_path/'pipeline').exists()
    client = CardModelBoundary()
    config['mode'] = 'replay'
    result = run_material_pipeline(config, client_factory=lambda *args: client)
    assert result['planner_validated_rows'] == 1 and result['network_calls'] == 0
    again = run_material_pipeline(config, client_factory=lambda *args: client)
    assert len(client.calls) == 1 and again['planning_pool']['pool_sha256'] == result['planning_pool']['pool_sha256']


def test_source_index_reader_assessment_and_skeleton_sufficiency(tmp_path):
    config, item = service_fixture(tmp_path)
    config.update(coverage_plan_path=item['plan_path'], facet_outputs={'F1':['response']})
    unit = source_unit_from_snapshot(snapshot_dir=item['snapshot_dir'], roles=['model_mechanism'])
    reader = Reader(unit)
    service = create_evidence_service(config, reader_client=reader)
    preview = service.preview(request())
    assert preview['network_calls'] == 0 and not reader.calls
    bundle = service.request(request())
    assert bundle.gap_fulfilled and bundle.source_units[0]['output_assessment']['scientific_truth_validated'] is False
    assert len(reader.calls) == 1
    assert service.request(request()).gap_fulfilled and len(reader.calls) == 1
    _, receipt = load_plan(item['plan_path'])
    proof = {'path': service.last_receipt_path, 'sha256': digest(service.last_receipt_path)}
    readings = _verified_pool_reading(config['planning_pool_path'], {}, evidence_receipt=proof,
                                     expected_plan_hash=receipt['plan_sha256'])
    assert readings['fixture-paper']['evidence_sufficient_by_facet']['F1'] is True
    with pytest.raises(ValueError, match='plan_mismatch'):
        _verified_pool_reading(config['planning_pool_path'], {}, evidence_receipt=proof, expected_plan_hash='other')
    saved = Path(service.last_receipt_path); saved.write_text(saved.read_text()+' ')
    with pytest.raises(ValueError, match='hash_mismatch'):
        _verified_pool_reading(config['planning_pool_path'], {}, evidence_receipt=proof, expected_plan_hash=receipt['plan_sha256'])


def test_assessment_rejects_invented_quote_and_keeps_conditions_unresolved(tmp_path):
    inputs = card_inputs(tmp_path)
    unit = source_unit_from_snapshot(snapshot_dir=inputs['snapshot_dir'], roles=['model_mechanism'])
    anchor = unit['anchors'][0]
    row = {'output_id':'O01','status':'fulfilled','answer':'value','reason':'source','applicability':'direct',
           'conditions':{},'source_refs':[{'source_block_id':anchor['source_block_id'],'quote':anchor['quote']}]}
    conditional = replace(request(),object_conditions={'temperature':'10 K'})
    assert not apply_output_assessments(conditional,unit,[row])['fulfilled_outputs']
    row['source_refs'][0]['quote'] = 'Invented numerical coefficient 42.'
    with pytest.raises(ValueError,match='quote_not_in_source'):
        apply_output_assessments(request(),unit,[row])


def test_actual_service_wires_optional_repair_with_real_sources(tmp_path):
    config,item = service_fixture(tmp_path, reader_enabled=False)
    # Query intentionally absent from local index; provider retrieves registered original material.
    req = replace(request(),question='unindexed_unique_requirement')
    records = tmp_path/'source_records.json'
    records.write_text(json.dumps({'records':[{'canonical_paper_id':'fixture-paper',
        'snapshot_dir':item['snapshot_dir'], 'provider_identifiers':{'s2':'provider-fixture'}}]}))
    config['repair'] = {'plan_path':item['plan_path'],'facet_outputs':{'F1':['response']},
                        'source_records_path':str(records),'budget':{'max_queries':2,'max_reads':1}}
    class Gateway:
        def __init__(self): self.calls=[]
        def search_papers(self, query, **kwargs):
            self.calls.append(query)
            return [SimpleNamespace(paper_id='provider-fixture',corpus_id=1,doi='',title='Synthetic device response',
                abstract='',year=2025,venue='',authors=[])],SimpleNamespace(ok=True,status_code=200)
        def search_snippets(self, query, **kwargs):
            self.calls.append(query); return [],SimpleNamespace(ok=True,status_code=200)
    gateway = Gateway()
    service = create_evidence_service(config,gateway=gateway)
    bundle = service.request(req)
    assert bundle.material_ready and not bundle.gap_fulfilled and gateway.calls
    receipt = json.loads(Path(service.last_receipt_path).read_text())
    assert receipt['repair_state']['reads'] == 1 and receipt['repair_state']['query_history']
    count = len(gateway.calls)
    service.request(req)
    assert len(gateway.calls) == count


def test_service_preview_and_changed_inputs_fail_before_calls(tmp_path):
    config,item = service_fixture(tmp_path,mode='preview')
    service = create_evidence_service(config)
    assert service.preview(request())['adapters']['reader'] == 'run_directed_reading'
    assert not Path(config['output_dir']).exists()
    with pytest.raises(ValueError,match='explicit_preview_only'): service.request(request())
    config['mode']='run'
    with pytest.raises(ValueError,match='injected_transports'): create_evidence_service(config,gateway=object())


def test_formal_material_cli_consumes_request_bound_card_replay(tmp_path, capsys):
    from scripts.upgrade3.review_material_pipeline import main
    inputs = card_inputs(tmp_path)
    nav = navigation(tmp_path, inputs['plan_path'])
    checkpoint = tmp_path/'CHECKPOINT.json'
    checkpoint.write_text(json.dumps({'items':[{'paper_id':'fixture-paper','snapshot_path':str(inputs['snapshot_dir'])}]}))
    config = {'navigation_manifest_path':str(nav),'plan_path':str(inputs['plan_path']),
        'checkpoint_path':str(checkpoint),'output_dir':str(tmp_path/'capture'),'mode':'replay',
        'batch_options':{'initial_workers':1,'max_workers':1}}
    client=CardModelBoundary()
    run_material_pipeline(config,client_factory=lambda *args:client)
    responses={}
    for messages,kwargs in client.calls:
        binding=model_request_hash(messages,kwargs)
        responses[binding]={'request_sha256':binding,'response':CardModelBoundary().complete(messages,**kwargs)}
    replay=tmp_path/'replay.json'; replay.write_text(json.dumps({'schema_version':'review.model_replay.v1','responses':responses}))
    config.update(output_dir=str(tmp_path/'cli'),card_replay_path=str(replay))
    path=tmp_path/'config.json'; path.write_text(json.dumps(config))
    assert main(['--config',str(path)]) == 0
    assert not (tmp_path/'cli').exists()
    assert main(['--config',str(path),'--replay']) == 0
    report=json.loads((tmp_path/'cli'/'MATERIAL_PIPELINE_REPORT.json').read_text())
    assert report['planner_validated_rows'] == 1 and report['network_calls'] == 0


def test_nonfinalized_wait_then_complete_and_actual_local_xml_acquisition(tmp_path):
    inputs=card_inputs(tmp_path)
    nav=navigation(tmp_path,inputs['plan_path'])
    xml=tmp_path/'source.xml'
    xml.write_text('<article><front><article-meta><title-group><article-title>Synthetic device response</article-title></title-group></article-meta></front><body><sec><title>Results</title><p>The synthetic device response doubles at the measured temperature.</p></sec></body></article>')
    config={'navigation_manifest_path':str(nav),'plan_path':str(inputs['plan_path']),
        'output_dir':str(tmp_path/'pipeline'),'mode':'replay','finalized':False,
        'source_records':[{'canonical_paper_id':'fixture-paper','local_xml':str(xml)}],
        'batch_options':{'initial_workers':1,'max_workers':1}}
    client=CardModelBoundary()
    waiting=run_material_pipeline(config,client_factory=lambda *args:client)
    assert waiting['status']=='waiting_material_manifest_not_finalized' and not client.calls
    config['finalized']=True
    result=run_material_pipeline(config,client_factory=lambda *args:client)
    assert result['planner_validated_rows']==1 and result['network_calls']==0


def test_formal_evidence_cli_replays_actual_directed_request(tmp_path, capsys):
    from scripts.upgrade3.article_evidence import main
    config,item=service_fixture(tmp_path)
    unit=source_unit_from_snapshot(snapshot_dir=item['snapshot_dir'],roles=['model_mechanism'])
    reader=Reader(unit)
    assert create_evidence_service(config,reader_client=reader).request(request()).gap_fulfilled
    responses={}
    for messages,kwargs in reader.calls:
        binding=model_request_hash(messages,kwargs)
        responses[binding]={'request_sha256':binding,'response':Reader(unit).complete(messages,**kwargs)}
    replay=tmp_path/'reader_replay.json'; replay.write_text(json.dumps({'schema_version':'review.model_replay.v1','responses':responses}))
    config.update(output_dir=str(tmp_path/'cli_service'),reader_replay_path=str(replay))
    file=tmp_path/'service_config.json';file.write_text(json.dumps(config))
    req=tmp_path/'request.json';req.write_text(json.dumps(request().to_dict()))
    assert main(['--config',str(file),'--request',str(req)])==0
    assert not Path(config['output_dir']).exists()
    assert main(['--config',str(file),'--request',str(req),'--replay'])==0
    result=json.loads(next(Path(config['output_dir']).glob('EVIDENCE_*.json')).read_text())
    assert result['evidence_bundle']['gap_fulfilled'] and result['network_calls']==0


def test_granular_crossdomain_requirements_and_frozen_benchmark(tmp_path):
    from optomind_research.runtime.upgrade3.coverage_evaluation import evaluate_manifest
    inputs=card_inputs(tmp_path)
    cases=[]
    for domain,question,clauses in [
        ('optics','Compare cavity and interferometer sensitivity after 2020 excluding biological samples.',
         ['cavity','interferometer','sensitivity','after 2020','excluding biological samples']),
        ('agriculture','Compare irrigation and rainfed wheat yield under drought using field experiments.',
         ['irrigation','rainfed','wheat yield','under drought','field experiments']),
        ('batteries','Compare cycle life of sodium and lithium cells below 0 C excluding simulations.',
         ['cycle life','sodium','lithium','below 0 C','excluding simulations']),
        ('education','Compare tutoring and classroom instruction for adult literacy using randomized trials.',
         ['tutoring','classroom instruction','adult literacy','randomized trials'])]:
        plan=json.loads(Path(inputs['plan_path']).read_text())
        plan['original_question']=question
        plan['requirements']=[{'id':f'R{i}','source_text':clause} for i,clause in enumerate(clauses,1)]
        plan['facets'][0]['requirement_mapping']=[row['id'] for row in plan['requirements']]
        loaded,receipt=load_plan(plan,expected_question=question)
        assert [r['source_text'] for r in loaded['requirements']]==clauses
        assert receipt['semantic_coverage']=='not_assessed'
        ident='synthetic-'+domain
        annotation={'case_id':ident,'annotation_id':ident+'-labels-v1','annotator':'synthetic-software-fixture-author',
            'requirements':clauses,'facets':['F1','F2'],'papers':{
                'p1':{'facets':['F1'],'method_candidate':False,'counter_evidence':False},
                'p2':{'facets':['F2'],'method_candidate':True,'counter_evidence':True}}}
        baseline={'case_id':ident,'understood_requirements':clauses[:2],
            'papers':{'p1':{'label':'directly_relevant','read_confirmed':False},'p2':{'label':'clearly_irrelevant'}}}
        repaired={'case_id':ident,'understood_requirements':clauses,'papers':{
            'p1':{'label':'directly_relevant','read_confirmed':True,'source_validated':True,'fulfilled_facets':['F1']},
            'p2':{'label':'background_or_method','read_confirmed':True,'source_validated':True,'fulfilled_facets':['F2']}},
            'cost_receipts':[{'round':1,'actual_cny':0,'confirmed_facets':['F1']},{'round':2,'actual_cny':0,'confirmed_facets':['F1','F2']}]}
        row={'case_id':ident,'domain':domain,'evidence_kind':'synthetic_software_fixture'}
        for name,payload in [('baseline',baseline),('repaired',repaired),('annotation',annotation)]:
            file=tmp_path/(ident+'-'+name+'.json');file.write_text(json.dumps(payload))
            row[name]={'path':file.name,'sha256':digest(file)}
        cases.append(row)
    manifest=tmp_path/'benchmark.json';manifest.write_text(json.dumps({'schema_version':'review.coverage_benchmark.v1','cases':cases}))
    report=evaluate_manifest(manifest)
    assert len(report['domains'])==4 and not report['scientific_quality_certified']
    assert all(row['repaired']['facet_evidence']['F2']['coverage']==1 for row in report['cases'])
    target=tmp_path/cases[0]['baseline']['path'];target.write_text(target.read_text()+' ')
    with pytest.raises(ValueError,match='hash_mismatch'):evaluate_manifest(manifest)


def test_zero_selected_material_reports_insufficiency_and_empty_service_repairs(tmp_path):
    inputs=card_inputs(tmp_path)
    nav=navigation(tmp_path,inputs['plan_path'])
    result=run_material_pipeline({'navigation_manifest_path':str(nav),'plan_path':str(inputs['plan_path']),
        'output_dir':str(tmp_path/'no_material'),'mode':'replay'},client_factory=lambda *args:CardModelBoundary())
    assert result['status']=='insufficient_material' and result['planner_validated_rows']==0
    pool=tmp_path/'empty.jsonl';pool.write_text('')
    config={'mode':'replay','planning_pool_path':str(pool),'output_dir':str(tmp_path/'empty_service'),'reader_enabled':False}
    service=create_evidence_service(config)
    bundle=service.request(request())
    assert bundle.status=='unresolved' and not bundle.source_units
    assert service.request(request()).status=='unresolved'


def test_saved_replay_budget_change_never_silently_reuses_response(tmp_path):
    from optomind_research.runtime.upgrade3.offline_replay import RecordedClient
    messages=[{'role':'user','content':'fixture'}]
    kwargs={'model':'fixture-model','max_output_tokens':100}
    binding=model_request_hash(messages,kwargs)
    file=tmp_path/'replay.json';file.write_text(json.dumps({'schema_version':'review.model_replay.v1',
        'responses':{binding:{'request_sha256':binding,'response':{'content':'fixture'}}}}))
    client=RecordedClient(file)
    assert client.complete(messages,**kwargs)['content']=='fixture'
    with pytest.raises(ValueError,match='request_missing'):
        client.complete(messages,model='fixture-model',max_output_tokens=101)


def test_actual_service_model_query_revision_uses_gap_feedback_replay(tmp_path):
    from optomind_research.runtime.upgrade3.evidence_query_revision import revision_messages
    from optomind_research.runtime.upgrade3.candidate_corpus import plan_queries
    from optomind_research.runtime.upgrade3.query_plan import provider_feedback
    config,item=service_fixture(tmp_path,reader_enabled=False)
    req=replace(request(),question='unindexed_unique_requirement')
    plan,_=load_plan(item['plan_path'])
    specs=plan_queries(plan)
    feedback=provider_feedback([{'query':s.query_text,'facet':s.facet_id,'provider_total':0} for s in specs])
    messages=revision_messages(plan=plan,unmet_facets=('F1',),gaps={'F1':'zero_results'},feedback=feedback)
    kwargs={'model':'qwen3.7-flash','max_output_tokens':2048,'thinking':True,'thinking_budget':1024}
    binding=model_request_hash(messages,kwargs)
    replay=tmp_path/'revision_replay.json'
    replay.write_text(json.dumps({'schema_version':'review.model_replay.v1','responses':{binding:{
        'request_sha256':binding,'response':{'content':json.dumps({'revisions':[{'parent_query_id':specs[0].query_id,
            'query_text':'synthetic device temperature mechanism'}]})}}}}))
    config['repair']={'plan_path':item['plan_path'],'facet_outputs':{'F1':['response']},
        'budget':{'max_queries':4,'max_rounds':2},'revision_options':{},'revision_replay_path':str(replay)}
    class Gateway:
        def __init__(self):self.calls=[]
        def search_papers(self,query,**kwargs):self.calls.append(query);return [],SimpleNamespace(ok=True,status_code=200)
        def search_snippets(self,query,**kwargs):self.calls.append(query);return [],SimpleNamespace(ok=True,status_code=200)
    gateway=Gateway();service=create_evidence_service(config,gateway=gateway)
    bundle=service.request(req)
    assert not bundle.gap_fulfilled and 'synthetic device temperature mechanism' in gateway.calls
    receipt=json.loads(Path(service.last_receipt_path).read_text())
    assert receipt['revision_messages']==messages
    assert receipt['repair_state']['revisions']==1
    lineage=[r for r in receipt['repair_state']['receipts'] if r['operation']=='revision'][0]['lineage']
    assert lineage[0]['parent_query_id']==specs[0].query_id
    count=len(gateway.calls);service.request(req)
    assert len(gateway.calls)==count


def test_zero_request_budget_never_constructs_live_transports(tmp_path,monkeypatch):
    from optomind_research.runtime.upgrade3 import module4
    from optomind_research.runtime.upgrade3.module4 import runtime
    import optomind_research.s2_intelligence_gateway as s2
    config,item=service_fixture(tmp_path,mode='run')
    config['repair']={'plan_path':item['plan_path'],'facet_outputs':{'F1':['response']},'revision_options':{}}
    def forbidden(*args,**kwargs):raise AssertionError('live transport constructed')
    monkeypatch.setattr(s2,'S2IntelligenceGateway',forbidden)
    monkeypatch.setattr(runtime,'QwenDirectClient',forbidden)
    req=replace(request(),budget={'max_papers':0,'max_supplementary_calls':1})
    result=create_evidence_service(config).request(req)
    assert result.status=='unresolved' and not result.source_units


def test_query_revision_shared_ledger_stops_before_client_on_low_budget(tmp_path,monkeypatch):
    from optomind_research.runtime.upgrade3.evidence_query_revision import EvidenceQueryReviser
    from optomind_research.runtime.upgrade3.module4 import runtime
    from optomind_research.runtime.upgrade3.directed_reading import DirectedReadingError
    inputs=card_inputs(tmp_path);plan,_=load_plan(inputs['plan_path'])
    ledger=runtime.GlobalBudgetLedger(limit_cny=1e-9,path=tmp_path/'ledger.sqlite')
    def forbidden(*args,**kwargs):raise AssertionError('live client constructed before budget admission')
    monkeypatch.setattr(runtime,'QwenDirectClient',forbidden)
    reviser=EvidenceQueryReviser(mode='run',options={},output_dir=tmp_path/'revision',ledger=ledger)
    with pytest.raises(DirectedReadingError,match='preflight_insufficient'):
        reviser(plan=plan,unmet_facets=('F1',),gaps={'F1':'zero_results'},feedback=[])
    assert not ledger.as_dict()['reservations']


def test_live_local_reader_budget_stop_retains_source_without_model(tmp_path,monkeypatch):
    from optomind_research.runtime.upgrade3 import directed_reading
    config,item=service_fixture(tmp_path,mode='run')
    key=tmp_path/'synthetic-unused-key.txt';key.write_text('offline-test-placeholder')
    config['reader_options']={'key_file':str(key),'budget_ledger_path':str(tmp_path/'ledger.sqlite'),'budget_limit_cny':1e-9}
    def forbidden(*args,**kwargs):raise AssertionError('client created despite denied budget')
    monkeypatch.setattr(directed_reading,'QwenDirectClient',forbidden)
    bundle=create_evidence_service(config).request(request())
    assert bundle.material_ready and not bundle.gap_fulfilled
    assert any(row.get('reason')=='monetary_budget_reached' for row in bundle.diagnostics)


def test_repair_budget_denial_is_known_not_uncertain_and_keeps_source(tmp_path):
    from optomind_research.runtime.upgrade3.coverage_repair import CoverageRepairRunner
    from optomind_research.runtime.upgrade3.directed_reading import DirectedReadingError
    inputs=card_inputs(tmp_path)
    plan,_=load_plan(inputs['plan_path'])
    original=source_unit_from_snapshot(snapshot_dir=inputs['snapshot_dir'],roles=['model_mechanism'])
    class Gateway:
        def search_papers(self,query,**kwargs):return [SimpleNamespace(paper_id='fixture-paper',corpus_id=1,doi='',title='Source',abstract='',year=2025,venue='',authors=[])],SimpleNamespace(ok=True,status_code=200)
        def search_snippets(self,query,**kwargs):return [],SimpleNamespace(ok=True,status_code=200)
    def denied(**kwargs):raise DirectedReadingError('global_budget_preflight_insufficient')
    runner=CoverageRepairRunner(plan=plan,facet_outputs={'F1':['response']},gateway=Gateway(),
        source_loader=lambda **kwargs: original,reader=denied,state_path=tmp_path/'repair.json')
    units=runner(request=request(),unmet_outputs=('response',))
    assert units and runner.state['stop_reason']=='monetary_budget_reached'
    assert all(row.get('status')!='started' for row in runner.state['receipts'])
    resumed=CoverageRepairRunner(plan=plan,facet_outputs={'F1':['response']},gateway=Gateway(),
        source_loader=lambda **kwargs: original,reader=denied,state_path=tmp_path/'repair.json')
    assert resumed.state['stop_reason']=='monetary_budget_reached'


def test_existing_foreign_index_rejected_before_mutation(tmp_path):
    from optomind_research.runtime.upgrade3.planning_material_search import PlanningMaterialIndex
    config,item=service_fixture(tmp_path,reader_enabled=False)
    path=tmp_path/'foreign.sqlite';index=PlanningMaterialIndex(path);index.close()
    before=path.read_bytes();config['index_path']=str(path)
    with pytest.raises(ValueError,match='explicit_service_binding'):
        create_evidence_service(config).request(request())
    assert path.read_bytes()==before


def test_navigation_checkpoint_canonical_alias_and_ambiguous_versions(tmp_path):
    inputs=card_inputs(tmp_path);nav=navigation(tmp_path,inputs['plan_path'])
    item={'paper_id':'old-P0001','canonical_paper_id':'fixture-paper','snapshot_path':str(inputs['snapshot_dir'])}
    result=reading_manifest_from_navigation(navigation_manifest_path=nav,checkpoint={'items':[item]},plan_path=inputs['plan_path'])
    assert result['records'][0]['snapshot_dir']==str(inputs['snapshot_dir'])
    assert result['records'][0]['paper_id']=='fixture-paper' and not result['excluded_checkpoint_ids']
    with pytest.raises(ValueError,match='ambiguous_acquisition'):
        reading_manifest_from_navigation(navigation_manifest_path=nav,
            checkpoint={'items':[item,{**item,'snapshot_path':'other-version'}]},plan_path=inputs['plan_path'])


def test_authentic_585_pool_projection_identity_depth_and_no_overwrite(tmp_path):
    from optomind_research.runtime.upgrade3.progressive_review_plan import load_planning_pool
    archive=Path(__file__).resolve().parents[2]/'docs/acceptance/body40-20261005/planning/input585/INPUT_POOL.jsonl'
    original=archive.read_bytes()
    old=[json.loads(line) for line in original.decode().splitlines() if line.strip()]
    assert len(old)==585 and all(row['planning_view']['paper_identity']['canonical_paper_id'] for row in old)
    assert len(load_planning_pool(archive))==585
    state,item=batch_fixture(tmp_path)
    published=publish_planning_pool(batch_state_path=state,output_dir=tmp_path/'merged',legacy_pool_path=archive)
    merged=[json.loads(line) for line in Path(published['pool_path']).read_text().splitlines()]
    assert merged[:585]==old and len(merged)==586
    assert len(load_planning_pool(published['pool_path']))==586
    assert archive.read_bytes()==original
    assert all(row['canonical_paper_id']==old[index]['planning_view']['paper_identity']['canonical_paper_id']
               and row['paper_id']==old[index]['paper_id'] for index,row in enumerate(published['legacy_mapping']))
