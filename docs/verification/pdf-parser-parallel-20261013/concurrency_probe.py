"""Run one explicit parallel benchmark through the formal product CLI."""
import argparse
import json
import sqlite3
import subprocess
import time
from pathlib import Path

O = Path(__file__).resolve().parent
R = O.parent.parent
S = R / 'worktree'

def budget(path):
    with sqlite3.connect('file:' + Path(path).as_posix() + '?mode=ro', uri=True) as conn:
        return {status: {'entries': n, 'actual_cny': actual, 'amount_cny': amount}
                for status, n, actual, amount in conn.execute(
                    'select status,count(*),coalesce(sum(actual_cny),0),sum(amount_cny) from reservations group by status')}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--concurrency', type=int, required=True)
    p.add_argument('--label', default='bandwidth')
    p.add_argument('--batch', action='store_true')
    p.add_argument('--run', action='store_true')
    p.add_argument('--replay', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--trial', default='')
    a = p.parse_args()
    binding = json.loads((R / 'RUN_BINDING.json').read_text(encoding='utf-8'))
    rec = next(x for x in json.loads((O / 'FRESH_LOCAL_MANIFEST.json').read_text(encoding='utf-8'))['records'] if x['label'] == a.label)
    base = O / 'concurrency_probe' / f'region_{a.concurrency}' / ('batch5' if a.batch else a.label)
    if a.trial:
        if not a.trial.replace('_', '').isalnum():
            raise ValueError('Unsafe trial name')
        base = base / a.trial
    base.mkdir(parents=True, exist_ok=True)
    conf = json.loads((O / 'qwen3.5-plus-recognition.json').read_text(encoding='utf-8'))
    conf['concurrency'] = a.concurrency
    config = base / 'recognition.json'
    config.write_text(json.dumps(conf, indent=2), encoding='utf-8')
    output = base / ('replay' if a.replay else 'live' if a.run else 'preview')
    cmd = ['C:/Anaconda/python.exe', '-B', '-X', 'utf8', '-m', 'scripts.upgrade3.build_local_materials',
           '--input', rec['pdf'], '--record-json', str(O / (a.label + '-record.json')),
           '--output-root', str(output), '--cache-root', str(O / 'acquisition_cache'),
           '--disable-rescue', '--deadline-seconds', '1200', '--request-timeout-seconds', '10',
           '--docling-cli-config', str(O / 'DOCLING_CLI.json'), '--docling-cache-root', str(O / 'docling_cache'),
           '--recognition-config', str(config), '--recognition-cache-root', str(base / 'cache'),
           '--recognition-ceiling-cny', '3',
           '--budget-ledger', binding['project_ledger'], '--budget-limit', '80',
           '--account-ledger', binding['account_ledger'], '--account-budget-limit', '300',
           '--account-mapping-dir', str(R / 'account_mappings'), '--key-file', binding['key_file'],
           '--key-index', '1', '--tokenizer', 'F:/OptoMind-Review-2/data/tokenizers/qwen3_5_9b/tokenizer.json']
    if a.batch:
        rows = []
        for item in json.loads((O / 'FRESH_LOCAL_MANIFEST.json').read_bytes())['records']:
            row = json.loads((O / (item['label'] + '-record.json')).read_bytes())
            row['local_pdf'] = item['pdf']
            rows.append(row)
        manifest = base / 'manifest.json'
        manifest.write_text(json.dumps({'records': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
        start = cmd.index('--input')
        end = cmd.index('--output-root')
        cmd[start:end] = ['--batch-manifest', str(manifest)]
        cmd.extend(['--docling-max-processes', '2', '--network-workers', '5'])
    if a.run or a.replay:
        cmd.append('--recognition-run')
    mode = 'replay' if a.replay else 'live' if a.run else 'preview'
    receipt = base / (mode + ('_resume' if a.resume else '') + '_receipt.json')
    if receipt.exists():
        raise RuntimeError('Existing benchmark receipt retained; do not repeat it: ' + str(receipt))
    before = {name: budget(binding[key]) for name, key in [('project', 'project_ledger'), ('account', 'account_ledger')]}
    if a.resume and not (base / (mode + '_receipt.json')).is_file():
        raise RuntimeError('Resume requires the preserved original receipt')
    suffix = mode + ('_resume' if a.resume else '')
    (base / (suffix + '_command.json')).write_text(json.dumps({'command': cmd, 'budget_before': before}, ensure_ascii=False, indent=2), encoding='utf-8')
    started = time.monotonic()
    child = subprocess.run(cmd, cwd=S, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1200)
    elapsed = time.monotonic() - started
    (base / (suffix + '.stdout.log')).write_text(child.stdout, encoding='utf-8')
    (base / (suffix + '.stderr.log')).write_text(child.stderr, encoding='utf-8')
    after = {name: budget(binding[key]) for name, key in [('project', 'project_ledger'), ('account', 'account_ledger')]}
    result = {'concurrency': a.concurrency, 'label': 'batch5' if a.batch else a.label, 'mode': mode,
              'exit_code': child.returncode, 'elapsed_seconds': round(elapsed, 3),
              'before': before, 'after': after, 'output': str(output)}
    for name in before:
        old = before[name].get('settled', {})
        new = after[name].get('settled', {})
        result[name + '_new_actual_cny'] = round(new.get('actual_cny', 0) - old.get('actual_cny', 0), 10)
        result[name + '_new_settled_requests'] = new.get('entries', 0) - old.get('entries', 0)
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return child.returncode

if __name__ == '__main__':
    raise SystemExit(main())
