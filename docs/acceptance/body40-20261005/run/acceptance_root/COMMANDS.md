# BODY-40 staged acceptance preparation

Status: prepared; no paid execution was launched by this preparation task.

New root: `F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny`
Worktree: `F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\worktree` at fbf6f79328d533af2563678fc2d0e81364595c44
Ledger: `F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\budget.sqlite` initialized with limit 40 CNY and zero actual/held spend.

The offline preflight used the unchanged production CLI and reported:

- 585 pool rows; no missing card paths.
- Estimated provisional payload: 516,670 local tokens.
- With 12% margin and 8,192 thinking + 32,000 output: 627,055 tokens.
- Conservative first-call reservation estimate: 3.31206 CNY.
- Budget before paid work: actual 0, held 0, available 40 CNY.

## Offline preflight command

```text
'C:\Anaconda\python.exe' -X utf8 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\worktree\scripts\upgrade3\progressive_review_plan.py' --preflight --pool 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\INPUT_POOL.jsonl' --plan 'F:\OptoMind-Review-2\outputs\upgrade3\CROSSDOMAIN_REWORK\X1_microbiome_ICI\PLAN.json' --output-dir 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning' --topic-id microbiome_ici_review --budget-ledger 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\budget.sqlite' --budget-limit-cny 40 --chapter-model qwen3.7-flash --timeout-seconds 900 --thinking-budget 8192 --output-tokens 32000 --deep-read-limit 40 --chapter-workers 1 --reader-workers 3 --tokenizer 'F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json' --local-material-index 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\index\topic_material_index.sqlite' --planning-revision
```

## First paid command after root review

The root driver catches its intentional `BaseException` after the original production `_stage` persists `provisional_scope`; the CLI should therefore leave no `RUN_FAILURE.json` for that checkpoint.

```text
'C:\Anaconda\python.exe' -X utf8 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\staged_driver.py' --checkpoint provisional_scope -- --run --stop-after level1 --pool 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\INPUT_POOL.jsonl' --plan 'F:\OptoMind-Review-2\outputs\upgrade3\CROSSDOMAIN_REWORK\X1_microbiome_ICI\PLAN.json' --output-dir 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning' --topic-id microbiome_ici_review --key-file 'F:\OptoMind-Review-2\api_keys\qwen-api-key.txt' --budget-ledger 'F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\budget.sqlite' --budget-limit-cny 40 --chapter-model qwen3.7-flash --timeout-seconds 900 --thinking-budget 8192 --output-tokens 32000 --deep-read-limit 40 --chapter-workers 1 --reader-workers 3 --tokenizer 'F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json' --local-material-index 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\index\topic_material_index.sqlite' --planning-revision --prior-reading 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\N391413e9fcdb\round_1\directed\20c143d5373eab0bfea2067cf66b0e260dfeb1ef\DIRECTED_READING.json' --prior-reading 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\Nbe99ec47a99f\round_1\directed\CorpusId_252309032\DIRECTED_READING.json' --prior-reading 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\Ndfd78cc95a8e\round_1\directed\705d021ac660029794f037498bc1ffea03cbe239\DIRECTED_READING.json' --prior-reading 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\level1\external\N0ee5abc13eb0\round_1\directed\071823eff68d7dbea395490dd4643170131b5f6a\DIRECTED_READING.json' --prior-reading 'F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\level1\external\Nba5308868ca9\round_1\directed\CorpusId_286786239\DIRECTED_READING.json'
```

The five `--prior-reading` paths are the only compatible `DIRECTED_READING.json` files selected by `load_prior_readings` from run585 level1/chapters. No planner stage cache or prior planner answer is supplied.

First acceptance check: inspect `planning/stages/provisional_scope.json`, `planning/RUN_STATE.json`, `EXECUTION_STATE.json`, and `budget.sqlite`; then root reviews the real stage input/output before authorizing the next stage.
