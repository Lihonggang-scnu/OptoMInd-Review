import sys,json,subprocess,hashlib,datetime,types,importlib.machinery
from pathlib import Path
from zoneinfo import ZoneInfo
R=Path(r'R');N=R/'outputs/e2e_metasurface_imaging_20261016';S=N/'worktree';O=Path(__file__).resolve().parents[1]
def git(root,*args):
 p=subprocess.run(['git','--no-optional-locks','-C',str(root),*args],text=True,capture_output=True)
 return {'exit_code':p.returncode,'stdout':p.stdout.strip(),'stderr':p.stderr.strip()}
repos={}
for label,root in [('development',R),('execution',S)]:
 repos[label]={'path':str(root),'top':git(root,'rev-parse','--show-toplevel'),'head':git(root,'rev-parse','HEAD'),'branch':git(root,'symbolic-ref','--quiet','--short','HEAD'),'git_common_dir':git(root,'rev-parse','--git-common-dir'),'status':git(root,'-c','core.quotePath=false','status','--porcelain','--untracked-files=normal'),'relevant_diff':git(root,'diff','--name-status','--','optomind_research/runtime/upgrade3','scripts/upgrade3','optomind_research/runtime/publication_integrity.py','optomind_research/runtime/latex_publication_renderer.py')}
mods=['optomind_research','optomind_research.runtime','optomind_research.runtime.upgrade3','optomind_research.runtime.upgrade3.review_unit_writer','scripts','scripts.upgrade3','scripts.upgrade3.writer_candidates']
resolution={}
for label,root in [('development',R),('execution',S)]:
 res={};parts={}
 for m in mods:
  parent=m.rpartition('.')[0];search=[str(root)] if not parent else parts.get(parent)
  # Empty in-memory parent stubs allow namespace resolution without executing
  # package __init__ files (runtime.__init__ imports application code).
  if parent:
   stub=types.ModuleType(parent);stub.__path__=search;sys.modules[parent]=stub
  spec=importlib.machinery.PathFinder.find_spec(m,search) if search is not None else None
  parts[m]=list(spec.submodule_search_locations) if spec and spec.submodule_search_locations is not None else None
  res[m]={'origin':spec.origin if spec else None,'package_locations':parts[m]}
 resolution[label]=res
paths=[]
for root in [R,S]:
 for area in ['scripts/upgrade3','optomind_research/runtime/upgrade3']:
  paths.extend(p for p in (root/area).rglob('*.py') if '__pycache__' not in p.parts)
 for rel in ['optomind_research/runtime/publication_integrity.py','optomind_research/runtime/latex_publication_renderer.py','optomind_research/runtime/publication_mainline_adapter.py']:
  if (root/rel).exists():paths.append(root/rel)
fingerprints={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
base={'observed_at':datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'repositories':repos,'merge_base':git(R,'merge-base','HEAD',repos['execution']['head']['stdout']),'runtime':{'executable':sys.executable,'version':sys.version,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'dont_write_bytecode':sys.dont_write_bytecode,'module_resolution_without_import':resolution,'resolution_method':'PathFinder.find_spec with explicit repository roots and inert in-memory parent stubs; no application package initializer or application entrypoint executed. Origins are resolved filesystem paths, not a dynamic-import or current-running-process claim.'},'source_fingerprints':fingerprints}
target=O/'analysis/BASELINE_LOCAL.json'
assert not target.exists(),'Initial baseline must not be overwritten'
target.write_text(json.dumps(base,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'file':'analysis/BASELINE_LOCAL.json','heads':{k:v['head']['stdout'] for k,v in repos.items()},'merge_base_exit':base['merge_base']['exit_code'],'merge_base':base['merge_base']['stdout'],'source_file_count':len(fingerprints),'python_version':sys.version.split()[0],'module_origins':{k:v['optomind_research.runtime.upgrade3.review_unit_writer']['origin'] for k,v in resolution.items()}},ensure_ascii=False))
