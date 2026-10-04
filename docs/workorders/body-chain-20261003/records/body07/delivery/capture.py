"""Real small-batch assembly/delivery, synthetic inputs, no network or model.
Run from repository root. Baseline imported from the locked git commit.
"""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile

ROOT = Path.cwd()
HERE = ROOT/'docs/workorders/body-chain-20261003/records/body07/delivery'

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def deny(*a,**kw): raise AssertionError('WO07 capture forbids network')
socket.socket.connect=deny; socket.create_connection=deny
fixture=load('body07_delivery_fixture',ROOT/'tests/upgrade3/test_body07_delivery_pending_gate.py')
from optomind_research.runtime.upgrade3 import review_delivery as current
with tempfile.TemporaryDirectory(prefix='body07-delivery-') as temp:
    base=Path(temp)
    old_path=base/'old_delivery.py'
    old_path.write_bytes(subprocess.check_output(['git','show','841e9143972c408a3b0f42f04e955efa2137c9b4:optomind_research/runtime/upgrade3/review_delivery.py']))
    old=load('optomind_research.runtime.upgrade3.body07_old_delivery',old_path)
    for label,module in [('BEFORE',old),('AFTER',current)]:
        evidence={'fixture':'synthetic one-unit batch; production history assembly and downstream entry', 'model_calls':0,'network_calls':0,'cases':{}}
        for name,problem in [('missing_table','table'),('truncated','length'),('normal','')]:
            root=base/label/name
            manifest,config,original,body=fixture.batch(root,problem)
            raw=original.read_bytes()
            result=module.run_review_delivery(start='history',out_dir=root/'delivery',manifest_path=manifest,batch_root=root,config_path=config)
            files={str(p.relative_to(root)):p.read_text(encoding='utf-8') for p in root.rglob('*') if p.is_file()}
            evidence['cases'][name]={'actual_report':result,'original_bytes_unchanged':raw==original.read_bytes(),'body_retained':body in (root/'delivery/assembled/REVIEW_DRAFT_HANDLES.md').read_text(), 'files':files}
        text=json.dumps(evidence,ensure_ascii=False,indent=2).replace(str(base),'$TEMP')
        (HERE/(label+'.json')).write_text(text+'\n',encoding='utf-8')
