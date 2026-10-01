from pathlib import Path
import json, hashlib, importlib.util, shutil
BASE=Path(r'F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001')
S=Path(r'F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930')
A=BASE/'worktree/archives/manuscript-parts-local-test-20261001'
def sha(b): return hashlib.sha256(b).hexdigest()
def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def save(p,x): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
spec=importlib.util.spec_from_file_location('builder',BASE/'build_archive.py'); builder=importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
quotes=set()
def collect(x,path=()):
    if isinstance(x,dict):
        for k,v in x.items():
            if k=='text' and 'local_passages' in path and isinstance(v,str) and len(v)>400: quotes.add(v)
            collect(v,path+(k,))
    elif isinstance(x,list):
        for v in x: collect(v,path)
for p in [S/'new_plan/DETAILED_REVIEW_PLAN.json',S/'body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json',Path(r'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\DETAILED_REVIEW_PLAN.json')]: collect(load(p))
variants=sorted({v for q in quotes for v in (q,json.dumps(q,ensure_ascii=False)[1:-1])},key=len,reverse=True)
redactions=[]
def clean(x,rel,path=()):
    if isinstance(x,dict): return {k:clean(v,rel,path+(str(k),)) for k,v in x.items()}
    if isinstance(x,list): return [clean(v,rel,path+(str(i),)) for i,v in enumerate(x)]
    if not isinstance(x,str): return x
    for q in variants:
        anchor=q[:200]
        while anchor in x:
            start=x.find(anchor); n=200
            while n<len(q) and start+n<len(x) and q[n]==x[start+n]: n+=1
            old=x[start:start+n]
            redactions.append({'relative_path':rel,'json_path':list(path),'removed_chars':n,'removed_sha256':sha(old.encode()),'reason':'known_original_publisher_passage'})
            x=x[:start]+'[REDACTED_ORIGINAL_PASSAGE; DOI/source identity retained elsewhere]'+x[start+n:]
    return x
m=load(A/'manifest.json'); entries={e['relative_path']:e for e in m['files']}
def register(dest,source,purpose,transform):
    rel=dest.relative_to(A).as_posix(); raw=source.read_bytes()
    entries[rel]={'relative_path':rel,'purpose':purpose,'run':'body_restore_20261001','original_path':str(source),'original_sha256':sha(raw),'original_bytes':len(raw),'transform':transform}
p=S/'body_restore_20261001/parts/MANUSCRIPT_FINAL.md'; d=A/'raw_public/body_restore_20261001/parts/MANUSCRIPT_FINAL.md'; d.write_text(builder.scrub_text(p.read_text(encoding='utf-8-sig')),encoding='utf8'); register(d,p,'first restored real complete manuscript','public text sanitation')
for p in (S/'body_restore_20261001/body/arrangement').glob('*/CHAPTER_ARRANGEMENT.json'):
    d=A/'raw_public'/p.relative_to(S); save(d,builder.redact(load(p))); register(d,p,'actual validated arrangement with source catalog','public original-text field sanitation')
for p in list(A.rglob('*')):
    if not p.is_file() or p.name=='manifest.json': continue
    rel=p.relative_to(A).as_posix(); text=p.read_text(encoding='utf-8-sig',errors='replace')
    if not any(q[:200] in text for q in variants): continue
    if p.suffix=='.json': save(p,clean(json.loads(text),rel))
    elif p.suffix=='.jsonl': p.write_text('\n'.join(json.dumps(clean(json.loads(line),rel,(str(i),)),ensure_ascii=False,separators=(',',':')) for i,line in enumerate(text.splitlines()) if line.strip())+'\n',encoding='utf8')
    else: p.write_text(clean(text,rel),encoding='utf8')
    entries[rel]['transform']=entries[rel].get('transform','')+'; selective known publisher passage removal'
    entries[rel]['redaction']=True
save(A/'curated/SELECTIVE_PASSAGE_REDACTIONS.json',{'scope':'public copies only; originals unchanged','events':redactions})
idxp=A/'curated/materials/AB_DEEP_SUMMARIES.index.json'; idx=load(idxp)
for e in idx['chunks']:
    p=A/e['path']; e['sha256']=sha(p.read_bytes()); e['bytes']=p.stat().st_size
idx['public_redaction_note']='Record keys retain historical material identity; public content can omit publisher-origin quotations. Keys are not hashes of the final redacted record.'
save(idxp,idx)
tool=A/'curated/archive_tooling'; tool.mkdir(parents=True,exist_ok=True)
for name in ['build_archive.py','audit_archive.py','finalize_archive.py']: shutil.copyfile(BASE/name,tool/name)
readme=A/'README.md'
t=readme.read_text(encoding='utf8')
t+='\n## 云端开发材料导航\n\n- `curated/materials/PLAN_SECTIONS_INDEX.json` 列出全部拆分规划；`PUBLIC_PLAN_OUTLINE.md` 提供便于阅读的规划概览。\n- `curated/materials/AB_DEEP_SUMMARIES.index.json` 指向8份去重后的 A/B、精读及补充提炼；规划中的 `_archive_material_ref` 对应这些记录键。\n- `raw_public/body_restore_20261001/body/arrangement/` 保留5章实际编排输入、响应和导出结果。\n- `raw_public/body_restore_20261001/body_plus/writer/` 保留20个真实单元的输入、messages、响应、结果及正文。\n- 两轮 `parts/` 与 `parts_tuned/` 均附3组实际消息/响应和完整稿。\n- `raw_public/all_qwen_raw/` 追加已落盘的去重模型响应；它们属于历史调用，不是新增运行。\n- `curated/materials/source_index.json` 说明原材料去向；`curated/SELECTIVE_PASSAGE_REDACTIONS.json` 记录公开副本中残留的原文片段删节。\n- `curated/archive_tooling/` 是本次离线归档工具，不是生产算法。\n\n为尽量支持云端修改，实际消息、模型提炼与响应尽可能保留；大规划按结构拆分并抽出重复材料。材料原文仅删节，不以删节版冒充原调用完整复现。\n'
readme.write_text(t,encoding='utf8')
def refresh():
    out=[]
    for p in sorted(A.rglob('*')):
        if not p.is_file() or p.name=='manifest.json' and p.parent==A: continue
        rel=p.relative_to(A).as_posix(); e=entries.get(rel,{'relative_path':rel,'purpose':'archive-generated documentation or verification tooling','run':'archive preparation','original_path':None,'transform':'generated offline'})
        e['public_sha256']=sha(p.read_bytes()); e['public_bytes']=p.stat().st_size
        if e.get('original_sha256')==e['public_sha256']: e['redaction']=False; e['transform']='byte-identical original'
        out.append(e)
    m['files']=out; m['manifest_self_hash']='excluded to avoid self-reference'; m['file_count']=len(out); m['public_bytes_excluding_manifest']=sum(e['public_bytes'] for e in out); save(A/'manifest.json',m)
refresh()
print(json.dumps({'files':len(m['files']),'bytes':m['public_bytes_excluding_manifest'],'selective_redactions':len(redactions)},ensure_ascii=False))
