import os
from trial_transport import ROOT, LEDGER, KEY_FILE, TrialClient
os.environ['OPTOMIND_ECONOMY_TEXT_CEILING'] = '0'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['OPTOMIND_S2_CACHE_PATH'] = str(ROOT / 'review/s2-cache.sqlite')
from optomind_research.runtime.upgrade3.module4 import runtime
runtime.QwenDirectClient = TrialClient
from scripts.upgrade3.progressive_review_plan import main
out = ROOT / 'review'
args = ['--run', '--pool', str(out / 'planning-pool/PLANNING_POOL.jsonl'),
    '--plan', str(out / 'FINAL_SCHEMA.json'), '--output-dir', str(out / 'planning-02'),
    '--topic-id', 'visible-achromatic-metalens-pilot-20261013',
    '--key-file', str(KEY_FILE), '--budget-ledger', str(LEDGER.path), '--budget-limit-cny', '15',
    '--deep-read-limit', '1', '--chapter-workers', '1', '--reader-workers', '1',
    '--thinking-budget', '0', '--reader-thinking-budget', '0', '--output-tokens', '8192',
    '--reader-output-tokens', '8192', '--no-external-supplement', '--stop-after', 'level2',
    '--no-outline-strengthening', '--tokenizer', 'F:/OptoMind-Review-2/data/tokenizers/qwen3_5_9b/tokenizer.json',
    '--material-parser-mode', 'docling_basic', '--docling-batch-index', str(out / 'docling-basic/BATCH_RESULT.json'),
    '--local-formula-config', 'F:/OptoMind-Review-2/outputs/pdf_bulk_route_20261013/integration_audit/formal_formula_preview/hook_config.json',
    '--local-formula-cache-root', str(out / 'formula-cache')]
raise SystemExit(main(args))
