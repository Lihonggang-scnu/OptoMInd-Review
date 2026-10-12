param([switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
Set-Location 'F:\OptoMind-Review-2\outputs\outline_to_body_astra_20261010_60cny\worktree'
$env:PYTHONPATH = '.'
$checkerArgs = @(
 '-X', 'utf8', 'scripts/upgrade3/manuscript_delivery.py',
 '--parts-report', 'F:/OptoMind-Review-2/outputs/parts_checker_acceptance_20261012_20cny/live_parts/LIVE_REPORT.json',
 '--config', 'F:/OptoMind-Review-2/outputs/parts_checker_acceptance_20261012_20cny/delivery_live_config.json',
 '--output', 'F:/OptoMind-Review-2/outputs/parts_checker_acceptance_20261012_20cny/live_checker_after_seam_fix', '--live',
 '--ledger', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/budget.sqlite', '--budget-limit', '60',
 '--account-ledger', 'F:/OptoMind-Review-2/local_accounting/qwen_first_20261010_300cny/budget.sqlite', '--account-budget-limit', '300',
 '--account-mapping-dir', 'F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/accounting_meta/physical_calls',
 '--incremental-budget', '14.4644988', '--tokenizer', 'F:/OptoMind-Review-2/data/tokenizers/qwen3_5_9b/tokenizer.json'
)
if (-not $PreviewOnly) {
 $checkerArgs += @('--run', '--key-file', 'F:/OptoMind-Review-2/api_keys/qwen-api-key.txt', '--key-index', '1')
}
& 'C:\Anaconda\python.exe' @checkerArgs
exit $LASTEXITCODE
