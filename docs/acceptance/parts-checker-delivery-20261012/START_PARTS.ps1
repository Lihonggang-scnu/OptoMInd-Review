param([switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
Set-Location 'F:\OptoMind-Review-2\outputs\outline_to_body_astra_20261010_60cny\worktree'
$env:PYTHONPATH = '.'
$partsArgs = @(
 '-X', 'utf8', 'scripts/upgrade3/manuscript_parts.py',
 '--draft', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/fresh_full/article_edit/c270e6c6bf80a2ac33fa8a8fe05faf15a35f5f2b35fcffb2323e569d0bd0d965/CANDIDATE_HANDLES.md',
 '--body-report', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/fresh_full/DELIVERY_REPORT.json',
 '--context', 'F:/OptoMind-Review-2/outputs/serial_parts_final_delivery_20261011/INPUT_CONTEXT.json',
 '--output', 'F:/OptoMind-Review-2/outputs/parts_checker_acceptance_20261012_20cny/live_parts',
 '--live', '--repair-invalid',
 '--ledger', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/budget.sqlite', '--budget-limit', '60',
 '--account-ledger', 'F:/OptoMind-Review-2/local_accounting/qwen_first_20261010_300cny/budget.sqlite', '--account-budget-limit', '300',
 '--account-mapping-dir', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/accounting_meta/physical_calls',
 '--tokenizer', 'F:/OptoMind-Review-2/data/tokenizers/qwen3_5_9b/tokenizer.json',
 '--incremental-budget', '5', '--thinking-budget', '16384', '--output-tokens', '32768'
)
if (-not $PreviewOnly) {
 $partsArgs += @('--run', '--key-file', 'F:/OptoMind-Review-2/api_keys/qwen-api-key.txt', '--key-index', '1')
}
& 'C:\Anaconda\python.exe' @partsArgs
exit $LASTEXITCODE
