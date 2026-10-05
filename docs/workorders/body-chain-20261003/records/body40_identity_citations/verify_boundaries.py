import ast,hashlib,json,pathlib,subprocess
root=pathlib.Path.cwd();base='e0615b0f83ed001042b12c539a08a316f3ef6ab6';out=root/'docs/workorders/body-chain-20261003/records/body40_identity_citations'
files=['optomind_research/runtime/upgrade3/planning_material_triage.py','optomind_research/runtime/upgrade3/progressive_review_plan.py','optomind_research/runtime/upgrade3/review_unit_writer.py','scripts/upgrade3/full_review_draft.py','scripts/upgrade3/review_unit_writer.py']
checks=[]
for file,names in [(files[1],['run','_post_case_review','_cache_contract']), (files[2],['unit_payload','unit_messages','build_completion_payload','completion_messages'])]:
 before=ast.parse(subprocess.check_output(['git','show',f'{base}:{file}'],text=True));after=ast.parse((root/file).read_text())
 for name in names:
  a=[ast.dump(n,include_attributes=False) for n in ast.walk(before) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name];b=[ast.dump(n,include_attributes=False) for n in ast.walk(after) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name]
  checks.append({'path':file,'function':name,'found':bool(a),'unchanged':a==b});assert a and a==b,(file,name)
for file in files[:3]:
 a=ast.parse(subprocess.check_output(['git','show',f'{base}:{file}'],text=True));b=ast.parse((root/file).read_text())
 def prompts(tree):return {n.targets[0].id:ast.dump(n.value,include_attributes=False) for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and any(s in n.targets[0].id for s in ['PROMPT','INSTRUCTIONS'])}
 checks.append({'path':file,'prompt_constants_unchanged':prompts(a)==prompts(b)});assert prompts(a)==prompts(b)
assert not subprocess.check_output(['git','diff','--name-only',base,'--','prompts','docs/acceptance/body40-20261005'])
(out/'SOURCE_AND_BOUNDARY_CHECKS.json').write_text(json.dumps({'base_sha':base,'tested_source_sha':'7e293b63603157e741ec9b68f30565fcb4065f8a','production_files':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files},'checks':checks,'archive_and_prompt_files_unchanged':True},indent=2)+'\n')
print('AST prompt/order/message builders unchanged; archive unchanged; five production hashes recorded')
