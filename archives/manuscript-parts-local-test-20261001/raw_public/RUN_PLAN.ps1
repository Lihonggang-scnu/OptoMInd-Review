param([ValidateSet('level1','level2','full')][string]$Stage = 'level1', [switch]$Resume)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$taskRoot = 'F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930'
Set-Location -LiteralPath (Join-Path $taskRoot 'worktree')
$taskArgs = @('-X','utf8','scripts/upgrade3/progressive_review_plan.py','--run','--planning-revision',
'--pool','F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\INPUT_POOL.jsonl',
'--plan','F:\OptoMind-Review-2\outputs\upgrade3\CROSSDOMAIN_REWORK\X1_microbiome_ICI\PLAN.json',
'--output-dir',(Join-Path $taskRoot 'new_plan'),'--topic-id','X1_microbiome_ICI',
'--key-file','api_keys/<redacted>',
'--budget-ledger',(Join-Path $taskRoot 'budget.sqlite'),'--budget-limit-cny','100',
'--deep-read-limit','40','--chapter-workers','3','--reader-workers','3','--chapter-model','qwen3.7-flash',
'--thinking-budget','8192','--output-tokens','32000','--timeout-seconds','900',
'--tokenizer','F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json',
'--local-material-index','F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\index\topic_material_index.sqlite',
'--prior-reading','F:\OptoMind-Review-2\outputs\progressive_review_plan\20260924_microbiome\run01\level1\directed',
'--prior-reading','F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585')
if ($Stage -ne 'full') { $taskArgs += @('--stop-after',$Stage) }
if ($Resume) { $taskArgs += '--resume' }
& python @taskArgs
exit $LASTEXITCODE
