# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
import copy,json,uuid,difflib
from pathlib import Path
import driver_outline_tournament as d
arranging,planning,runtime=d.source_imports()
from optomind_research.runtime.upgrade3.module4 import runtime as rt
rt.MODEL_PRICING_CNY['qwen3.8-max']={'tiers':((1000000,12.0,36.0),),'max_input_tokens':991808,'max_output_tokens':32768,'thinking_json':False}
planning.DEFAULT_TOKENIZER_PATH=Path(r'<LOCAL_REVIEW2_PATH>')
common=d.load(d.OUT/'inputs'/'COMMON_REVISION_PAYLOAD.json')
def call(stage,messages):
    folder=d.OUT/'calls'/stage;folder.mkdir(parents=True,exist_ok=True)
    d.dump(folder/'request_messages.json',messages)
    if (folder/'response_parsed.json').exists():return d.load(folder/'response_parsed.json')
    client,ledger=d.make_r1_client(runtime,folder/'raw_responses')
    d.dump(folder/'ledger_before.json',ledger.as_dict())
    cid='outline-tournament:'+stage+':'+uuid.uuid4().hex[:12]
    d.dump(folder/'CALL_STATE.json',{'status':'calling','call_id':cid,'model':d.R1_MODEL})
    print(stage+' calling '+cid,flush=True)
    raw=runtime[3](client,messages,model=d.R1_MODEL,call_id=cid,max_output_tokens=d.R1_OUTPUT_TOKENS,thinking_budget=d.R1_THINKING_BUDGET)
    d.dump(folder/'response_envelope.json',raw);d.dump(folder/'ledger_after.json',ledger.as_dict())
    parsed,telemetry=planning._parse_planner_response(raw);d.dump(folder/'response_parsed.json',parsed)
    d.dump(folder/'CALL_STATE.json',{'status':'complete','call_id':cid,'model':d.R1_MODEL,'telemetry':telemetry,'budget':ledger.as_dict()})
    print(stage+' complete',flush=True)
    return parsed

review_messages=[{'role':'system','content':'你是独立的细纲审稿者。阅读研究问题、全文组织、当前局部细纲、对应实际正文和真实材料，判断这个任务簇怎样更好地增加读者认识。检查职责重叠、比较依据、条件、研究归属、重要内容遗漏以及机制和阴性结果的保留。给出问题与依据，不替章节负责人预写必须照抄的答案；原安排合理时可以无修改意见。综述转述原始研究凭可用内容和引用身份具有同等使用价值，不要求自身A/B或全文。输出JSON：{"chapter_feedback":[{"chapter_id":"...","unit_ids":["..."],"issue":"...","evidence_source_handles":["..."],"evidence":"...","suggested_direction":"...","priority":"..."}],"global_assessment":"...","valid_arrangements_to_preserve":["..."]}。不写正文，不猜测材料未记录的事实。'}, {'role':'user','content':json.dumps(common,ensure_ascii=False)}]
review=call('R2_independent_review',review_messages)
payload=copy.deepcopy(common)
payload['chapter_id']='Ch1'
payload['chapter_feedback']=review.get('chapter_feedback',[])
payload['editorial_feedback_for_chapter']=[{'assessment':review.get('global_assessment'),'preserve':review.get('valid_arrangements_to_preserve')}]
payload['revision_request']='作为原章节负责人独立判断审稿意见，不将其当成必须服从的科学答案。材料充分则实质修订任务职责、段落展开、案例用途与衔接；合理不改允许no_change。保留给定两个unit_id和重要知识，在updated_plan.revision_notes说明采纳、拒绝或暂缓的每条意见与依据。本次不重新检索，不写正文。'
messages=planning._messages_for('affected_chapter_revision',payload)
response=call('R2_existing_owner',messages)
cp=planning._owner_response_plan(response)
if cp is None and response.get('status')=='no_change':cp=copy.deepcopy(common['chapter_plan'])
assert cp is not None,'owner plan missing'
assert {u.get('unit_id') for u in cp['units']}==set(common['task_cluster']['unit_ids']),'stable IDs missing'
d.dump(d.OUT/'candidates'/'R2_candidate_plan.json',cp)
diff=''.join(difflib.unified_diff(json.dumps(common['chapter_plan'],ensure_ascii=False,indent=2).splitlines(True),json.dumps(cp,ensure_ascii=False,indent=2).splitlines(True),fromfile='original',tofile='R2'))
(d.OUT/'candidates'/'R2_vs_original.diff').write_text(diff,encoding='utf-8')
print('R2 candidate ready',flush=True)
