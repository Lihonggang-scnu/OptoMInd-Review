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


def test_missing_previously_marked_ledger_cannot_reset_spend(tmp_path):
    path = tmp_path / 'guide.sqlite'
    cli._execution_budget_guard(args(path))
    path.unlink()
    with pytest.raises(ValueError, match='no_budget_reset'):
        cli._execution_budget_guard(args(path))
    assert not path.exists()


@pytest.mark.parametrize('content', [b'', b'not a database'])
def test_marked_empty_or_corrupt_ledger_is_not_initialized(tmp_path, content):
    path = tmp_path / 'guide.sqlite'
    cli._execution_budget_guard(args(path))
    path.write_bytes(content)
    with pytest.raises(ValueError, match='invalid_sqlite'):
        cli._execution_budget_guard(args(path))
    assert path.read_bytes() == content


@pytest.mark.parametrize('field,value', [('limit_cny', 'nan'), ('limit_cny', 'inf'),
                                         ('amount_cny', float('inf')), ('actual_cny', -1)])
def test_invalid_persisted_values_are_rejected_read_only(tmp_path, field, value):
    import sqlite3
    path = tmp_path / 'guide.sqlite'
    cli._execution_budget_guard(args(path))
    led = GlobalBudgetLedger(limit_cny=30, path=path)
    reservation = led.reserve(5, 'spent')
    led.settle(reservation['reservation_id'], 2)
    with sqlite3.connect(path) as db:
        if field == 'limit_cny':
            db.execute("UPDATE budget_meta SET value=? WHERE key='limit_cny'", (value,))
        else:
            db.execute(f'UPDATE reservations SET {field}=?', (value,))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        cli._execution_budget_guard(args(path))
    assert path.read_bytes() == before
