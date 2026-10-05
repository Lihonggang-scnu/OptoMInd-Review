# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
import copy,json,uuid
from pathlib import Path
import driver_outline_tournament as d
_,planning,runtime=d.source_imports()
from optomind_research.runtime.upgrade3.module4 import runtime as rt
rt.MODEL_PRICING_CNY['qwen3.8-max']={'tiers':((1000000,12.0,36.0),),'max_input_tokens':991808,'max_output_tokens':32768,'thinking_json':False}
planning.DEFAULT_TOKENIZER_PATH=Path(r'<LOCAL_REVIEW2_PATH>')
payload=d.load(d.OUT/'inputs'/'COMMON_REVISION_PAYLOAD.json')
payload['chapter_id']='Ch1';payload['chapter_feedback']=[];payload['editorial_feedback_for_chapter']=[]
payload['revision_request']='作为原章节负责人，一次自主检查这个任务簇及其实际材料和局部正文，判断有无值得修改的职责、段落展开、案例用途与衔接。不是仅做措辞修改。无需改变时允许no_change。保留给定两个unit_id和重要知识，在updated_plan.revision_notes说明具体问题、采纳的修改或保留原安排的依据。本次不重新检索，不写正文。'
messages=planning._messages_for('affected_chapter_revision',payload)
folder=d.OUT/'calls'/'R1_aligned_existing_owner';folder.mkdir(parents=True,exist_ok=True)
d.dump(folder/'request_messages.json',messages);d.dump(folder/'request_payload.json',payload)
client,ledger=d.make_r1_client(runtime,folder/'raw_responses')
d.dump(folder/'ledger_before.json',ledger.as_dict());cid='outline-tournament:R1-aligned:'+uuid.uuid4().hex[:10]
print('R1 aligned calling '+cid,flush=True)
raw=runtime[3](client,messages,model=d.R1_MODEL,call_id=cid,max_output_tokens=d.R1_OUTPUT_TOKENS,thinking_budget=d.R1_THINKING_BUDGET)
d.dump(folder/'response_envelope.json',raw);d.dump(folder/'ledger_after.json',ledger.as_dict())
response,telemetry=planning._parse_planner_response(raw);d.dump(folder/'response_parsed.json',response)
cp=planning._owner_response_plan(response)
if cp is None and response.get('status')=='no_change':cp=copy.deepcopy(payload['chapter_plan'])
assert cp and {u.get('unit_id') for u in cp['units']}==set(payload['task_cluster']['unit_ids'])
d.dump(d.OUT/'candidates'/'R1_aligned_owner_plan.json',cp)
d.dump(folder/'CALL_STATE.json',{'status':'complete','call_id':cid,'model':d.R1_MODEL,'telemetry':telemetry})
print('R1 aligned complete',flush=True)
