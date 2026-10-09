"""Official once-review CLI, with user-authorized debt-only key rotation."""
from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'worktree'))
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import guide_review
BASE=runtime.QwenDirectClient

def debt_only(status,code):
    code=str(code).lower().replace('-','').replace('_','').replace(' ','')
    return status in {400,401,402,403} and any(x in code for x in ('arrearage','insufficientbalance','accountbalance','paymentrequired'))

class DebtRotationClient(BASE):
    def __init__(self,**kw):
        kw['max_retries']=0
        super().__init__(**kw)
        self.max_keys=max(1,len(self._keys()))
        with (ROOT/'KEY_ROTATION_POLICY.json').open('w',encoding='utf-8') as f:
            json.dump({'configured_candidate_count':self.max_keys,'rotation':'provider debt rejection only','paid_retries':0,'key_values_recorded':False},f,indent=2)

if __name__=='__main__':
    if sys.argv[1:]==['selftest']:
        assert debt_only(400,'Arrearage') and debt_only(403,'InsufficientBalance')
        assert not debt_only(429,'RateLimit') and not debt_only(500,'error')
        assert not debt_only(401,'InvalidApiKey') and not debt_only(403,'PolicyDenied')
        (ROOT/'WRAPPER_FREE_CHECK.json').write_text(json.dumps({'debt_only_rotation':True,'other_failures_not_rotated':True,'max_retries':0,'production_source_modified':False}),encoding='utf-8')
        print('Debt-only wrapper checks passed')
    else:
        runtime._account_rejection=debt_only
        runtime.QwenDirectClient=DebtRotationClient
        raise SystemExit(guide_review.main(sys.argv[1:]))
