from __future__ import annotations
import copy,json,hashlib,sys
from pathlib import Path
SOURCE=Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
ROOT=Path(r"F:\OptoMind-Review-2\outputs\outline_full_strengthening_20261006_40cny")
BASE=ROOT/'ch5_full_material'
OUT=ROOT/'ch5_clusters'
TOKENIZER=Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
sys.path.insert(0,str(SOURCE))
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile

def load(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def dump(p,v): Path(p).parent.mkdir(parents=True,exist_ok=True); Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
def sha(v): return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
def chars(v): return len(json.dumps(v,ensure_ascii=False,separators=(',',':')))
base=load(BASE/'INPUT_PAYLOAD.json')
profile=load(SOURCE/'config/outline_revision/quality.json')['profiles']['strong_outline_chapter']
counter=planning.qwen_local_token_counter(TOKENIZER)
all_units=base['chapter_plan']['units']
all_ids=[str(u['unit_id']) for u in all_units]
base_roles=copy.deepcopy(base.get('readonly_neighbor_unit_roles') or [])
clusters=[('cluster_01',['Ch5_U01','Ch5_U02','Ch5_U03']),('cluster_02',['Ch5_U04','Ch5_U05'])]
rows=[]
for name,editable in clusters:
    payload=copy.deepcopy(base)
    payload['call_id']=f'outline-full-strengthening-20261006:Ch5:{name}'
    payload['chapter_plan']['units']=[copy.deepcopy(u) for u in all_units if str(u.get('unit_id')) in editable]
    current_readonly=[]
    for u in all_units:
        uid=str(u.get('unit_id') or '')
        if uid in editable: continue
        current_readonly.append({
            'unit_id':uid,'chapter_id':payload['chapter_id'],'chapter_title':payload.get('chapter',{}).get('title',''),
            'substantive_point':u.get('substantive_point',''),'ordered_development':u.get('ordered_development',''),
            'synthesis':u.get('synthesis',''),'transition':u.get('transition',''),'read_only':True,
            'same_chapter_readonly':True,
        })
    payload['readonly_neighbor_unit_roles']=base_roles+current_readonly
    payload['modifiable_unit_ids']=editable
    payload['read_only_unit_ids']=[str(x.get('unit_id')) for x in payload['readonly_neighbor_unit_roles']]
    payload['unit_identity_contract']['existing_unit_ids']=editable
    payload['input_integrity']=strengthening._input_integrity(payload)
    strengthening._verify_envelope(payload)
    messages=strengthening.strengthening_messages(payload)
    estimate=strengthening.estimate_strengthening_request(messages,profile=profile,token_counter=counter)
    directory=OUT/name
    dump(directory/'INPUT_PAYLOAD.json',payload)
    dump(directory/'FULL_MAX_MESSAGES.json',{'messages':messages,'sha256':sha(messages),'profile':profile})
    report={'status':'prepared_no_paid_calls','mode':'full_material_max_cluster','chapter_id':payload['chapter_id'],'cluster':name,'modifiable_unit_ids':editable,'read_only_unit_ids':payload['read_only_unit_ids'],'source_material_count':len(payload['source_materials']),'tool_material_count':len(payload['tool_materials']),'neighbor_role_count':len(payload['readonly_neighbor_unit_roles']),'serialized_message_chars':chars(messages),'estimate':estimate,'payload_sha256':sha(payload),'messages_sha256':sha(messages),'source_head_at_prepare':'a0b645e096a148382196b5aadd3dcf36c43c69d6','cluster_reason':'full Ch5 serialized request exceeds the preparation context assumption; preserve all other Ch5 unit duties as read-only context.'}
    dump(directory/'PREPARE_REPORT.json',report)
    rows.append(report)
dump(OUT/'PREPARE_REPORT.json',{'status':'prepared_no_paid_calls','chapter_id':'Ch5','clusters':rows,'cluster_total_estimated_cost_cny':sum(float(r['estimate']['estimated_cost_cny']) for r in rows),'cluster_total_reserved_input_tokens':sum(int(r['estimate']['reserved_input_tokens']) for r in rows),'full_chapter_estimated_cost_cny':load(BASE/'PREPARE_REPORT.json')['estimates']['full_material_max']['estimated_cost_cny'],'full_chapter_prompt_tokens':load(BASE/'PREPARE_REPORT.json')['estimates']['full_material_max']['prompt_tokens_estimate'],'source_head_at_prepare':'a0b645e096a148382196b5aadd3dcf36c43c69d6'})
print(json.dumps({'clusters':[(r['cluster'],r['estimate']['prompt_tokens_estimate'],r['estimate']['estimated_cost_cny']) for r in rows],'total':sum(float(r['estimate']['estimated_cost_cny']) for r in rows)},ensure_ascii=False))
