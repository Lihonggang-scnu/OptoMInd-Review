"""Capture synthetic boundary messages/materials; no model or network calls.
Run from repository root with PYTHONPATH=/tmp/optomind-wo01-deps:.
Baseline is the tested R2 progressive module, loaded with current local dependencies.
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from optomind_research.runtime.upgrade3 import progressive_review_plan as current

def deny(*a,**kw): raise AssertionError('Offline WO04 evidence: network prohibited')
socket.create_connection=deny
socket.socket.connect=deny
ROOT=Path(__file__).resolve().parent
BASE='1b4f7b6a7c0dafb6f7dfe59f46980480f44d993f'
def dump(name,value):
    (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def plan(*handles): return {'units':[{'unit_id':'U1','source_handles':list(handles)}]}
source={'source_handle':'P0001','paper_id':'paper-one','title':'Foundation study','study_summary_A':{'finding':'Foundation finding in a stated setting'}}
candidate={'source_handle':'P0002','paper_id':'original-study','title':'Classroom original study','doi':'10.1234/original','supplement_material':{'usable_content':'SOURCE_BOUND_RESULT: random assignment of 200 classrooms; mean score gain four points','conditions':['classroom randomization'],'reporting_review':'review-paper'}}

def capture(p,label,root):
    root.mkdir(parents=True)
    identity={k:v for k,v in source.items() if k!='study_summary_A'}
    status,_,_,errors=p._classify_owner_response(plan('P0001'),{'updated_plan':plan('P0001')},[identity])
    report={'synthetic':True,'identity_only':{'status':status,'errors':errors}}
    messages=[]
    def owner(stage,payload):
        messages.append(p._messages_for(stage,payload))
        return {'status':'updated','updated_plan':plan('P0001','P0002')}
    packet={'chapter':{'chapter_id':'CH01'},'chapter_plan':plan('P0001'),'source_materials':[source],'candidate_materials':[candidate]}
    packet_path=root/'packet.json';packet_path.write_text(json.dumps(packet))
    arrangement={'issues':[{'action':'chapter_owner','problem':'Use the supplied classroom comparison when useful'}]}
    arrangement_path=root/'arrangement.json';arrangement_path.write_text(json.dumps(arrangement))
    inputs=[]
    def arrange(packet,prior,path):
        return {'units':[{'unit_id':'U1','paragraph_tasks':[{'source_handles':['P0001','P0002']}]}]}
    def writer(packet,arrangement,path):
        inputs.append(copy.deepcopy(packet))
        return {'body_markdown':'Offline synthetic boundary only','complete':True}
    kwargs=dict(packet_path=packet_path,arrangement_path=arrangement_path,arrangement=arrangement,owner_planner=owner,arrangement_runner=arrange,writer_runner=writer,output_dir=root/'loop')
    result=p.run_feedback_loop(**kwargs)
    report['feedback_status']=result['status']; report['structural_errors']=result.get('structural_errors',[])
    dump(label+'_owner_messages.json',messages)
    report['owner_calls_first']=len(messages)
    if inputs:
        formal=json.loads(Path(result['updated_packet']).read_text())
        dump(label+'_formal_packet.json',formal)
        dump(label+'_writer_input.json',inputs[0])
        replay=p.run_feedback_loop(**kwargs)
        report['replay_status']=replay['status'];report['owner_calls_after_replay']=len(messages)
    else: report['writer_input_created']=False
    dump(label+'_owner_result.json',report)

with tempfile.TemporaryDirectory() as temp:
    temp=Path(temp)
    file=temp/'baseline/runtime/upgrade3/progressive.py';file.parent.mkdir(parents=True);file.write_bytes(subprocess.check_output(['git','show',BASE+':optomind_research/runtime/upgrade3/progressive_review_plan.py']))
    name='optomind_research.runtime.upgrade3._body04_baseline'
    spec=importlib.util.spec_from_file_location(name,file)
    baseline=importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name]=baseline;spec.loader.exec_module(baseline);baseline.PROJECT_ROOT=current.PROJECT_ROOT
    capture(baseline,'before',temp/'before')
    capture(current,'after',temp/'after')
