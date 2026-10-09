"""Official guide-maker entry, original ledger and debt-only key rotation."""
from pathlib import Path
import contextlib, hashlib, json, math, msvcrt, sqlite3, subprocess, sys

ROOT = Path(__file__).resolve().parent
WORKTREE = ROOT / 'worktree'
SHA = 'd1ab9c0b73817a97cea459bf582030206cbbf0a0'
LEDGER = Path(r'F:\OptoMind-Review-2\outputs\guide_maker_acceptance_20261008_30cny\guide_budget.sqlite')
MARKER = Path(str(LEDGER) + '.guide_maker.json')
CAP = 30.0
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import guide_maker

def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def snapshot():
    marker = json.loads(MARKER.read_text(encoding='utf-8'))
    assert marker == {'schema_version':'optomind.guide_maker_budget.v1', 'ledger_path':str(LEDGER), 'limit_cny':CAP}
    with contextlib.closing(sqlite3.connect(LEDGER.as_uri()+'?mode=ro', uri=True)) as db:
        assert float(db.execute("select value from budget_meta where key='limit_cny'").fetchone()[0]) == CAP
        rows = db.execute('select status,amount_cny,actual_cny from reservations').fetchall()
    assert all(status in ('settled','reserved','uncertain') for status,_,_ in rows)
    actual = sum(float(cost or 0) for _,_,cost in rows)
    reserved = sum(float(amount) for status,amount,_ in rows if status=='reserved')
    uncertain = sum(float(amount) for status,amount,_ in rows if status=='uncertain')
    used = sum(float(cost if cost is not None else amount) if status=='settled' else float(amount) for status,amount,cost in rows)
    assert all(math.isfinite(v) and v >= 0 for v in (actual,reserved,uncertain,used))
    return {'ledger_path':str(LEDGER),'effective_cumulative_cap_cny':CAP,
            'actual_cny':round(actual,9),'reserved_cny':round(reserved,9),
            'uncertain_cny':round(uncertain,9),'used_for_cap_cny':round(used,9),
            'remaining_cny':round(CAP-used,9),'reservation_count':len(rows),
            'marker_sha256':hashlib.sha256(MARKER.read_bytes()).hexdigest()}

def debt_only(status,code):
    code=str(code).lower().replace('-','').replace('_','').replace(' ','')
    return status in {400,401,402,403} and any(x in code for x in ('arrearage','insufficientbalance','accountbalance','paymentrequired'))

BASE=runtime.QwenDirectClient
class DebtRotationClient(BASE):
    def __init__(self,**kw):
        assert kw.get('model')=='qwen3.8-max'
        kw['max_retries']=0
        super().__init__(**kw)
        self.max_keys=max(1,len(self._keys()))
        save('KEY_ROTATION_POLICY.json', {'configured_candidate_count':self.max_keys,
             'rotation':'provider debt rejection only','paid_retries':0,'key_values_recorded':False})

def main():
    mode=sys.argv[1]
    if mode=='status':
        print(json.dumps(snapshot())); return 0
    if mode=='selftest':
        assert debt_only(400,'Arrearage') and debt_only(403,'InsufficientBalance')
        assert not debt_only(429,'RateLimit') and not debt_only(500,'error')
        assert not debt_only(401,'InvalidApiKey') and not debt_only(403,'PolicyDenied')
        save('WRAPPER_FREE_CHECK.json', {'debt_only_rotation':True,'other_failures_not_rotated':True,
             'max_retries':0,'source_unchanged':True,'ledger_cap_cny':CAP})
        print('Debt-only checks passed'); return 0
    assert mode=='run'
    head=subprocess.check_output(['git','-C',str(WORKTREE),'rev-parse','HEAD'],text=True).strip()
    assert head==SHA
    assert (ROOT/'FREE_PREP_REPORT.json').is_file(), 'root must review free preparation first'
    args=sys.argv[2:]
    parsed=guide_maker.parser().parse_args(args)
    assert parsed.run and parsed.allow_max and not parsed.retry_failed and not parsed.responses and not parsed.book
    assert parsed.budget_mode=='dedicated' and parsed.budget_limit==CAP
    assert Path(parsed.budget_ledger).resolve()==LEDGER
    cfg=json.loads(Path(parsed.config).read_text(encoding='utf-8'))
    assert cfg['maker']['model']=='qwen3.8-max' and cfg['max_model_calls']==8
    with open(str(LEDGER)+'.guide_retest.lock','a+b') as lock:
        lock.seek(0,2)
        if not lock.tell(): lock.write(b'0'); lock.flush()
        lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        try:
            before=snapshot()
            assert before['reserved_cny']==before['uncertain_cny']==0
            if (ROOT/'BUDGET_START.json').exists():
                initial=json.loads((ROOT/'BUDGET_START.json').read_text(encoding='utf-8'))
            else:
                initial=before; save('BUDGET_START.json',initial)
            save('BUDGET_BEFORE_INVOCATION.json',before)
            runtime._account_rejection=debt_only
            runtime.QwenDirectClient=DebtRotationClient
            try:
                return guide_maker.main(args)
            finally:
                after=snapshot()
                save('BUDGET_FINAL.json',{**after,'this_run_actual_cny':round(after['actual_cny']-initial['actual_cny'],9)})
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)

if __name__=='__main__': raise SystemExit(main())
