param(
    [ValidateSet('preview','arrange','write','assemble')][string]$Stage = 'preview',
    [string]$WriterModel = 'qwen3.5-plus',
    [int]$WriterOutputTokens = 12000,
    [int]$WriterThinkingBudget = 4000,
    [int]$MaxWriters = 3,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$taskRoot = 'F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930'
$restoreRoot = Join-Path $taskRoot 'body_restore_20261001'
# Restored defaults match the successful 20260927 UNIT_RESULT/run metadata:
# qwen3.5-plus, output_tokens=12000, thinking_budget=4000, max concurrency=3.
$arrangementRoot = Join-Path $restoreRoot 'body\arrangement'
$bodyRoot = if ($Stage -in @('write','assemble')) { Join-Path $restoreRoot 'body_plus' } else { Join-Path $restoreRoot 'body' }
$taskArgs = @('-X','utf8',(Join-Path $taskRoot 'RUN_BODY.py'),$Stage,'--plan-root',(Join-Path $restoreRoot 'plan'),'--output-root',$bodyRoot,'--arrangement-root',$arrangementRoot,'--ledger',(Join-Path $taskRoot 'budget.sqlite'),'--global-budget-cny','100','--round-cap-cny','10','--max-writers',[string]$MaxWriters,'--writer-model',$WriterModel,'--writer-output-tokens',[string]$WriterOutputTokens,'--writer-thinking-budget',[string]$WriterThinkingBudget)
if ($Stage -in @('arrange','write') -and -not $DryRun) { $taskArgs += '--allow-paid' }
if ($Stage -eq 'write' -and $DryRun) { $taskArgs += '--dry-run' }
if ($Stage -eq 'assemble') { $taskArgs += '--write-assembly' }
& python @taskArgs
exit $LASTEXITCODE
