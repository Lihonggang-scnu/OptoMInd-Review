"""Read saved metadata only; never invoke a provider or consume model text."""
import json,csv,hashlib,datetime
from pathlib import Path
from collections import Counter,defaultdict
from zoneinfo import ZoneInfo
O=Path(__file__).resolve().parents[1];N=O.parents[1]
def read(p):
 if not p.is_file():return None
 try:return json.loads(p.read_text(encoding='utf-8-sig'))
 except (ValueError,UnicodeError):return None
def scalar(v):return v if isinstance(v,(str,int,float,bool)) else ''
def write_csv(name,rows):
 fields=list(dict.fromkeys(k for r in rows for k in r))
 with (O/'analysis'/name).open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
rows=[];gate_rows=[];mapping_rows=[];errors=[];timing_events=[]
for d in sorted((N/'supervision/calls').iterdir()):
 if not d.is_dir():continue
 req=read(d/'REQUEST.json') or {};profile=req.get('profile',{});kwargs=req.get('kwargs',{})
 preview=profile.get('preview',{});pe=preview.get('prompt_estimate',{})
 rd=read(d/'REQUEST_DECISION.json') or {};sd=read(d/'RESPONSE_DECISION.json') or {}
 invocation=read(d/'INVOCATION.json') or {};err=read(d/'INVOCATION_ERROR.json') or {}
 response=read(d/'RESPONSE.json') or {};result=response.get('result',{})
 info=result if isinstance(result,dict) else {};usage=info.get('usage',{})
 if not isinstance(usage,dict):usage={}
 payload={}
 for msg in req.get('messages',[]):
  if msg.get('role')!='user' or not isinstance(msg.get('content'),str):continue
  text=msg['content'];start=text.find('{')
  if start>=0:
   try:
    possible,_=json.JSONDecoder().raw_decode(text[start:])
    if isinstance(possible,dict):payload=possible
   except ValueError:pass
 nested=payload.get('original_writer_payload',{})
 chapter=payload.get('chapter_id',nested.get('chapter_id',''))
 unit=payload.get('unit_id',nested.get('unit_id',''))
 transport={}
 for event in info.get('transport_events',[]) if isinstance(info.get('transport_events'),list) else []:
  if not isinstance(event,dict):continue
  stage=event.get('stage','');transport[stage]=event
  epoch=event.get('epoch');stamp=datetime.datetime.fromtimestamp(epoch,ZoneInfo('Asia/Shanghai')).isoformat() if isinstance(epoch,(int,float)) else ''
  timing_events.append({'record_type':'recorded_transport_event','chapter_id':chapter,'unit_id':unit,'qualified_unit':chapter+':'+unit if chapter and unit else '','gate_id':d.name,'call_id':req.get('call_id',''),'event':stage,'recorded_at':stamp,'elapsed_seconds':event.get('elapsed_seconds',''),'sequence':event.get('sequence',''),'request_body_bytes':event.get('request_body_bytes',''),'request_body_sha256':event.get('request_body_sha256',''),'event_count':event.get('event_count',''),'raw_bytes':event.get('raw_bytes',''),'note':'Existing transport telemetry only; not an inferred queue/generation/parse/assembly decomposition.'})
 # No source/model text goes into this metadata projection.
 row={'record_type':'supervision_gate','gate_id':d.name,'call_id':req.get('call_id',''),'native_call_id':kwargs.get('call_id',''),'namespace':profile.get('supervision_namespace',profile.get('namespace','')),'stage':req.get('stage',''),'module':profile.get('module',''),'model':kwargs.get('model',profile.get('model','')),'messages_sha256':req.get('messages_sha256',''),'profile_sha256':req.get('profile_sha256',''),'kwargs_sha256':req.get('kwargs_sha256',''),'request_sha256':invocation.get('request_sha256',sd.get('request_sha256',rd.get('request_sha256',''))),'response_file_sha256':hashlib.sha256((d/'RESPONSE.json').read_bytes()).hexdigest() if (d/'RESPONSE.json').is_file() else '','result_sha256':response.get('result_sha256',''),'request_decision':rd.get('decision',''),'response_decision':sd.get('decision',''),'invocation_marker_present':(d/'INVOCATION.json').is_file(),'saved_response_present':(d/'RESPONSE.json').is_file(),'invocation_error_present':(d/'INVOCATION_ERROR.json').is_file(),'error_type':scalar(err.get('error_type',err.get('type',''))),'response_received_at':scalar(info.get('received_at',info.get('completed_at',''))),'elapsed_seconds':scalar(info.get('elapsed_seconds','')),'provider_request_id':scalar(info.get('request_id','')),'actual_prompt_tokens':usage.get('prompt_tokens',''),'actual_completion_tokens':usage.get('completion_tokens',''),'actual_total_tokens':usage.get('total_tokens',''),'estimated_prompt_tokens':pe.get('prompt_tokens',''),'measured_input_tokens':pe.get('measured_tokens',''),'input_estimate_method':pe.get('method',''),'estimated_reservation_cny':preview.get('estimated_reservation_cny',''),'cost_state':'','actual_cny':'','held_or_uncertain_cny':'','consumption_state':'not_deduced_from_gate_alone','source':'N/supervision/calls/'+d.name,'timing_note':'File mtimes are not queue/request/provider/parse durations; invocation marker alone does not prove physical dispatch.'}
 row.update({'chapter_id':chapter,'unit_id':unit,'qualified_unit':chapter+':'+unit if chapter and unit else '','binding_sha256':profile.get('binding_sha256',''),'source_identity_fingerprint':'not_separately_stored_in_supervision_metadata','transport_request_start_epoch':transport.get('request_open_start',{}).get('epoch',''),'transport_response_opened_epoch':transport.get('response_opened',{}).get('epoch',''),'transport_complete_epoch':transport.get('stream_complete',{}).get('epoch','')})
 gate_rows.append(row)
for p in sorted((N/'account_mappings').glob('*.json')):
 data=read(p)
 if not isinstance(data,dict):errors.append(p.name);continue
 st=data.get('settlement') or {};tele=st.get('telemetry') or {};usage=tele.get('usage') or {}
 state=data.get('state','');actual=st.get('actual_cny','') if state=='settled' else ''
 row={'record_type':'dual_ledger_mapping','gate_id':'','call_id':data.get('call_id',''),'native_call_id':'','namespace':'','stage':'','module':'','model':tele.get('returned_model',''),'messages_sha256':'','profile_sha256':'','kwargs_sha256':'','request_sha256':'','response_file_sha256':'','result_sha256':tele.get('raw_response_sha256',''),'request_decision':'','response_decision':'','provider_request_id':tele.get('request_id',''),'actual_prompt_tokens':usage.get('prompt_tokens',''),'actual_completion_tokens':usage.get('completion_tokens',''),'actual_total_tokens':usage.get('total_tokens',''),'actual_reasoning_tokens':(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',''),'cost_state':state,'actual_cny':actual,'held_or_uncertain_cny':data.get('amount_cny','') if state in ['reserved','held','pending','uncertain'] else '','reserved_estimate_cny':data.get('amount_cny',''),'pair_id':data.get('pair_id',''),'project_reservation_id':data.get('project_reservation_id',''),'account_reservation_id':data.get('total_reservation_id',''),'settlement_uncertain':st.get('uncertain',''),'consumption_state':'not_deduced_from_cost_mapping','source':'N/account_mappings/'+p.name,'timing_note':'Provider usage and settled actual_cny are measured fields; amount_cny is a reservation estimate, never substitute for actual_cny.'}
 mapping_rows.append(row)
write_csv('GATE_OBSERVATIONS.csv',gate_rows);write_csv('MAPPING_COST_ROWS.csv',mapping_rows)
write_csv('CALL_AND_COST_SUMMARY_BASE.csv',gate_rows+mapping_rows)
write_csv('TRANSPORT_EVENTS.csv',timing_events)
messages=defaultdict(list)
for r in gate_rows:
 if r['messages_sha256']:messages[r['messages_sha256']].append(r)
groups=[]
for h,rs in messages.items():
 if len(rs)>1:
  groups.append({'messages_sha256':h,'count':len(rs),'gate_ids':[r['gate_id'] for r in rs],'call_ids':[r['call_id'] for r in rs],'model_config_fingerprints':[r['kwargs_sha256'] for r in rs],'response_decisions':[r['response_decision'] for r in rs],'note':'Exact same stored messages alone does not establish reusable request identity; profile, model/generation config, contract, namespace and lifecycle may differ.'})
summary={'observed_at':datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'gate_count':len(gate_rows),'gate_request_decisions':dict(Counter(r['request_decision'] for r in gate_rows)),'gate_response_decisions':dict(Counter(r['response_decision'] for r in gate_rows)),'mapping_count':len(mapping_rows),'mapping_states':dict(Counter(r['cost_state'] for r in mapping_rows)),'mapping_settled_actual_cny_sum':round(sum(float(r['actual_cny']) for r in mapping_rows if r['actual_cny']!=''),10),'mapping_held_uncertain_amount_sum':round(sum(float(r['held_or_uncertain_cny']) for r in mapping_rows if r['held_or_uncertain_cny']!=''),10),'exact_message_hash_repeat_groups':groups,'parse_errors':errors,'blank_fields':'Missing/unrecorded stays blank. Separate gate and mapping record types are NOT additive physical call counts; no fuzzy call-ID join and no ledger DB copied. CSV cost rows use saved mapping state, not an assertion of current SQLite reconciliation.'}
(O/'analysis/CALL_METADATA_INDEX.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='exact_message_hash_repeat_groups'},ensure_ascii=False))
