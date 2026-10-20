"""Read saved JSON only; no project imports, tokenizer, entrypoints or network."""
import json,csv,hashlib,statistics
from collections import Counter,defaultdict
from evidence_export_lib import O,N,DEST,write_json,alias_path,clean

def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def canon(x):return json.dumps(x,ensure_ascii=False,separators=(',',':')).encode('utf-8')
def sha(x):return hashlib.sha256(x).hexdigest()
def stages(root):
 for ch in sorted(root.iterdir()):
  if not ch.is_dir():continue
  for un in sorted(ch.iterdir()):
   if not un.is_dir():continue
   pp=[p for p in un.iterdir() if p.is_dir() and (p/'UNIT_MESSAGES.json').is_file()]
   assert len(pp)==1,(ch.name,un.name,len(pp))
   yield ch.name,un.name,pp[0]

normal=N/'stages/09_body_delivery_min600_normal_cache_handoff_v29/body'
actual=N/'stages/09_body_delivery_min600_quality_repair_v27/body'
report=load(normal/'DELIVERY_REPORT.json')
byunit={(u['chapter_id'],u['unit_id']):u for u in report['units']}
actualstage={(ch,un):p for ch,un,p in stages(actual/'units')}
rows=[];fields=[];pairs=defaultdict(list);presence=Counter();combinations=Counter();within=[]
for ch,un,p in stages(normal/'units'):
 msgp=p/'UNIT_MESSAGES.json';b=msgp.read_bytes();m=load(msgp);u=json.loads(m[1]['content']);sources=u.get('sources') or []
 ar=[f for f in actualstage[ch,un].rglob('RAW_RESPONSE.json') if 'quality' not in f.relative_to(actualstage[ch,un]).parts]
 assert len(ar)==1,(ch,un,len(ar));raw=load(ar[0]);usage=raw.get('usage') or {};events=raw.get('transport_events') or []
 def endpoint(name):
  vv=[e.get('epoch') for e in events if e.get('stage')==name and e.get('epoch') is not None]
  return vv[-1] if vv else None
 start=endpoint('request_open_start');opened=endpoint('response_opened');end=endpoint('stream_complete') or endpoint('stream_done')
 row={'record_type':'unit_author','chapter_id':ch,'unit_id':un,'message_file':alias_path(msgp),'message_sha256':sha(b),'file_bytes':len(b),'system_characters':len(m[0]['content']),'system_sha256':sha(m[0]['content'].encode()),'pretty_user_characters':len(m[1]['content']),'pretty_user_utf8_bytes':len(m[1]['content'].encode()),'compact_user_characters':len(canon(u).decode()),'source_records':len(sources),'sources_compact_characters':len(canon(sources).decode()),'prior_body_field_present':any(k in u for k in ['existing_body_markdown','previous_body','prior_body']),**byunit[ch,un]['estimate'],'provider_request_id':raw.get('request_id'),'native_call_id':raw.get('call_id'),'actual_prompt_tokens':usage.get('prompt_tokens'),'actual_completion_tokens':usage.get('completion_tokens'),'actual_total_tokens':usage.get('total_tokens'),'raw_response_sha256':raw.get('raw_response_sha256'),'raw_stream_sha256':raw.get('raw_stream_sha256'),'recorded_request_elapsed_seconds':raw.get('elapsed_seconds'),'http_open_latency_seconds':opened-start if opened is not None and start is not None else None,'stream_tail_read_seconds':end-opened if end is not None and opened is not None else None,'request_open_epoch':start,'response_opened_epoch':opened,'stream_complete_epoch':end,'queue_seconds':None,'isolated_generation_seconds':None,'parse_seconds':None,'assembly_seconds':None,'author_cache_hit':byunit[ch,un].get('cache_hit'),'quality_cache_hit':byunit[ch,un].get('quality',{}).get('cache_hit'),'quality_current_model_calls':byunit[ch,un].get('quality',{}).get('model_calls')}
 row.pop('input_capacity',None);rows.append(row)
 for k,v in u.items():fields.append({'chapter_id':ch,'unit_id':un,'field':k,'type':type(v).__name__,'compact_characters':len(canon(v).decode()),'compact_sha256':sha(canon(v))})
 local=Counter()
 for i,s in enumerate(sources):
  handle=str(s.get('source_handle') or s.get('source_id') or s.get('paper_id') or s.get('canonical_paper_id') or '')
  key=(handle,sha(canon(s)));local[key]+=1;pairs[key].append({'chapter_id':ch,'unit_id':un,'source_index':i})
  nz=tuple(sorted(k for k,v in s.items() if v not in (None,'',[],{})));combinations[nz]+=1
  for k in nz:presence[k]+=1
 within.extend({'chapter_id':ch,'unit_id':un,'source_id':k[0],'content_sha256':k[1],'count':v} for k,v in local.items() if v>1)
assert len(rows)==36
def csvwrite(rel,rr):
 pp=DEST/rel;pp.parent.mkdir(parents=True,exist_ok=True)
 keys=list(dict.fromkeys(k for r in rr for k in r))
 with pp.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(clean(rr))
csvwrite('CONTEXT_AND_TIMING.csv',rows)
csvwrite('evidence/context/UNIT_FIELD_HASHES.csv',fields)
write_json('evidence/context/SOURCE_RECORD_REPETITION_INDEX.json',{'method':'Exact pair(source handle, SHA256 compact complete source-record JSON), no trimming or semantic merging','total_records':sum(len(v) for v in pairs.values()),'unique_pairs':len(pairs),'within_request_duplicates':within,'cross_request_repeated_groups':sum(len(v)>1 for v in pairs.values()),'pairs':[{'source_id':k[0],'content_sha256':k[1],'occurrences':v} for k,v in pairs.items()],'field_presence':dict(presence),'nonempty_field_combinations':{'|'.join(k):v for k,v in combinations.items()}})
summary={}
for k in ['file_bytes','system_characters','pretty_user_characters','pretty_user_utf8_bytes','compact_user_characters','source_records','sources_compact_characters','prompt_tokens_estimate','reserved_input_tokens','total_context_tokens','actual_prompt_tokens','actual_completion_tokens','actual_total_tokens','recorded_request_elapsed_seconds','http_open_latency_seconds','stream_tail_read_seconds']:
 vv=[r[k] for r in rows if r.get(k) is not None];summary[k]={'recorded_rows':len(vv),'sum':sum(vv),'median':statistics.median(vv),'min':min(vv),'max':max(vv)}
write_json('evidence/context/CONTEXT_STATISTICS.json',{'rows':36,'summary':summary,'unique_provider_request_ids':len({r['provider_request_id'] for r in rows}),'unique_raw_response_hashes':len({r['raw_response_sha256'] for r in rows}),'token_method':'Saved native estimate with tokenizer=provided_token_counter. Not retokenized in this read-only task. Reservations add multiplier/framing. Provider usage is separately reported. Compact field character counts are non-additive to pretty request bytes and not token counts.','timing_method':'HTTP open latency is not queue time; stream tail is not isolated model generation. Missing fields stay blank. Sums across calls are not whole-run wall time.','source_scope':'Exact actual top-level model messages; mirrored wire preview is not counted again. Full input files are hashed, not truncated or replayed. Shared records are not asserted removable.','cache_current':{'author_hits':dict(Counter(r['author_cache_hit'] for r in rows)),'quality_hits':dict(Counter(r['quality_cache_hit'] for r in rows)),'quality_new_calls':dict(Counter(r['quality_current_model_calls'] for r in rows))}})
print(json.dumps({'rows':36,'prompt_estimate':summary['prompt_tokens_estimate'],'actual_prompt':summary['actual_prompt_tokens'],'source_pairs':len(pairs),'within_duplicates':len(within)},ensure_ascii=False))
