import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from scripts.upgrade3 import guide_maker as cli
from scripts.upgrade3 import guided_body_writer as writer
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenTransportError

def args(path, cap=30):
    return argparse.Namespace(budget_mode='dedicated',budget_ledger=str(path),budget_limit=cap)

def test_default_legacy_keeps_original_guard():
    assert cli.parser().parse_args(['--manifest','x','--output','y']).budget_mode=='legacy'
    assert cli._ledger_guard is writer._ledger_guard

@pytest.mark.parametrize('cap',[31,float('inf'),0])
def test_dedicated_rejects_unapproved_cap(tmp_path,cap):
    with pytest.raises(ValueError): cli._execution_budget_guard(args(tmp_path/'b.sqlite',cap))
    assert not (tmp_path/'b.sqlite').exists()

def test_30_cap_atomic_concurrent_reservations(tmp_path):
    path=tmp_path/'b.sqlite'; cli._execution_budget_guard(args(path))
    def reserve(i):
        try: return GlobalBudgetLedger(limit_cny=30,path=path).reserve(18,str(i))
        except QwenTransportError: return None
    with ThreadPoolExecutor(max_workers=2) as pool: rows=list(pool.map(reserve,[1,2]))
    assert sum(r is not None for r in rows)==1
    assert cli._execution_budget_guard(args(path))['reserved_cny']==18

def test_uncertain_is_held_and_resume_preserves_spend(tmp_path):
    path=tmp_path/'b.sqlite'; cli._execution_budget_guard(args(path))
    led=GlobalBudgetLedger(limit_cny=30,path=path)
    r=led.reserve(20,'unknown'); led.settle(r['reservation_id'],None,uncertain=True)
    s=led.reserve(5,'done'); led.settle(s['reservation_id'],3)
    snap=cli._execution_budget_guard(args(path))
    assert snap['actual_cny']==3 and snap['reserved_cny']==20
    with pytest.raises(QwenTransportError): GlobalBudgetLedger(limit_cny=30,path=path).reserve(8,'over')
    with pytest.raises(ValueError): cli._execution_budget_guard(args(path,29))

def test_foreign_ledger_is_untouched(tmp_path):
    foreign=tmp_path/'old60.sqlite'; led=GlobalBudgetLedger(limit_cny=60,path=foreign)
    r=led.reserve(3,'old'); led.settle(r['reservation_id'],1)
    before=foreign.read_bytes()
    with pytest.raises(ValueError): cli._execution_budget_guard(args(foreign))
    dedicated=tmp_path/'new30.sqlite'; cli._execution_budget_guard(args(dedicated))
    assert foreign.read_bytes()==before
    assert cli._execution_budget_guard(args(dedicated))['actual_cny']==0
