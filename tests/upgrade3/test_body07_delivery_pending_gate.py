"""Synthetic short batch: actual history assembly must gate downstream editing."""
import json
import socket
from pathlib import Path
import pytest
from optomind_research.runtime.upgrade3 import review_delivery as d


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def batch(root, problem=''):
    body = 'Synthetic measured response at 20 K; no extrapolation [P0001].'
    arrangement = put(root / 'arrangement.json', {
        'chapter_id':'CH01', 'title':'Physical response',
        'units':[{'unit_id':'U1', 'focus':'Response', 'paragraph_tasks':[]}],
        'source_catalog':{'P0001':{'paper_id':'synthetic-one', 'title':'Synthetic response', 'year':'2024'}}})
    result = {'chapter_id':'CH01', 'unit_id':'U1', 'complete':True,
              'finish_reason':'stop', 'body_markdown':body, 'issues':[]}
    if problem == 'table':
        result.update(output_consumption_status='pending_table', issues=[{'code':'markdown_table_missing_or_invalid'}])
    elif problem == 'length':
        result.update(complete=False, finish_reason='length')
    result_path = put(root/'unit'/'UNIT_RESULT.json', result)
    manifest = put(root/'manifest.json', {'chapters':[{'chapter_id':'CH01', 'arrangement_path':str(arrangement)}]})
    put(root/'BATCH_JOBS.json',[{'chapter_id':'CH01','unit_id':'U1','arrangement':str(arrangement),'output':str(result_path.parent)}])
    edit = put(root/'edit.json', {'fixture':True, 'changes':[], 'unresolved_questions':[]})
    config = put(root/'config.json', {'schema':d.DELIVERY_CONFIG_SCHEMA,'text_edit':{'fixture':str(edit)}})
    return manifest, config, result_path, body


@pytest.fixture(autouse=True)
def network_off(monkeypatch):
    def deny(*a, **kw): raise AssertionError('No network in WO07 offline checks')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)


@pytest.mark.parametrize('problem',['table','length'])
def test_actual_assembly_pending_stops_before_editing(tmp_path, problem):
    manifest, config, original, body = batch(tmp_path, problem)
    original_bytes = original.read_bytes()
    report = d.run_review_delivery(start='history',out_dir=tmp_path/'delivery',manifest_path=manifest,batch_root=tmp_path,config_path=config)
    assert report['assembly']['status'] == 'complete'  # all units loaded, not content approval
    assert report['assembly']['problems_resolved'] is False
    assert report['downstream_status'] == 'pending'
    assert report['halt_reasons'] == ['assembly_pending']
    assert report['stages'] == {}
    assert not (tmp_path/'delivery'/'02_text_edit').exists()
    assert body in (tmp_path/'delivery'/'assembled'/'REVIEW_DRAFT_HANDLES.md').read_text()
    assert original.read_bytes() == original_bytes
    assert json.loads((tmp_path/'delivery'/'DELIVERY_REPORT.json').read_text())['halt_reasons'] == ['assembly_pending']


def test_normal_complete_batch_still_reaches_existing_editor(tmp_path):
    manifest, config, original, body = batch(tmp_path)
    report = d.run_review_delivery(start='history',out_dir=tmp_path/'delivery',manifest_path=manifest,batch_root=tmp_path,config_path=config)
    assert report['assembly']['problems_resolved'] is True
    assert report['stages']['02_text_edit']['status'] == 'no_change'
    assert report['halt_reasons'] == ['03_front_back']  # no fixture: no parts or publication attempted


@pytest.mark.parametrize('extra', [
    {'pending':[{'step':'writer:U2','status':'pending_missing_recording'}]},
    {'missing_units':['U2']},
    {'restricted_import':True},
    {'assembly':{'status':'partial_check','problems_resolved':True}},
    {'assembly':{'status':'complete','problems_resolved':True,'pending_problems':[{'code':'unresolved'}]}},
    {'assembly':{}},
])
def test_pending_report_does_not_use_stale_draft(tmp_path, extra):
    (tmp_path/'REVIEW_DRAFT_HANDLES.md').write_text('Existing valid body.\n')
    report = {'output_root':str(tmp_path),'assembly':{'status':'complete','problems_resolved':True},**extra}
    result = d.run_downstream_delivery(config={},assembly_report=report,out_dir=tmp_path/'downstream')
    assert result['halt_reasons'] == ['assembly_pending']
    assert result['stages'] == {}
    assert (tmp_path/'REVIEW_DRAFT_HANDLES.md').read_text() == 'Existing valid body.\n'


def test_identity_only_gap_still_uses_existing_catalog_resolution_path(tmp_path):
    manifest, config, _, _ = batch(tmp_path)
    arrangement_path = tmp_path/'arrangement.json'
    arrangement = json.loads(arrangement_path.read_text())
    arrangement['source_catalog'] = {}
    put(arrangement_path, arrangement)
    report = d.run_review_delivery(start='history',out_dir=tmp_path/'delivery',manifest_path=manifest,batch_root=tmp_path,config_path=config)
    assert report['assembly']['unknown_citations'] == ['P0001']
    assert report['assembly']['problems_resolved'] is False
    assert report['assembly']['pending_problems'] == []
    assert report['stages']['02_text_edit']['status'] == 'no_change'
    assert report['halt_reasons'] == ['03_front_back']


@pytest.mark.parametrize('assembly',[
    {'status':'complete','problems_resolved':False},
    {'status':'complete','problems_resolved':False,'unknown_citations':['P0001'],
     'pending_problems':[{'code':'writer_issues:1'}]},
    {'status':'complete','problems_resolved':False,'unknown_table_handles':['P0001'],
     'pending_problems':[{'code':'unmapped_table_handle:P0001'}]},
])
def test_identity_exception_never_hides_task_or_bare_table_problem(tmp_path,assembly):
    (tmp_path/'REVIEW_DRAFT_HANDLES.md').write_text('Existing body.')
    result = d.run_downstream_delivery(config={},assembly_report={'output_root':str(tmp_path),'assembly':assembly},out_dir=tmp_path/'downstream')
    assert result['halt_reasons'] == ['assembly_pending']
    assert result['stages'] == {}
