from pathlib import Path
import hashlib,json,shutil,subprocess
B=Path(r'F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001')
A=B/'worktree/archives/manuscript-parts-local-test-20261001'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p):return json.loads(p.read_text(encoding='utf8'))
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
pool={}
for p in (A/'curated/materials').glob('AB_DEEP_SUMMARIES.[0-9]*.json'):pool.update(load(p)['records'])
refs=set();errors=[]
def walk(x):
    if isinstance(x,dict):
        for row in x.get('material_refs',[]) if isinstance(x.get('material_refs'),list) else []:
            if isinstance(row,dict) and isinstance(row.get('record_sha256'),str):refs.add(row['record_sha256'])
        fragment=x.get('_archive_fragment')
        if isinstance(fragment,str):assert (A/fragment).is_file(),fragment
        for v in x.values():walk(v)
    elif isinstance(x,list):
        for v in x:walk(v)
for p in A.rglob('*.json'):
    if p.name=='manifest.json':continue
    walk(load(p))
missing=sorted(refs-set(pool));errors+=missing
idxp=A/'curated/materials/PLAN_SECTIONS_INDEX.json';idx=load(idxp)
for e in idx['sections']:
    p=A/e['path'];assert p.exists(),e['path'];e['bytes']=p.stat().st_size;e['public_sha256']=sha(p)
save(idxp,idx)
result={'schema':'archive.material-references-check.v1','pool_records':len(pool),'referenced_materials':len(refs),'missing_references':missing,'pass':not errors}
save(A/'curated/MATERIAL_REFERENCE_CHECK.json',result)
shutil.copyfile(B/'verify_and_seal.py',A/'curated/archive_tooling/verify_and_seal.py')
shutil.copyfile(B/'ROOT_AUDIT_RESULT.json',A/'curated/ROOT_AUDIT_RESULT.json')
m=load(A/'manifest.json');old={e['relative_path']:e for e in m['files']};rows=[]
for p in sorted(A.rglob('*')):
    if not p.is_file() or p==A/'manifest.json':continue
    rel=p.relative_to(A).as_posix();e=old.get(rel,{'relative_path':rel,'purpose':'root offline archive verification','run':'archive preparation','original_path':None,'transform':'generated offline'})
    e.update(public_sha256=sha(p),public_bytes=p.stat().st_size);rows.append(e)
m.update(files=rows,file_count=len(rows),public_bytes_excluding_manifest=sum(e['public_bytes'] for e in rows));save(A/'manifest.json',m)
print(json.dumps(result));assert not errors
