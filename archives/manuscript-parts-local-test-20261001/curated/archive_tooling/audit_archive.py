from pathlib import Path
import json,re,hashlib,subprocess,collections
SOURCE=Path(r"F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930")
ROOT=Path(r"F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001\worktree")
A=ROOT/"archives/manuscript-parts-local-test-20261001"
files=[p for p in A.rglob("*") if p.is_file()]
manifest=json.loads((A/"manifest.json").read_text(encoding="utf-8-sig"))
entries={x["relative_path"]:x for x in manifest["files"]}
errors=[]
for p in files:
 rel=p.relative_to(A).as_posix()
 if rel=="manifest.json":continue
 e=entries.get(rel)
 if not e:errors.append({"kind":"missing_manifest_entry","path":rel});continue
 if hashlib.sha256(p.read_bytes()).hexdigest()!=e.get("public_sha256"):errors.append({"kind":"hash_mismatch","path":rel})
 if p.stat().st_size>5000000:errors.append({"kind":"large_file","path":rel,"bytes":p.stat().st_size})
for rel in entries:
 if not (A/rel).is_file():errors.append({"kind":"manifest_missing_file","path":rel})
patterns={"credential_token":re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"),"private_key":re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),"literal_bearer":re.compile(r"(?i)Bearer\s+[A-Za-z0-9._-]{24,}")}
urlpattern=re.compile(r"https?://[^\s\"<>]+[?&](?:api_key|apikey|access_token|token|sig|signature|X-Amz-Signature|X-Amz-Credential)=[^\s\"<>]+",re.I)
quotes=set()
def collect(x,path=()):
 if isinstance(x,dict):
  for k,v in x.items():
   if k=="text" and "local_passages" in path and isinstance(v,str) and len(v)>400:quotes.add(v[:200])
   collect(v,path+(k,))
 elif isinstance(x,list):
  for v in x:collect(v,path)
for p in [SOURCE/"new_plan/DETAILED_REVIEW_PLAN.json",SOURCE/"body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json",Path(r"F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\DETAILED_REVIEW_PLAN.json")]:
 if p.exists():collect(json.loads(p.read_text(encoding="utf-8-sig")))
anchors=set(q for x in quotes for q in [x,json.dumps(x,ensure_ascii=False)[1:-1]])
for p in files:
 t=p.read_text(encoding="utf-8-sig",errors="replace");rel=p.relative_to(A).as_posix()
 for k,rx in patterns.items():
  if rx.search(t):errors.append({"kind":k,"path":rel})
 for hit in urlpattern.findall(t):
  if "redacted" not in hit.lower():errors.append({"kind":"unredacted_signed_or_credential_url","path":rel})
 n=sum(1 for q in anchors if q in t)
 if n:errors.append({"kind":"known_original_publisher_passage","path":rel,"anchors_found":n})
expected=A/"raw_public/body_restore_20261001/body_plus/writer"
counts={name:len(list(expected.rglob(name))) for name in ["UNIT_INPUT.json","UNIT_MESSAGES.json","UNIT_RESULT.json","UNIT_BODY.md","*.raw"]}
for k,n in counts.items():
 if n!=20:errors.append({"kind":"incomplete_plus_units","artifact":k,"actual":n,"expected":20})
parts={}
for run in ["parts","parts_tuned"]:
 p=A/"raw_public/body_restore_20261001"/run
 parts[run]={"messages":len(list((p/"messages").glob("*.json"))),"raw_responses":len(list((p/"raw_responses").glob("*.raw"))),"manuscript":(p/"MANUSCRIPT_FINAL.md").exists()}
 if parts[run]["messages"]!=3 or parts[run]["raw_responses"]!=3 or not parts[run]["manuscript"]:errors.append({"kind":"incomplete_parts","run":run,"counts":parts[run]})
changed=subprocess.check_output(["git","diff","--name-only","HEAD"],cwd=ROOT,text=True).splitlines()
bad=[p for p in changed if p!="README.md" and not p.startswith(("advisor/","archives/"))]
if bad:errors.append({"kind":"algorithm_change","paths":bad})
report={"schema":"archive.root-audit.v1","file_count":len(files),"total_bytes":sum(p.stat().st_size for p in files),"max_file_bytes":max(p.stat().st_size for p in files),"manifest_entries":len(entries),"plus_counts":counts,"parts_counts":parts,"publisher_passage_anchors_checked":len(anchors),"credential_patterns_checked":list(patterns),"errors":errors,"algorithm_files_changed":bad,"archive_model_calls":0,"pass":not errors}
print(json.dumps(report,ensure_ascii=False,indent=2))
Path(r"F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001\ROOT_AUDIT_RESULT.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
