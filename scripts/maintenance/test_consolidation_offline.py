#!/usr/bin/env python3
"""Offline synthetic cleanup-script audit. No production repository is opened.

Usage: python cleanup_synthetic_tests.py [path/to/consolidate_remote_branches.py]
All remotes live in a new temporary directory; Git permits file transport only.
The production module is imported, then REPO, ROOT and ARCHIVE are replaced.
Checks include pushurl and URL-rewrite destination safety regressions.
The output JSON contains individual checks and the inspected source SHA-256.
"""
import contextlib, hashlib, importlib.util, io, json, os, pathlib, subprocess, sys, tempfile
SCRIPT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).with_name('consolidate_remote_branches.py')
BASE = pathlib.Path(tempfile.mkdtemp(prefix='cleanup-synthetic-'))
os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Synthetic Test', GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_NAME='Synthetic Test', GIT_COMMITTER_EMAIL='test@example.invalid', GIT_ALLOW_PROTOCOL='file')
for key in list(os.environ):
    if key.startswith('GIT_CONFIG_KEY_') or key.startswith('GIT_CONFIG_VALUE_') or key in {'GIT_CONFIG_COUNT','GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE'}:
        os.environ.pop(key)
spec=importlib.util.spec_from_file_location('cleanup_under_test', SCRIPT)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
realgit=m.git
results=[]

def raw(repo,*args,input=None):
    p=subprocess.run(['git',*args],cwd=repo,input=input,text=True,capture_output=True)
    if p.returncode: raise RuntimeError(p.stderr or p.stdout)
    return p.stdout.strip()

def setup(name):
    base=BASE/name; base.mkdir(); repo=base/'remote.git'
    raw(base,'init','--bare',str(repo)); raw(repo,'symbolic-ref','HEAD','refs/heads/main')
    tree=raw(repo,'hash-object','-t','tree','-w','--stdin',input='')
    def commit(label, parents=()):
        args=['commit-tree',tree,'-m',label]
        for p in parents: args += ['-p',p]
        return raw(repo,*args)
    main=commit('main'); olda=commit('old a',[main]); oldb=commit('old b',[main]); active=commit('active',[main]); archive=commit('archive',[olda,oldb]); newer=commit('new unarchived',[active])
    refs={'main':main,'lihonggang-dev':active,'archive/history':archive,'old/a':olda,'old/b':oldb}
    for ref,sha in refs.items(): raw(repo,'update-ref','refs/heads/'+ref,sha)
    raw(repo,'update-ref','refs/tags/keep-tag',olda)
    root=base/'project'; (root/'docs/current').mkdir(parents=True)
    raw(root,'init')
    manifest={'main_unchanged':main,'branches':[{'branch':n,'commit':refs[n]} for n in ['main','old/a','old/b']]}
    (root/'docs/current/HISTORICAL_BRANCHES.json').write_text(json.dumps(manifest))
    m.REPO=str(repo); m.ROOT=root; m.ARCHIVE=archive; m.git=realgit
    return base,repo,refs,newer

def run(base, active, apply=False, backup='backup'):
    sys.argv=[str(SCRIPT),'--active-commit',active,'--backup-dir',str(base/backup)]+(['--apply'] if apply else [])
    out=io.StringIO()
    try:
        with contextlib.redirect_stdout(out): m.main()
        return None,out.getvalue()
    except Exception as e: return str(e),out.getvalue()

def record(name, passed, details):
    results.append({'test':name,'passed':passed,'details':details})
    print(json.dumps(results[-1]),flush=True)

b,r,refs,new=setup('dry_run')
e,o=run(b,refs['lihonggang-dev'])
rec=json.loads((b/'backup/cleanup-receipt.json').read_text())
record('dry_run_preserves_all_refs',e is None and m.remote_heads()==refs and not rec['deleted'], {'error':e,'output':o.strip()})
record('bundle_hash_verified',rec['backup_sha256']==hashlib.sha256((b/'backup/before-cleanup.bundle').read_bytes()).hexdigest() and rec['backup_verified'], {})

b,r,refs,new=setup('apply')
e,o=run(b,refs['lihonggang-dev'],True)
kept={n:s for n,s in refs.items() if n in m.KEEP}
record('apply_deletes_only_old_refs',e is None and m.remote_heads()==kept and raw(r,'rev-parse','refs/tags/keep-tag')==refs['old/a'], {'error':e,'output':o.strip()})
record('archive_retains_original_tips',all(subprocess.run(['git','merge-base','--is-ancestor',sha,m.ARCHIVE],cwd=r).returncode==0 for sha in [refs['main'],refs['old/a'],refs['old/b']]), {})
e,o=run(b,refs['lihonggang-dev'],True,'backup2')
rec=json.loads((b/'backup2/cleanup-receipt.json').read_text())
record('idempotent_fresh_backup',e is None and m.remote_heads()==kept and rec['deleted']==[], {'error':e,'output':o.strip()})
e,o=run(b,refs['lihonggang-dev'],True,'backup2')
record('existing_backup_refused',e is not None and m.remote_heads()==kept, {'error':e})

for name,ref in [('moved_main','main'),('moved_active','lihonggang-dev'),('moved_archive','archive/history'),('moved_old','old/a'),('new_branch','unknown')]:
    b,r,refs,new=setup(name)
    raw(r,'update-ref','refs/heads/'+ref,new)
    before=m.remote_heads(); e,o=run(b,refs['lihonggang-dev'],True)
    record('reject_'+name,e is not None and m.remote_heads()==before, {'error':e})

b,r,refs,new=setup('not_ancestor')
m.ARCHIVE=refs['main']; raw(r,'update-ref','refs/heads/archive/history',m.ARCHIVE)
before=m.remote_heads(); e,o=run(b,refs['lihonggang-dev'],True)
record('reject_unarchived_tip',e is not None and m.remote_heads()==before, {'error':e})

b,r,refs,new=setup('atomic_rejection')
hook=r/'hooks/update'; hook.write_text('#!/bin/sh\n[ "$1" != "refs/heads/old/b" ]\n'); hook.chmod(0o755)
e,o=run(b,refs['lihonggang-dev'],True)
record('atomic_server_rejection_changes_nothing',e is not None and m.remote_heads()==refs, {'error':e})

for name,ref in [('old_race','old/a'),('main_race','main'),('archive_race','archive/history'),('new_race','unknown')]:
    b,r,refs,new=setup(name)
    def racegit(*args,cwd=None):
        if 'push' in args:
            raw(r,'update-ref','refs/heads/'+ref,new)
        return realgit(*args,cwd=cwd)
    m.git=racegit
    e,o=run(b,refs['lihonggang-dev'],True)
    after=m.remote_heads(); surviving_old=[n for n in ('old/a','old/b') if n in after]
    expected_safe=(ref=='old/a' and surviving_old==['old/a','old/b']) or (ref!='old/a' and surviving_old==[])
    record('race_'+name, bool(e) and expected_safe, {'error':e,'surviving_old_refs':surviving_old,'output':o.strip()})

# Destination-safety regression: inherited origin pushurl must be bypassed.
b,r,refs,new=setup('global_pushurl')
other=b/'unrelated-remote.git'
raw(b,'clone','--mirror',str(r),str(other))
config=b/'test-gitconfig'
config.write_text('[remote "origin"]\n\tpushurl = '+str(other)+'\n')
os.environ['GIT_CONFIG_GLOBAL']=str(config)
e,o=run(b,refs['lihonggang-dev'],True)
actual={ref.removeprefix('refs/heads/'):sha for sha,ref in (line.split() for line in raw(other,'show-ref','--heads').splitlines())}
record('destination_pushurl_regression', e is None and len(actual)==5 and len(m.remote_heads())==3,
       {'error':e, 'intended_remote_branch_count':len(m.remote_heads()),
        'other_remote_branch_count':len(actual), 'expected':'Only the intended remote loses old refs; the other remote must be unchanged'})
os.environ['GIT_CONFIG_GLOBAL']='/dev/null'

# URL rewrites must be rejected before any remote inspection, backup or delete.
for rule in ('pushInsteadOf', 'insteadOf'):
    b,r,refs,new=setup('rewrite_'+rule)
    other=b/'unrelated-remote.git'
    raw(b,'clone','--mirror',str(r),str(other))
    config=b/'test-gitconfig'
    config.write_text('[url "'+str(other)+'"]\n\t'+rule+' = '+str(r)+'\n')
    os.environ['GIT_CONFIG_GLOBAL']=str(config)
    e,o=run(b,refs['lihonggang-dev'],True)
    # Inspect by direct local Git cwd, avoiding the deliberately rewritten URL.
    original_count=len(raw(r,'show-ref','--heads').splitlines())
    other_count=len(raw(other,'show-ref','--heads').splitlines())
    record('reject_'+rule+'_destination',
           e == 'repository URL rewrite detected; no refs changed'
           and original_count==5 and other_count==5 and not (b/'backup').exists(),
           {'error':e,'intended_remote_branch_count':original_count,
            'other_remote_branch_count':other_count,'backup_created':(b/'backup').exists()})
    os.environ['GIT_CONFIG_GLOBAL']='/dev/null'

(ROOT_REPORT:=BASE/'test-results.json').write_text(json.dumps({'script_sha256':hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),'results':results},indent=2))
print('REPORT='+str(ROOT_REPORT))
assert all(x['passed'] for x in results)
