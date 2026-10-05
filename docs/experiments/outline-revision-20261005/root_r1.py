# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
import copy,json,re,uuid,difflib
from pathlib import Path
import driver_outline_tournament as d

arranging,planning,runtime=d.source_imports()
from optomind_research.runtime.upgrade3.module4 import runtime as rt
rt.MODEL_PRICING_CNY['qwen3.8-max']={'tiers':((1000000,12.0,36.0),),'max_input_tokens':991808,'max_output_tokens':32768,'thinking_json':False}
data=d.make_subset_inputs()
planning.DEFAULT_TOKENIZER_PATH=Path(r'<LOCAL_REVIEW2_PATH>')
payload=d.r1_payload(data)
body=d.BODY_PATH.read_text(encoding='utf-8')
start=re.search(r'^### 1\.1\s',body,re.M)
end=re.search(r'^### 1\.3\s',body,re.M)
assert start and end
payload['actual_local_body']=body[start.start():end.start()]
payload.pop('candidate_navigation',None)
# Both routes receive this exact frozen material and original plan snapshot.
d.dump(d.OUT/'inputs'/'COMMON_REVISION_PAYLOAD.json',payload)
messages=planning._messages_for('chapter_details',payload)
messages[-1]['content']+='\n本次是细纲修订实验，不是从零生成。保持给定两个unit_id；允许实质修改段落职责、论述顺序、案例用途与衔接。只返回chapter_plan；在其revision_notes中逐项写明原问题、修改对象、依据，以及拒绝或暂缓的修改。不要输出正文。'
folder=d.OUT/'calls'/'R1_strong_once';folder.mkdir(parents=True,exist_ok=True)
d.dump(folder/'request_messages.json',messages);d.dump(folder/'request_payload.json',payload)
client,ledger=d.make_r1_client(runtime,folder/'raw_responses')
d.dump(folder/'ledger_before.json',ledger.as_dict())
cid='outline-tournament:R1:'+uuid.uuid4().hex[:12]
d.dump(folder/'CALL_STATE.json',{'status':'calling','call_id':cid,'model':d.R1_MODEL})
print('R1 calling '+cid,flush=True)
raw=runtime[3](client,messages,model=d.R1_MODEL,call_id=cid,max_output_tokens=d.R1_OUTPUT_TOKENS,thinking_budget=d.R1_THINKING_BUDGET)
d.dump(folder/'response_envelope.json',raw);d.dump(folder/'ledger_after.json',ledger.as_dict())
parsed,telemetry=planning._parse_planner_response(raw)
d.dump(folder/'response_parsed.json',parsed)
cp=parsed['chapter_plan']
assert {u.get('unit_id') for u in cp['units']}==set(data['unit_ids']), 'stable unit IDs missing'
d.dump(d.OUT/'candidates'/'R1_candidate_plan.json',cp)
diff=''.join(difflib.unified_diff(json.dumps(payload['chapter_plan'],ensure_ascii=False,indent=2).splitlines(True),json.dumps(cp,ensure_ascii=False,indent=2).splitlines(True),fromfile='original',tofile='R1'))
(d.OUT/'candidates'/'R1_vs_original.diff').write_text(diff,encoding='utf-8')
d.dump(folder/'CALL_STATE.json',{'status':'complete','call_id':cid,'model':d.R1_MODEL,'telemetry':telemetry,'budget':ledger.as_dict()})
print('R1 complete '+str(ledger.as_dict()),flush=True)
