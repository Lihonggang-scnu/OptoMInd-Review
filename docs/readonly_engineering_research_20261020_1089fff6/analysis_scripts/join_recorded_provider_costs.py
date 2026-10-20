import csv,json,datetime
from pathlib import Path
from zoneinfo import ZoneInfo
O=Path(__file__).resolve().parents[1];A=O/'analysis'
def read(name):
 with (A/name).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
gates=read('GATE_OBSERVATIONS.csv');maps=read('MAPPING_COST_ROWS.csv')
by_id={r['provider_request_id']:r for r in maps if r['provider_request_id']}
assert len(by_id)==897
joined=[];used=set();decisions={}
for g in gates:
 row=dict(g);pid=g['provider_request_id']
 if pid:
  assert pid not in used and pid in by_id
  used.add(pid);m=by_id[pid]
  for key in ['actual_prompt_tokens','actual_completion_tokens','actual_total_tokens']:
   assert g[key]==m[key],(g['gate_id'],key)
  for k in ['cost_state','actual_cny','held_or_uncertain_cny','reserved_estimate_cny','pair_id','project_reservation_id','account_reservation_id','settlement_uncertain','actual_reasoning_tokens']:
   row[k]=m.get(k,'')
  row['mapping_call_id']=m['call_id'];row['mapping_source']=m['source'];row['join_basis']='exact_unique_provider_request_id_and_equal_recorded_usage'
  row['record_type']='gate_with_settled_cost_mapping'
  decisions[g['response_decision']]=decisions.get(g['response_decision'],0)+1
 else:
  row['join_basis']='no_provider_request_id; no fuzzy join or no-call inference'
 joined.append(row)
for m in maps:
 if m['provider_request_id'] not in used:
  row=dict(m);row['record_type']='unmatched_mapping_uncertain';row['join_basis']='no_provider_request_id; keep separate until lifecycle trace provides a proven link';joined.append(row)
assert len(used)==897
fields=list(dict.fromkeys(k for r in joined for k in r))
with (O/'CALL_AND_COST_SUMMARY.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(joined)
result={'observed_at':datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'matched_settled_gate_rows':897,'unmatched_gate_rows':sum(not r['provider_request_id'] for r in gates),'unmatched_cost_mappings':len(maps)-len(used),'csv_rows':len(joined),'settled_gate_response_decisions':decisions,'method':'Exact unique provider request ID with equal recorded prompt/completion/total usage. No topic, filename, namespace or native-call-id heuristic used. Row types are not additive physical calls. Uncertain mapping with missing provider ID remains separate; no cost or duration imputed.'}
(A/'PROVIDER_COST_JOIN_PROOF.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
