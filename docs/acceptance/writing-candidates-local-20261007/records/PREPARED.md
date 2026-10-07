# Writing candidates 60 CNY preparation checkpoint

Prepared offline at locked HEAD `fe2f1c2adde414e71341287845d0520325e602c6`.

- Approved source: `on_demand_promotion_local_20261007_10cny/arrangement_paid/Ch2/CHAPTER_ARRANGEMENT.json` with paired `ARRANGEMENT_INPUT.json`.
- Planned first comparison: the same complete four unit Ch2 input through `chapter`, `units_edit`, and `hierarchical`, all using `config/writer_candidates/balanced.json`.
- Local meter: `F:/OptoMind-Review-2/data/tokenizers/qwen3_5_9b/tokenizer.json`; no provider, key, paid call, or ledger mutation has occurred.
- Shared ledger to inspect: `post_body_revision_local_experiment_20261005/live/budget.sqlite`; preserve all historical rows and cap this round from its startup committed/held total plus 60 CNY only after root review.

Offline acceptance: `PYTHONUTF8=1 python -m pytest -q tests/upgrade3/test_writer_candidates_contracts.py tests/upgrade3/test_writer_candidates_engine.py tests/upgrade3/test_writer_candidates_cli.py tests/upgrade3/test_writer_candidates_integration.py` -> `124 passed in 17.17s`.

All three previews use the same input hash `11ca4978141ad642d8cab038a286855e1fce29cc05b5ede87998199473000270`, arrangement SHA-256 `c9a7ebafefc253a211ab0c6b5345cee5acaa66e30d490e7638e40b6bce8e40ad`, paired view SHA-256 `66d1dd3e7d696eb6d8c8d634721202610b42261f39576d0aa1c5d7d0df997472`, and local tokenizer SHA-256 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42` (`tokenizers 0.23.2`). Each has `model_calls=0`, `client_invocations=0`, and `paid_dispatch_count=0`.

Balanced effective profile for every planned stage: `qwen3.5-plus`, thinking enabled with budget `16384`, answer allowance `49152` (combined output allowance `65536`), `json_mode=false`, `stream=true`, request/read timeout `900s`, overall stream timeout `3600s`.

Preview stage estimates (local tokenizer; provider usage is unknown until a real response):

| route | stages | measured -> reserved input tokens | fits | estimated max CNY | stage message SHA-256 |
|---|---:|---:|---|---:|---|
| chapter | 1 writer | 126311 -> 149661 | yes | 2.171508 | `writer_chapter`: `b76228dece3a85c3dcefe334e2b78b45e89eda8846847b07bd03b395a123414c` |
| units_edit | 4 writers | 59541 -> 74878; 49218 -> 63317; 58005 -> 73158; 58293 -> 73481 | yes | 1.872376; 1.826132; 1.865496; 1.866788 | `writer_unit_001`: `b49d45230bc9950890cb81f4190e445086fbdc3e63bd08dff6d341a3ac28e148`; `writer_unit_002`: `a4ce33114cfea7dca0c159f31fbf050f7e734909cf697e90fe028b812fba60e5`; `writer_unit_003`: `46fd381c91117722f1ab0abdf0e50671edd3a44078749102b624ac81b2b6798f`; `writer_unit_004`: `85635606620335923821dc01d4580741846f7949422fe9acc7645a3c9b1834fe` |
| hierarchical | 1 cluster | 126333 -> 149685 | yes | 2.171604 | `writer_cluster_001`: `638e39a071def0d76e2044bd660357ed14d31dbcb1496a78dfb2c5f72c757160` |

The units and hierarchical editor requests are correctly deferred in preview because they depend on actual draft prose; no editor call is claimed or priced as if it had executed. The hierarchical preview fits the full chapter as one adjacent cluster, so no capacity partition or fallback was introduced.

The preview payload has four units and twelve editable tasks, thirty-two source records with matching handles and material identity fields, six read-only neighboring chapters, twelve task roles, and four unit contexts. The chapter and hierarchical messages carry the full 32-source set; each units message carries its own complete unit source set. The source `study_summary_A`, `review_planning_B`, aliases, source briefs, task uses, and chapter frame fields are retained by the real material builder. No provider response or scientific answer was added by preparation.

Read-only ledger snapshot: source `budget.sqlite` SHA-256 `cf087990fb368f0ea407e75c2e830b9a4ebd8c3ffa08d523a928b7a9feac6a6b`, snapshot `budget.sqlite.initial_snapshot` SHA-256 `5dc6e0ff3a39f445ff15412ebf085da0da9330b219221a9901d5ec18750c1fbf`. Persisted cap is `125.0` CNY; 90 reservation rows are `85 settled`, `2 reserved`, and `3 uncertain`. Settled actual is `77.5919498` CNY, open reserved amount is `9.421812` CNY, open uncertain amount is `8.7471504` CNY, and current committed exposure is `95.7609122` CNY. Proposed cap for this round is `155.7609122` CNY (`95.7609122 + 60.0`), retaining every historical row. No cap or row was mutated by this preparation. The open rows belong to prior experiment call IDs and must remain held; root should check for any active unrelated writer before changing the shared cap.

After root reviews this checkpoint and authorizes the one-time existing-ledger cap adjustment, the first proposed paid execution is the single balanced chapter writer:

```powershell
$env:PYTHONUTF8='1'; python scripts/upgrade3/writer_candidates.py `
  --arrangement 'F:\OptoMind-Review-2\outputs\on_demand_promotion_local_20261007_10cny\arrangement_paid\Ch2\CHAPTER_ARRANGEMENT.json' `
  --view 'F:\OptoMind-Review-2\outputs\on_demand_promotion_local_20261007_10cny\arrangement_paid\Ch2\ARRANGEMENT_INPUT.json' `
  --route chapter --config config/writer_candidates/balanced.json `
  --tokenizer 'F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json' `
  --output 'F:\OptoMind-Review-2\outputs\writer_candidates_local_20261007_60cny\chapter_live' `
  --run --budget-ledger 'F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite' `
  --key-file '<local-key-file>'
```

The chapter preview projects a first-call maximum reservation/cost of `2.171508` CNY. `--budget-limit` and `--allow-max` remain omitted after the existing ledger cap is updated; no editor or second route should be launched until root inspects this first draft and its actual settled/uncertain usage.

Final preview run IDs are `chapter`: `20261007T114934Z-b71205dd0d`, `units_edit`: `20261007T114934Z-4979245985`, and `hierarchical`: `20261007T114934Z-20055d3707`. The full-field projection evidence is `PROJECTION_CHECK.json`; the official test log is `offline_acceptance.log`.

No Max configuration, key-file read, model request, budget reservation, cap change, or production source edit is authorized at this checkpoint.
