# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
from pathlib import Path
import json
import sqlite3

OLD = Path(r'<LOCAL_REVIEW2_PATH>')
OUT = Path(__file__).parent

def change_kind(original, replacement):
    if original == replacement:
        return 'unchanged'
    if original.replace('\r\n', '\n').replace('\r', '\n') == replacement.replace('\r\n', '\n').replace('\r', '\n'):
        return 'line_endings_only'
    return 'content_change_needs_quality_judgment'

def main():
    assert change_kind('A\r\n', 'A\n') == 'line_endings_only'
    assert change_kind('A  B', 'A B') != 'line_endings_only'
    assert change_kind('x=1', 'x=2') != 'unchanged'
    paths = {'old_A': 'live_attempt2/A/live/report.json', 'old_B': 'live_B_retry/B/recovery/report.json', 'old_C': 'live/C/live/report.json'}
    rows = []
    for scheme, relative in paths.items():
        report = json.loads((OLD / relative).read_text(encoding='utf-8-sig'))
        for record in report['issue_records']:
            issue = record['issue']
            attempts = record.get('attempts', [])
            last = attempts[-1] if attempts else {}
            proposal = last.get('proposal', {})
            replacement = proposal.get('replacement_text')
            operation = proposal.get('operation')
            kind = ('explicit_keep' if operation == 'no_change' else
                    change_kind(issue.get('original_text', ''), replacement) if isinstance(replacement, str) else
                    'no_executable_replacement')
            rows.append({'scheme': scheme, 'issue_id': issue['issue_id'], 'original_program_status': record.get('status'),
                         'original_applied': record.get('applied'), 'replayed_classification': kind,
                         'content_change_not_proof_of_improvement': True,
                         'reason': proposal.get('reason', record.get('reason')),
                         'report': relative})
    with sqlite3.connect('file:' + str(OLD/'live<LOCAL_RUNTIME_PATH>') + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        reservations = [dict(row) for row in conn.execute('SELECT * FROM reservations')]
    accounting = {}
    for variant in ('A', 'B', 'C'):
        selected = [r for r in reservations if str(r.get('call_id', '')).startswith('post-body-' + variant + '-')]
        accounting[variant] = {'actual_provider_attempts': len(selected),
                               'settled_cny': sum(float(r.get('actual_cny') or 0) for r in selected if r['status'] == 'settled'),
                               'uncertain_held_cny': sum(float(r.get('amount_cny') or 0) for r in selected if r['status'] == 'uncertain'),
                               'reserved_cny': sum(float(r.get('amount_cny') or 0) for r in selected if r['status'] == 'reserved')}
    result = {'model_calls': 0, 'classification': rows, 'cumulative_original_scheme_costs': accounting,
              'note': 'Replayed from saved returns. Reasoned unchanged output is a keep candidate, not a protocol/quality failure. Content changes still require independent quality assessment. Full scheme cost includes original failures and recovery, not just recovery increment.'}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/'root_old_replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'rows': len(rows), 'B_R3': next(r['replayed_classification'] for r in rows if r['scheme']=='old_B' and r['issue_id']=='R3'), 'accounting': accounting, 'model_calls': 0}, ensure_ascii=False))

if __name__ == '__main__':
    main()
