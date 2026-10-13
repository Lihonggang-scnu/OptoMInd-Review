import json
from trial_transport import ROOT, LEDGER, KEY_FILE, TrialClient, write_json
from optomind_research.runtime.upgrade3.paper_reading_batch import run_paper_reading_batch
from optomind_research.runtime.upgrade3.planning_pool_adapter import publish_planning_pool
out = ROOT / 'review'
records = []
for path in sorted((out / 'materials-02/_batch_checkpoints').glob('*.json')):
    row = json.loads(path.read_text(encoding='utf-8'))
    if row['material_depth'] != 'fulltext':
        raise ValueError('Metadata fallback cannot enter the full-text pilot')
    records.append({'paper_id': row['identity']['canonical_paper_id'],
        'snapshot_dir': row['snapshot_path'], 'plan_path': str(out / 'FINAL_SCHEMA.json')})
write_json(out / 'READING_MANIFEST.json', {'finalized': True, 'records': records})
result = run_paper_reading_batch(manifest_path=out / 'READING_MANIFEST.json', output_root=out / 'cards',
    mode='run', thinking=False, thinking_budget=0, concise_output=True, max_output_tokens=8192,
    key_file=KEY_FILE, budget_ledger_path=LEDGER.path, budget_limit_cny=15., initial_workers=1,
    max_workers=1, client_factory=lambda item, attempt: TrialClient(model='qwen3.7-flash', lane='review-card'))
write_json(out / 'CARDS_RESULT.json', result)
published = publish_planning_pool(batch_state_path=out / 'cards/BATCH_STATE.json', output_dir=out / 'planning-pool')
write_json(out / 'POOL_RESULT.json', published)
print(json.dumps({'cards': result.get('counts'), 'selected': len(published['selected_cards'])}))
