# Resume after handle fix

`resume_after_handle_fix.py` reuses the existing `continuous_driver.QualityBarriers` lifecycle. It reads the five root-reviewed stage results (`provisional_scope`, `level1_tools`, `level1_outline`, `chapter_proposals`, and `harmonized_scope`) directly from the current planning output, so their stage callables are not invoked. It replays the root-reviewed `quality_checkpoints/source_routing.json` snapshot and marks the existing routing method as already reviewed. The normal production `_tool_cycle` remains in control for level2 and later stages; the repaired handle fallback therefore reaches its real adaptive boundary. The next gate is `08_level2_tools`, after the new production stage is persisted.

Offline verification passed:

- five replayed stage callables: 0 invocations;
- routing provider calls: 0;
- fake level2 adaptive calls: 1;
- repaired directed requests at the adaptive boundary: 6;
- level2 stage persisted before the gate: true;
- ledger changed: false.

## Command after root approval

Use the exact production arguments recorded in `CURRENT_PROCESS_FULL_STAGE_GATED.json`, replacing only the driver path:

```text
C:\Anaconda\python.exe -X utf8 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\resume_after_handle_fix.py -- --run --resume --pool F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\INPUT_POOL.jsonl --plan F:\OptoMind-Review-2\outputs\upgrade3\CROSSDOMAIN_REWORK\X1_microbiome_ICI\PLAN.json --output-dir F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning --topic-id microbiome_ici_review --key-file F:\OptoMind-Review-2\api_keys\qwen-api-key.txt --budget-ledger F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\budget.sqlite --budget-limit-cny 40 --chapter-model qwen3.7-flash --timeout-seconds 900 --thinking-budget 8192 --output-tokens 32000 --deep-read-limit 40 --chapter-workers 1 --reader-workers 3 --tokenizer F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json --local-material-index F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\index\topic_material_index.sqlite --planning-revision --prior-reading F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\N391413e9fcdb\round_1\directed\20c143d5373eab0bfea2067cf66b0e260dfeb1ef\DIRECTED_READING.json --prior-reading F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\Nbe99ec47a99f\round_1\directed\CorpusId_252309032\DIRECTED_READING.json --prior-reading F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\chapters\external\Ndfd78cc95a8e\round_1\directed\705d021ac660029794f037498bc1ffea03cbe239\DIRECTED_READING.json --prior-reading F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\level1\external\N0ee5abc13eb0\round_1\directed\071823eff68d7dbea395490dd4643170131b5f6a\DIRECTED_READING.json --prior-reading F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585\level1\external\Nba5308868ca9\round_1\directed\CorpusId_286786239\DIRECTED_READING.json
```

Do not run concurrently with another driver, add a retry flag, or alter the saved planning artifacts. Root owns the gate decision.
