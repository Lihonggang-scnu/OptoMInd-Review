"""Use formal raw-response cache recovery after an address-only parser fix.

No provider is instantiated. The old response is copied unchanged only after
the actual initial messages and wire request have been verified identical.
"""
from pathlib import Path
import hashlib, json, shutil, subprocess, sys

ROOT=Path(__file__).resolve().parent
WORKTREE=ROOT/'worktree'
sys.path.insert(0,str(WORKTREE))
from scripts.upgrade3 import guide_maker
from optomind_research.runtime.upgrade3.writer_candidates import _hash
from optomind_research.runtime.upgrade3.guide_maker_contracts import compile_guide_input,parse_maker_response
from scripts.upgrade3.evidence_body_writer import load_body_manifest

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf-8'))

if __name__=='__main__':
    dst=ROOT/'alias_recovered_live'
    assert not dst.exists(),'new recovery directory required'
    argv=['--manifest',r'F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json',
          '--feedback',str(ROOT/'message_preview/USER_FEEDBACK.txt'),'--config',str(WORKTREE/'config/guide_maker/explicit_max.json'),
          '--allow-max','--tokenizer',r'F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json','--output',str(dst)]
    assert guide_maker.main(argv)==0
    preview=next(dst.glob('runs/*/plans/maker_001/REQUEST_PREVIEW.json'))
    p=read(preview)
    messages=read(preview.parent/'MESSAGES.json')
    original=next((ROOT/'cold_start_live/stages/maker_001').glob('*/attempt_001'))
    oldmessages=read(original/'MESSAGES.json')
    oldrequest=read(original/'REQUEST.json')
    assert messages==oldmessages
    assert p['estimate']['wire_request_sha256']==oldrequest['estimate']['wire_request_sha256']
    assert p['effective_profile']==oldrequest['effective_profile']
    signature={**p['signature'],'execution_mode':'live','messages':messages}
    key=_hash(signature)
    source=read(original/'RAW_RESPONSE.json')
    manifest=argv[1]
    book,_=load_body_manifest(manifest)
    bundle=compile_guide_input(book,feedback=(ROOT/'message_preview/USER_FEEDBACK.txt').read_text(encoding='utf-8'))
    parsed=parse_maker_response(source,book,bundle)
    assert not parsed['errors'] and parsed['transport_complete']
    assert parsed['guide']==read(ROOT/'cold_start_live/DRAFT_GUIDE.json')
    target=dst/'stages/maker_001'/key/'attempt_001'
    target.mkdir(parents=True,exist_ok=False)
    for name in ('RAW_RESPONSE.json','USAGE.json','MESSAGES.json'):
        shutil.copy2(original/name,target/name)
        assert sha(original/name)==sha(target/name)
    rawobject=json.loads(source['content'])
    receipt={'source_attempt':str(original),'source_original_commit':'742bef4a9134da9cbccc4e407ee7a2e36d5eab5f',
             'recovery_source_commit':subprocess.check_output(['git','-C',str(WORKTREE),'rev-parse','HEAD'],text=True).strip(),
             'source_response_sha256':sha(original/'RAW_RESPONSE.json'),'target_response_sha256':sha(target/'RAW_RESPONSE.json'),
             'actual_initial_messages_unchanged':True,'wire_request_unchanged':True,'scientific_guide_unchanged':True,
             'original_reading_needs':rawobject['reading_needs'],'normalized_reading_needs':parsed['reading_needs'],
             'first_paid_request_repeated':False,'provider_calls_by_seed':0,
             'original_stage_cost_cny':read(original/'USAGE.json')['estimated_actual_cost_cny'],
             'native_recovery':'_execute_stage reparses saved RAW_RESPONSE with no RESULT; cached first stage incurs no provider call'}
    (target/'RECOVERED_FROM.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'ALIAS_RECOVERY_RECEIPT.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Saved raw response seeded unchanged; no provider call')
