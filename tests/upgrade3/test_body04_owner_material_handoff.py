"""WO04 offline content handoff controls: no provider/network calls."""
import copy
import json
import pytest
from optomind_research.runtime.upgrade3 import progressive_review_plan as p


def plan(*handles):
    return {'units': [{'unit_id': 'U1', 'source_handles': list(handles)}]}


def source(handle='P0001'):
    return {'source_handle': handle, 'paper_id': 'paper-' + handle, 'title': 'Study ' + handle,
            'study_summary_A': {'finding': 'Observed classroom score gain under specified conditions'}}


def candidate():
    return {'source_handle': 'P0002', 'paper_id': 'paper-P0002', 'title': 'Original study',
            'doi': '10.1234/original', 'supplement_material': {
                'usable_content': 'SOURCE_BOUND_RESULT: 200 classrooms, score gain four points',
                'conditions': ['random assignment'], 'reporting_review': 'review-paper'}}


def test_identity_only_and_nested_missing_are_unavailable():
    identity = {k: v for k, v in source().items() if k != 'study_summary_A'}
    status, _, _, errors = p._classify_owner_response(plan('P0001'),
        {'updated_plan': plan('P0001')}, [identity])
    assert status == 'unresolved' and errors
    nested = plan('P0001')
    nested['units'][0]['paragraph_briefs'] = [{'source_handles': ['P0999']}]
    status, _, _, errors = p._classify_owner_response(plan('P0001'), {'updated_plan': nested}, [source()])
    assert status == 'unresolved' and any('P0999' in e for e in errors)


@pytest.mark.parametrize('channel', ['candidate_materials', 'candidate_navigation', 'pool_supplement', 'deep_read', 'tool_materials'])
def test_selected_source_uses_actual_material_without_own_ab(channel):
    row = candidate()
    args = {}
    if channel == 'candidate_materials': args[channel] = [row]
    elif channel == 'candidate_navigation': args[channel] = {'candidate_materials': [row]}
    elif channel == 'tool_materials': args[channel] = [{'usable_content': row['supplement_material']['usable_content'], 'sources': [{k: row[k] for k in ('source_handle','paper_id','title','doi')}]}]
    else:
        pool = {'_source_handle': row['source_handle'], '_paper_id': row['paper_id'], 'title': row['title'], 'doi': row['doi']}
        if channel == 'pool_supplement': pool['supplement_gap_material'] = row['supplement_material']
        else: args['deep_material_by_paper'] = {row['paper_id']: {'question_material': [{'finding': row['supplement_material']['usable_content']}]}}
        args['pool_rows'] = [pool]
    rows, report = p._resolve_owner_source_materials(source_materials=[source()], chapter_plan=plan('P0001','P0002'), **args)
    assert 'SOURCE_BOUND_RESULT' in json.dumps(rows)
    assert any(r['source_handle'] == 'P0002' for r in rows)
    assert not report['unresolved']


def test_conflicting_handle_never_splices_old_identity_to_new_content(tmp_path):
    card = tmp_path/'card.json';card.write_text(json.dumps({'general_understanding': {'finding':'NEW_ID_RESULT'}}))
    rows, report = p._resolve_owner_source_materials(source_materials=[source()], chapter_plan=plan('P0001'),
        pool_rows=[{'_source_handle':'P0001','_paper_id':'new-paper','title':'New','card_path':str(card)}])
    assert not any(r.get('paper_id') == 'paper-P0001' and 'NEW_ID_RESULT' in json.dumps(r) for r in rows)
    assert any('identity' in item['reason'] for item in report['unresolved'])


def test_owner_response_adopts_supplied_candidate_and_returns_material(tmp_path):
    calls = []
    def owner(stage, payload):
        calls.append(copy.deepcopy(payload))
        return {'status':'updated','updated_plan':plan('P0001','P0002')}
    config = p.ProgressivePlannerConfig(topic_id='offline',pool_path=tmp_path/'pool',plan_path=tmp_path/'plan',output_dir=tmp_path)
    result = p.ProgressiveReviewPlanner(config,planner=owner).revise_from_arrangement_issues(
        chapter={'chapter_id':'CH01'},chapter_plan=plan('P0001'),source_materials=[source()],candidate_materials=[candidate()],
        arrangement={'issues':[{'action':'chapter_owner','problem':'Use supplied comparison when relevant'}]})
    assert result['status'] == 'updated', result
    assert 'SOURCE_BOUND_RESULT' in json.dumps(result['rebuild_arrangement_input']['source_materials'])
    assert calls[0]['candidate_materials'] == [candidate()]


def test_metadata_and_proposed_use_are_not_study_material():
    row = {'source_handle':'P0002','paper_id':'paper-P0002','title':'Known identity',
           'supplement_material':{'status':'complete','question':'What happened?','intended_use':'Compare results','sources':[{'title':'Original title'}]}}
    rows, report = p._resolve_owner_source_materials(source_materials=[source()],chapter_plan=plan('P0001','P0002'),candidate_materials=[row])
    status,_,_,errors=p._classify_owner_response(plan('P0001'),{'updated_plan':plan('P0001','P0002')},rows)
    assert status == 'unresolved' and errors


def test_feedback_loop_persists_adopted_content_and_identity(tmp_path):
    packet={'chapter':{'chapter_id':'CH01'}, 'chapter_plan':plan('P0001'),
            'source_materials':[source()], 'candidate_materials':[candidate()]}
    packet_path=tmp_path/'input.json';packet_path.write_text(json.dumps(packet))
    arrangement={'issues':[{'action':'chapter_owner','problem':'Use the supplied classroom comparison'}]}
    arrangement_path=tmp_path/'arrangement.json';arrangement_path.write_text(json.dumps(arrangement))
    def owner(stage,payload):
        (tmp_path/'OWNER_MESSAGES.json').write_text(json.dumps(p._messages_for(stage,payload),indent=2))
        return {'status':'updated','updated_plan':plan('P0001','P0002')}
    def arrange(packet,prior,path):
        assert 'SOURCE_BOUND_RESULT' in json.dumps(packet['source_materials'])
        return {'units':[{'unit_id':'U1','source_handles':['P0001','P0002']}]}
    def write(packet,arrangement,path):
        (tmp_path/'WRITER_INPUT.json').write_text(json.dumps(packet,indent=2))
        assert packet['source_identity_map']['P0002']['doi']=='10.1234/original'
        return {'body_markdown':'Synthetic offline body', 'complete':True}
    result=p.run_feedback_loop(packet_path=packet_path,arrangement_path=arrangement_path,
        arrangement=arrangement,owner_planner=owner,arrangement_runner=arrange,writer_runner=write,output_dir=tmp_path/'run')
    assert result['status'] not in {'partial','unresolved'}
    persisted=json.loads(__import__('pathlib').Path(result['updated_packet']).read_text())
    assert 'SOURCE_BOUND_RESULT' in json.dumps(persisted['source_materials'])
    assert persisted['source_identity_map']['P0002']['paper_id']=='paper-P0002'
    again=p.run_feedback_loop(packet_path=packet_path,arrangement_path=arrangement_path,arrangement=arrangement,
        owner_planner=owner,arrangement_runner=arrange,writer_runner=write,output_dir=tmp_path/'run')
    assert again['status']=='reused'


def test_invalid_owner_does_not_replace_existing_packet(tmp_path):
    result=p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(topic_id='offline',pool_path=tmp_path/'pool',plan_path=tmp_path/'plan',output_dir=tmp_path),
        planner=lambda *_:{'updated_plan':plan('P0001','P0999')}).revise_from_arrangement_issues(
            chapter={'chapter_id':'CH01'},chapter_plan=plan('P0001'),source_materials=[source()],
            arrangement={'issues':[{'problem':'Consider supplied material'}]})
    assert result['updated_plan'] is None
    assert result['rebuild_arrangement_input']['chapter_plan']==plan('P0001')
    assert result['rebuild_arrangement_input']['source_materials']==[source()]


def test_owner_closure_does_not_read_unselected_pool(monkeypatch):
    def unexpected(*_args,**_kwargs): pytest.fail('unselected card was read')
    monkeypatch.setattr(p,'build_local_material_payload',unexpected)
    rows,_=p._resolve_owner_source_materials(source_materials=[source()],chapter_plan=plan('P0001'),
        pool_rows=[{'_source_handle':'P0002','_paper_id':'two','card_path':'unselected'}])
    assert rows==[source()]


@pytest.mark.parametrize('payload',[{'status':'complete','sources':[{'title':'Name','doi':'10.1/name'}]},
    {'question_material':[{'question_id':'Q1','examples':[],'explanation':'','remaining_points':['Unknown']}]},
    {'examples':[{'attribution':'Doe 2020','use_in_review':'make a comparison','reference_ids':['R1']}]}])
def test_empty_answers_and_nested_bibliography_are_not_substance(payload):
    assert not p._owner_material_has_content({'source_handle':'P0002','paper_id':'two','deep_read_material':payload})


@pytest.mark.parametrize('identity',[{'canonical_paper_id':'different-paper'}, {'paper_id':'paper-P0001','canonical_paper_id':'different-paper'}])
def test_canonical_identity_conflicts_never_splice_material(identity):
    incoming={**candidate(), 'source_handle':'P0001', **identity}
    if 'paper_id' not in identity: incoming.pop('paper_id',None)
    rows,report=p._resolve_owner_source_materials(source_materials=[source()],chapter_plan=plan('P0001'),candidate_materials=[incoming])
    assert any('identity' in item['reason'] for item in report['unresolved'])
    assert not any('SOURCE_BOUND_RESULT' in json.dumps(row) and row.get('paper_id')=='paper-P0001' for row in rows)


def test_unit_scoped_tool_is_not_folded_into_shared_source():
    packet={'chapter':{'chapter_id':'CH01'},'source_materials':[source()]}
    tool={'unit_key':'CH01:U2','chapter_ids':['CH01'],'usable_content':'Only U2 requested this comparison',
          'sources':[{k:v for k,v in source().items() if k!='study_summary_A'}]}
    merged=p.merge_tool_materials_into_packets([packet],[tool])[0]
    assert not merged['source_materials'][0].get('tool_supplement_materials')
    assert merged['tool_materials'][0]['unit_key']=='CH01:U2'


def test_refresh_conflicting_card_quarantines_old_content_and_marks_conflict(tmp_path):
    card=tmp_path/'card.json';card.write_text(json.dumps({'paper_identity':{'canonical_paper_id':'different-paper','doi':'10.1234/different'},
        'general_understanding':{'finding':'WRONG_NEW_PAPER_FINDING'}}))
    original={**source(),'card_path':str(card)}
    refreshed=p._refresh_local_material_snapshots([{'source_materials':[original]}])[0]['source_materials'][0]
    assert refreshed['study_summary_A']=={}
    assert original['study_summary_A']==source()['study_summary_A']
    assert refreshed['material_identity_conflict'] is True
    assert 'WRONG_NEW_PAPER_FINDING' not in json.dumps(refreshed)
    assert not p._owner_material_has_content(refreshed)


def test_direct_pool_payload_rejects_foreign_card_identity(tmp_path):
    card=tmp_path/'card.json';card.write_text(json.dumps({'paper_identity':{'canonical_paper_id':'different-paper'},
        'general_understanding':{'finding':'FOREIGN_CARD_RESULT'}}))
    raw={'_source_handle':'P0001','_paper_id':'paper-P0001','title':'Expected study','card_path':str(card)}
    payload=p.build_local_material_payload(raw)
    assert payload['material_identity_conflict']
    assert 'FOREIGN_CARD_RESULT' not in json.dumps(payload)
    rows,report=p._resolve_owner_source_materials(source_materials=[source()],chapter_plan=plan('P0001'),pool_rows=[raw])
    assert rows[0]['study_summary_A']==source()['study_summary_A']
    assert report['unresolved'][0]['reason']=='source_identity_conflict'


def test_legacy_card_without_identity_remains_usable(tmp_path):
    card=tmp_path/'card.json';card.write_text(json.dumps({'general_understanding':{'finding':'LEGACY_REAL_RESULT'}}))
    payload=p.build_local_material_payload({'_source_handle':'P0001','_paper_id':'paper-P0001','card_path':str(card)})
    assert p._owner_material_has_content(payload)
    assert 'LEGACY_REAL_RESULT' in json.dumps(payload)
    assert not payload.get('material_identity_conflict')


@pytest.mark.parametrize('container', ['general_understanding', 'review_planning'])
def test_direct_ab_identity_rejects_foreign_card_but_not_cited_example(tmp_path, container):
    card=tmp_path/'card.json'
    card.write_text(json.dumps({container:{'paper_identity':{'canonical_paper_id':'foreign'},'finding':'FOREIGN_NESTED'}}))
    raw={'_source_handle':'P0001','_paper_id':'paper-P0001','card_path':str(card)}
    assert p.build_local_material_payload(raw)['material_identity_conflict']
    refreshed=p._refresh_local_material_snapshots([{'source_materials':[{**source(),'card_path':str(card)}]}])[0]['source_materials'][0]
    assert refreshed['material_identity_conflict'] and 'FOREIGN_NESTED' not in json.dumps(refreshed)
    card.write_text(json.dumps({'general_understanding':{'finding':'SAME_STUDY_FINDING', 'examples':[{'paper_identity':{'canonical_paper_id':'cited-original'},'finding':'Legitimate review account'}]}}))
    assert p._owner_material_has_content(p.build_local_material_payload(raw))
