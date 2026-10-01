from pathlib import Path
import json,hashlib,importlib.util
B=Path(r'F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001');S=Path(r'F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930');A=B/'worktree/archives/manuscript-parts-local-test-20261001'
spec=importlib.util.spec_from_file_location('builder',B/'build_archive.py');b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
b.SENSITIVE_KEYS.discard('sources');b.SENSITIVE_KEYS.discard('references');b.SENSITIVE_KEYS.add('abstract')
def sha(x):return hashlib.sha256(x).hexdigest()
def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def save(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
plans=[('new_plan',S/'new_plan/DETAILED_REVIEW_PLAN.json'),('body_restore_20261001',S/'body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json'),('old_baseline',Path(r'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\DETAILED_REVIEW_PLAN.json'))]
quotes=set()
def collect(x,path=()):
 if isinstance(x,dict):
  for k,v in x.items():
   if k=='text' and 'local_passages' in path and isinstance(v,str) and len(v)>400:quotes.add(v)
   collect(v,path+(k,))
 elif isinstance(x,list):
  for v in x:collect(v,path)
for _,p in plans:collect(load(p))
variants=sorted({v for q in quotes for v in [q,json.dumps(q,ensure_ascii=False)[1:-1]]},key=len,reverse=True)
def clean(x):
 if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
 if isinstance(x,list):return [clean(v) for v in x]
 if not isinstance(x,str):return x
 for q in variants:
  while q[:200] in x:
   start=x.find(q[:200]);n=200
   while n<len(q) and start+n<len(x) and q[n]==x[start+n]:n+=1
   x=x[:start]+'[REDACTED_ORIGINAL_PASSAGE]'+x[start+n:]
 return x
fragments={}
def externalize(x,is_root=False):
 if not isinstance(x,(dict,list)):return x
 raw=json.dumps(x,ensure_ascii=False,sort_keys=True).encode();key=sha(raw)
 if len(raw)<2500:return x
 if key in fragments:return {'_archive_fragment':fragments[key]}
 out={k:externalize(v) for k,v in x.items()} if isinstance(x,dict) else [externalize(v) for v in x]
 if is_root:return out
 rel='curated/complete_plans/fragments/'+key+'.json';save(A/rel,out);fragments[key]=rel
 return {'_archive_fragment':rel}
m=load(A/'manifest.json');entries={e['relative_path']:e for e in m['files']};index=[]
for run,p in plans:
 rel='curated/complete_plans/'+run+'.json';cleaned=clean(b.redact(load(p)));save(A/rel,externalize(cleaned,True))
 index.append({'run':run,'root':rel,'original_path':str(p),'original_sha256':sha(p.read_bytes()),'top_level_fields':list(cleaned),'note':'All keys and list entries retained; publisher-origin fields redacted; recursive archive fragments replace repeated or large containers.'})
 entries[rel]={'relative_path':rel,'purpose':'complete public plan root; resolve recursive _archive_fragment to recover full public plan','run':run,'original_path':str(p),'original_sha256':sha(p.read_bytes()),'original_bytes':p.stat().st_size,'transform':'recursive content fragments with original-text redaction','redaction':True}
save(A/'curated/complete_plans/INDEX.json',{'plans':index,'fragment_count':len(fragments),'fragment_base':'paths relative to archive root','not_production_cache':True})
shutil=None
(A/'curated/archive_tooling/export_complete_plans.py').write_bytes((B/'export_complete_plans.py').read_bytes())
r=A/'README.md';t=r.read_text(encoding='utf8').replace('规划中的 `_archive_material_ref` 对应这些记录键。','packet 的 `.materials.json` 中 `material_refs[].record_sha256` 对应这些记录键。')
t+='\n`curated/complete_plans/INDEX.json` 另外提供3次完整规划的公开结构版本：保留全部顶层字段及数组条目，包含章节、工具结果、writer packets，不用形状摘要代替实际内容。递归 `_archive_fragment` 指向同目录 fragments 文件（路径以归档根为基准）；相同容器只存一份，方便云端程序展开。此版本保留来源身份目录；原文字段仍明确删节。\n';r.write_text(t,encoding='utf8')
rows=[]
for p in sorted(A.rglob('*')):
 if not p.is_file() or p==A/'manifest.json':continue
 rel=p.relative_to(A).as_posix();e=entries.get(rel,{'relative_path':rel,'purpose':'complete plan fragment or archival navigation','run':'archive preparation','original_path':None,'transform':'generated from public redacted plan'})
 e.update(public_sha256=sha(p.read_bytes()),public_bytes=p.stat().st_size);rows.append(e)
m.update(files=rows,file_count=len(rows),public_bytes_excluding_manifest=sum(e['public_bytes'] for e in rows));save(A/'manifest.json',m)
print(json.dumps({'plans':len(index),'fragments':len(fragments),'archive_files':len(rows)},ensure_ascii=False))
