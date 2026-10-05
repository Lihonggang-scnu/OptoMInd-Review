# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
import json,sys,uuid,copy
from pathlib import Path
import driver_outline_tournament as d
arranging,planning,runtime=d.source_imports()
planning.DEFAULT_TOKENIZER_PATH=Path(r'<LOCAL_REVIEW2_PATH>')
from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient,GlobalBudgetLedger
common=d.load(d.OUT/'inputs'/'COMMON_REVISION_PAYLOAD.json')
order=int(sys.argv[1]) if len(sys.argv)>1 else 0
folder=d.OUT/'evaluation';folder.mkdir(exist_ok=True)
output=folder/f'NEW_BLIND_ORDER{order}.json'
if output.exists():print('saved result reused');raise SystemExit(0)
labels={'X':'R2_candidate','Y':'control_original','Z':'R1_candidate'}
paths={'control_original':d.OUT/'inputs'/'base_subset_plan.json','R1_candidate':d.OUT/'candidates'/'R1_candidate_plan.json','R2_candidate':d.OUT/'candidates'/'R2_candidate_plan.json'}
arms={}
for k,arm in list(labels.items())[::1 if order==0 else -1]:
    cp=copy.deepcopy(d.load(paths[arm]));cp.pop('revision_notes',None)
    bodies=list((d.OUT/'production'/arm/'writer').glob('*/UNIT_BODY.md'))
    assert len(bodies)==2, str((arm,bodies))
    bodies=sorted(bodies,key=lambda p:p.parent.name)
    arms[k]={'fine_outline':cp,'actual_new_bodies':[{'unit_id':p.parent.name,'body':p.read_text(encoding='utf-8')} for p in bodies]}
payload={'research_question':common['research_question'],'review_argument':common['review_argument'],'scope':common['shared_scope'],'other_chapters_brief':common['shared_outline'],'materials':common['source_materials'],'tool_materials':common['tool_materials'],'anonymous_candidates':arms}
guide='''你是独立的研究综述质量评议者。这是同一个两单元任务簇的三份候选细纲及各自重新生成的正文；不告诉你模型、路线和哪个是原版，不猜身份。候选顺序没有意义。阅读真实材料判断实际认识质量：比较是否增加解释、案例用途是否不同、条件和研究对象是否正确、机制/阴性结果/必要知识是否保留、具体内容是否真的从细纲传到正文。不能用更多字/更短/更多引用/段落数量代替质量。叙述性综述转述的原始研究与其他来源同等使用，不因来源层级降权。材料可能有前后矛盾或总体与亚组认识不完整，不能把某份A/B的措辞机械当真理；分辨明确支持、尚不能判定和明显误配。
这是局部对比，不是整章全文：共享thesis和其他章节轻量职责不能证明未提供的邻近单元根本没讲某事。不把局部提不到内容等同于全领域无研究。不要求所有资料一一用完。
请JSON返回 {"ranking":["X","Y","Z"],"dimension_comparison":{"organization":"...","knowledge_preservation":"...","conditions_and_facts":"...","body_realization":"..."},"candidate_findings":{"X":{"improvements":[...],"problems":[...],"representative_passages":[{"text":"正文逐字片段","judgment":"...","source_handles":[...]}]},"Y":{...},"Z":{...}},"confidence":"...","limits":[...]}。每份只挑2-3个决定性变化，正文quote要逐字，不虚构数字。保留合理不改/有利复用。排名可有并列，明确知识收益和代价，不能仅给分数。'''
messages=[{'role':'system','content':guide},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
d.dump(folder/f'NEW_BLIND_ORDER{order}_MESSAGES.json',messages);d.dump(folder/'NEW_BLIND_LABEL_MAP.json',labels)
counter=planning.qwen_local_token_counter(planning.DEFAULT_TOKENIZER_PATH)
ledger=GlobalBudgetLedger(path=d.LEDGER_PATH,limit_cny=10)
client=QwenDirectClient(model='qwen3.5-plus',key_file=d.KEY_PATH,max_retries=0,max_keys=1,timeout_seconds=900,max_output_tokens=8192,thinking=True,thinking_budget=2048,json_mode=False,raw_response_dir=folder/f'order{order}_raw',budget_ledger=ledger,prompt_token_counter=counter,prompt_token_multiplier=planning.TOKEN_MARGIN_MULTIPLIER,prompt_token_framing_margin=planning.TOKEN_FRAMING_MARGIN)
cid='outline-tournament:blind-order'+str(order)+':'+uuid.uuid4().hex[:10]
print('new blind calling '+cid,flush=True)
raw=runtime[3](client,messages,model='qwen3.5-plus',call_id=cid)
d.dump(folder/f'NEW_BLIND_ORDER{order}_ENVELOPE.json',raw)
parsed,telemetry=planning._parse_planner_response(raw);d.dump(output,parsed)
print(json.dumps({'ranking':parsed.get('ranking'),'telemetry':telemetry},ensure_ascii=False),flush=True)
