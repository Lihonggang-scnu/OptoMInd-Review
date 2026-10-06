from __future__ import annotations
import copy,json,hashlib,sys
from pathlib import Path
SOURCE=Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
ROOT=Path(r"F:\OptoMind-Review-2\outputs\outline_full_strengthening_20261006_40cny")
BASE=ROOT/'ch5_full_material'
OUT=ROOT/'ch5_scoped_clusters'
TOKENIZER=Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
sys.path.insert(0,str(SOURCE))
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile

def load(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def dump(p,v): Path(p).parent.mkdir(parents=True,exist_ok=True); Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
def sha(v): return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
def chars(v): return len(json.dumps(v,ensure_ascii=False,separators=(',',':')))
def handles(value):
    out=set()
    if isinstance(value,dict):
        if value.get('source_handle'): out.add(str(value['source_handle']))
        for x in value.values(): out |= handles(x)
    elif isinstance(value,list):
        for x in value: out |= handles(x)
    return out
base=load(BASE/'INPUT_PAYLOAD.json')
profile=load(SOURCE/'config/outline_revision/quality.json')['profiles']['strong_outline_chapter']
counter=planning.qwen_local_token_counter(TOKENIZER)
all_units=base['chapter_plan']['units']
base_roles=copy.deepcopy(base.get('readonly_neighbor_unit_roles') or [])
clusters=[('cluster_01',['Ch5_U01','Ch5_U02','Ch5_U03']),('cluster_02',['Ch5_U04','Ch5_U05'])]
reports=[]
for name,editable in clusters:
    payload=copy.deepcopy(base)
    plan_units=[copy.deepcopy(u) for u in all_units if str(u.get('unit_id')) in editable]
    relevant=set().union(*(handles(u) for u in plan_units))
    payload['source_materials']=[copy.deepcopy(r) for r in base['source_materials'] if str(r.get('source_handle') or '') in relevant]
    payload['candidate_materials']=[copy.deepcopy(r) for r in base.get('candidate_materials') or [] if str(r.get('source_handle') or '') in relevant]
    navigation=copy.deepcopy(base.get('candidate_navigation') or {})
    if isinstance(navigation,dict) and isinstance(navigation.get('candidate_materials'),list):
        navigation['candidate_materials']=[copy.deepcopy(r) for r in navigation['candidate_materials'] if str(r.get('source_handle') or '') in relevant]
    payload['candidate_navigation']=navigation
    # All available tool records are retained; they are mediated source content,
    # and can carry cross-unit conditions even without a top-level handle.
    payload['tool_materials']=copy.deepcopy(base.get('tool_materials') or [])
    payload['chapter_plan']['units']=plan_units
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
    payload['material_transport']['scoped_source_handles']=sorted(relevant)
    payload['material_transport']['scope_basis']='current chapter unit source_handles, case_groups and supporting_studies; complete selected records retained'
    payload['input_integrity']=strengthening._input_integrity(payload)
    strengthening._verify_envelope(payload)
    messages=strengthening.strengthening_messages(payload)
    estimate=strengthening.estimate_strengthening_request(messages,profile=profile,token_counter=counter)
    directory=OUT/name
    dump(directory/'INPUT_PAYLOAD.json',payload)
    dump(directory/'FULL_MAX_MESSAGES.json',{'messages':messages,'sha256':sha(messages),'profile':profile})
    report={'status':'prepared_no_paid_calls','mode':'scoped_full_material_max_cluster','chapter_id':payload['chapter_id'],'cluster':name,'modifiable_unit_ids':editable,'relevant_source_handles':sorted(relevant),'source_material_count':len(payload['source_materials']),'candidate_material_count':len(payload['candidate_materials']),'tool_material_count':len(payload['tool_materials']),'neighbor_role_count':len(payload['readonly_neighbor_unit_roles']),'serialized_message_chars':chars(messages),'estimate':estimate,'payload_sha256':sha(payload),'messages_sha256':sha(messages),'source_head_at_prepare':'a0b645e096a148382196b5aadd3dcf36c43c69d6','scope_warning':'This is a measured scoped-full option for context sizing; it does not claim omitted chapter records are irrelevant or make access planning a hard material filter.'}
    dump(directory/'PREPARE_REPORT.json',report)
    reports.append(report)
dump(OUT/'PREPARE_REPORT.json',{'status':'prepared_no_paid_calls','chapter_id':'Ch5','clusters':reports,'cluster_total_estimated_cost_cny':sum(float(r['estimate']['estimated_cost_cny']) for r in reports),'full_chapter_estimated_cost_cny':load(BASE/'PREPARE_REPORT.json')['estimates']['full_material_max']['estimated_cost_cny'],'source_head_at_prepare':'a0b645e096a148382196b5aadd3dcf36c43c69d6'})
print(json.dumps({'clusters':[(r['cluster'],r['source_material_count'],r['estimate']['prompt_tokens_estimate'],r['estimate']['total_context_tokens'],r['estimate']['estimated_cost_cny']) for r in reports],'total':sum(float(r['estimate']['estimated_cost_cny']) for r in reports)},ensure_ascii=False))
