import csv,json,datetime,sqlite3,hashlib
from collections import defaultdict,Counter
from evidence_export_lib import *
rr=list(csv.DictReader((O/'CALL_AND_COST_SUMMARY.csv').open(encoding='utf-8-sig')))
by=defaultdict(list)
for r in rr:
 if r.get('messages_sha256'):by[r['messages_sha256']].append(r)
groups=[]
for h,rows in by.items():
 if len(rows)<2:continue
 sample=[]
 for r in rows:
  sample.append({k:r.get(k) for k in ['gate_id','call_id','native_call_id','namespace','chapter_id','unit_id','stage','module','model','messages_sha256','profile_sha256','kwargs_sha256','binding_sha256','provider_request_id','request_decision','response_decision','cost_state','actual_cny','consumption_state']})
 groups.append({'messages_sha256':h,'same_profile_sha256':len({r['profile_sha256'] for r in rows})==1,'same_kwargs_sha256':len({r['kwargs_sha256'] for r in rows})==1,'members':sample})
write_json('evidence/calls/EXACT_MESSAGE_REPEATS.json',{'group_count':len(groups),'member_rows':sum(len(g['members']) for g in groups),'groups':groups,'interpretation':'Same messages alone is not existing-contract equivalence. Profile/source/binding/raw-output path/call ID and lifecycle may differ. Gates with no physical response are not counted as paid calls.'})
observed=datetime.datetime.now(datetime.timezone.utc).isoformat();ledgers=[]
for label,p,cap in [('project',N/'budget.sqlite',110),('account',R/'local_accounting/qwen_first_20261010_300cny/budget.sqlite',300)]:
 before=sha(p.read_bytes());sidecars={s:Path(str(p)+s).exists() for s in ['-wal','-shm']}
 db=sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True)
 try:
  data=[{'status':s,'rows':c,'actual_cny_sum':a,'amount_cny_sum':m} for s,c,a,m in db.execute('SELECT status,COUNT(*),SUM(actual_cny),SUM(amount_cny) FROM reservations GROUP BY status')]
 finally:db.close()
 assert before==sha(p.read_bytes())
 settled=sum(x['actual_cny_sum'] or 0 for x in data if x['status']=='settled');held=sum(x['amount_cny_sum'] or 0 for x in data if x['status'] in ['reserved','uncertain'])
 ledgers.append({'scope':label,'observed_at_utc':observed,'cap_cny':cap,'states':data,'settled_actual_cny':settled,'held_reserved_and_uncertain_cny':held,'available_cny':cap-settled-held,'database_sha256_before_after':before,'sidecars_before':sidecars,'sidecars_after':{s:Path(str(p)+s).exists() for s in ['-wal','-shm']},'method':'immutable URI read-only SELECT aggregates; database not copied. Sidecar absence observed, no checkpoint. actual_cny is local settlement using recorded provider usage/pricing, not independently reconciled vendor invoice.'})
write_json('evidence/calls/COST_TOTALS_OBSERVED.json',{'observed_at_utc':observed,'ledgers':ledgers,'blank_policy':'No separate pending/held lifecycle column exists beyond actual native states in this table. Missing usage, parse/queue timing and independent invoice cost are unrecorded, not zero. Absent native reserved rows means observed count 0, not an inferred unknown release. Account totals include other projects; do not add project to account.'})
# Add proven consumption for this one case only; the rest remains unproven from gates alone.
dest=DEST/'CALL_AND_COST_SUMMARY.csv';keys=list(rr[0]);
for r in rr:
 if r.get('gate_id')=='441e0051b454ad1fb6724771d1b7853a27a15998bc64c294dfc836172e1326e1':r['consumption_state']='rejected_by_supervision;native_fragment_empty;normal_v29_kept_1657_body;explicit_reparse_path_not_proven_safe'
with dest.open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(clean(rr))
print(json.dumps({'repeat_groups':len(groups),'cost_totals':[(x['scope'],x['settled_actual_cny'],x['available_cny']) for x in ledgers]}))
