"""Reconcile NEW60 by immutable starting reservation IDs; never modify the ledger."""
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START = json.loads((ROOT / 'ROUND_BUDGET_START.json').read_text(encoding='utf-8'))
OLD_IDS = set(START['historical_reservation_ids'])
DB = Path(START['ledger_path'])


def read_db(path):
    con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(row) for row in con.execute('select * from reservations order by created_at,reservation_id')]
    meta = dict(con.execute('select key,value from budget_meta'))
    con.close()
    return rows, meta


rows, meta = read_db(DB)
snapshot_rows, _ = read_db(ROOT / 'budget_before_round.sqlite')
old_now = {row['reservation_id']: row for row in rows if row['reservation_id'] in OLD_IDS}
old_before = {row['reservation_id']: row for row in snapshot_rows if row['reservation_id'] in OLD_IDS}
new = [row for row in rows if row['reservation_id'] not in OLD_IDS]
requested_models = {}
for path in ROOT.glob('*live*/stages/*/*/attempt_*/ACTUAL_REQUEST.json'):
    actual = json.loads(path.read_text(encoding='utf-8'))
    prefix = f'{path.parent.parent.parent.name}-{path.parent.parent.name[:12]}-{path.parent.name}'
    requested_models[prefix] = actual.get('profile', {}).get('model')


def requested_model(row):
    for prefix, model in requested_models.items():
        if row['call_id'].startswith(prefix + ':'):
            return model
    return None

settled = sum(float(row['actual_cny'] or 0) for row in new if row['status'] == 'settled')
held = sum(float(row['amount_cny']) for row in new if row['status'] not in ['settled', 'released'])
max_rows = [row for row in new if requested_model(row) == 'qwen3.8-max' or (row['returned_model'] or '') == 'qwen3.8-max' or
            'qwen3.8-max' in (row['request_metadata_json'] or '')]
max_settled = sum(float(row['actual_cny'] or 0) for row in max_rows if row['status'] == 'settled')
max_hold = sum(float(row['amount_cny']) for row in max_rows if row['status'] not in ['settled', 'released'])
records = []
for row in new:
    records.append({key: row[key] for key in ['reservation_id', 'call_id', 'amount_cny', 'actual_cny',
        'status', 'created_at', 'returned_model', 'finish_reason', 'request_id', 'raw_response_sha256']})
    records[-1]['usage'] = json.loads(row['usage_json']) if row['usage_json'] else None
    records[-1]['requested_model_from_actual_stage'] = requested_model(row)

report = {
    'schema_version': 'optomind.writer_candidates.root_final_budget.v1',
    'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'ledger_path': str(DB),
    'baseline_file': str(ROOT / 'ROUND_BUDGET_START.json'),
    'baseline_sha256': hashlib.sha256((ROOT / 'ROUND_BUDGET_START.json').read_bytes()).hexdigest(),
    'accounting_basis': 'Reservation-ID set difference from saved start; includes failed and interrupted calls',
    'fee_basis': 'Production client estimates using real provider usage, recorded in the shared ledger; not a reconciled provider invoice',
    'historical_rows_count': len(OLD_IDS),
    'all_historical_rows_unchanged': old_now == old_before,
    'ledger_metadata': meta,
    'new_authorized_cap_cny': 60,
    'new_reservations': len(new),
    'completed_usage_calls': sum(row['status'] == 'settled' for row in new),
    'calls_note': 'A reservation alone does not prove provider dispatch. Interrupted call evidence is recorded separately.',
    'new_settled_cny': round(settled, 10),
    'new_reserved_cny': round(sum(float(row['amount_cny']) for row in new if row['status'] == 'reserved'), 10),
    'new_uncertain_cny': round(sum(float(row['amount_cny']) for row in new if row['status'] == 'uncertain'), 10),
    'new_total_exposure_cny': round(settled + held, 10),
    'new_remaining_cny': round(60 - settled - held, 10),
    'max_settled_cny': round(max_settled, 10),
    'max_hold_cny': round(max_hold, 10),
    'max_total_exposure_cny': round(max_settled + max_hold, 10),
    'calls': records,
}
assert report['all_historical_rows_unchanged'], 'Historical rows changed; review before reporting'
assert report['new_total_exposure_cny'] <= 60, 'NEW60 exceeded'
assert report['max_total_exposure_cny'] <= 10, 'Max advisory limit exceeded'
(ROOT / 'FINAL_BUDGET.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({key: report[key] for key in ['new_reservations', 'new_settled_cny', 'new_reserved_cny',
    'new_uncertain_cny', 'new_total_exposure_cny', 'new_remaining_cny', 'max_total_exposure_cny',
    'all_historical_rows_unchanged']}, ensure_ascii=False))
