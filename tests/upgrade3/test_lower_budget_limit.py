import json
import sqlite3
import pytest
from scripts.upgrade3.lower_budget_limit import lower_limit, main
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger,QwenTransportError


def setup_ledger(tmp_path):
    p=tmp_path/'budget.sqlite';ledger=GlobalBudgetLedger(path=p,limit_cny=100)
    r=ledger.reserve(15,'settled');ledger.settle(r['reservation_id'],10)
    r=ledger.reserve(12,'uncertain');ledger.settle(r['reservation_id'],None,uncertain=True)
    ledger.reserve(5,'inflight')
    return p,ledger


def test_preview_does_not_change_spend_or_limit(tmp_path):
    p,l=setup_ledger(tmp_path);before=l.as_dict();report=lower_limit(p,40)
    assert report['exposure_cny']==27 and report['remaining_cny']==13
    assert l.as_dict()==before


def test_lower_preserves_reservations_and_stale_client_obeys_cap(tmp_path):
    p,l=setup_ledger(tmp_path); before=l.as_dict()['reservations']
    assert lower_limit(p,40,expected_limit=100,apply=True)['status']=='lowered'
    with pytest.raises(QwenTransportError,match='global_budget_exceeded'):l.reserve(14,'too-much')
    assert l.as_dict()['limit_cny']==40
    assert l.as_dict()['reservations']==before
    l.reserve(13,'fits-exactly')
    with pytest.raises(QwenTransportError,match='global_budget_exceeded'):l.reserve(.01,'beyond')
    assert lower_limit(p,40,expected_limit=40,apply=True)['status']=='no_change'
    with sqlite3.connect(p) as db:assert db.execute('SELECT COUNT(*) FROM budget_limit_changes').fetchone()[0]==1


def test_exposure_over_new_cap_is_not_erased(tmp_path):
    p,l=setup_ledger(tmp_path); l.reserve(30,'extra')
    report=lower_limit(p,40,apply=True)
    assert report['already_over_new_limit'] and report['remaining_cny']==0
    assert len(l.as_dict()['reservations'])==4
    with pytest.raises(QwenTransportError):l.reserve(.01,'blocked')


@pytest.mark.parametrize('target,expected',[(101,None),(40,99),(float('nan'),None),(-1,None)])
def test_invalid_changes_fail_without_mutation(tmp_path,target,expected):
    p,l=setup_ledger(tmp_path);before=l.as_dict()
    with pytest.raises(ValueError):lower_limit(p,target,expected_limit=expected,apply=True)
    assert l.as_dict()==before


def test_cli_and_missing_ledger(tmp_path):
    p,l=setup_ledger(tmp_path); report=tmp_path/'report.json'
    assert main(['--ledger',str(p),'--lower-to','40','--expected-limit','100','--apply','--report',str(report)])==0
    assert json.loads(report.read_text())['new_limit_cny']==40
    assert main(['--ledger',str(tmp_path/'absent'),'--lower-to','40','--apply'])==2
    assert not (tmp_path/'absent').exists()


def test_receipt_failure_does_not_hide_successful_lowering(tmp_path,capsys):
    p,l=setup_ledger(tmp_path)
    assert main(['--ledger',str(p),'--lower-to','40','--apply','--report',str(tmp_path)])==3
    text=capsys.readouterr().out
    assert '"status": "lowered"' in text and 'report_write_error' in text
    assert l.as_dict()['limit_cny']==40
