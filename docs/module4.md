# Module 4 full available text reader

Module 4 consumes a frozen document snapshot plus the original research question and PLAN facets. It emits a source anchored paper dossier, coverage receipts, a separate semantic verification report, and append only feedback candidates for M3. It reads every available research block, including captions, formulas, footnotes, appendices, references, and textual supplements. Quotes are matched against the stored `text_normalized` coordinate space; model supplied anchor IDs are never trusted.

Implementation status is `IMPLEMENTED_PENDING_CONTENT_ACCEPTANCE`. The paid calibration report is `outputs/module4/20260919/paid6/M4_ACCEPTANCE_REPORT.md`. Three distinct papers have been used for calibration; repeated versions of those papers are not an independent holdout set. Developer-agent source reviews are provisional, not human expert acceptance. An automated `ready` flag means the recorded local gates passed, not that all scientific omissions or mistakes have been eliminated.

The current workhorse recommendation is explicit `qwen3.7-flash` with thinking, an 8192-token thinking budget and whole-block source binding. The tested Plus configuration did not show a consistent quality advantage sufficient to justify its latency. One previously unseen permafrost paper was additionally tested with frozen prompt/engine hashes; it is a probe, not completion of the planned 16-paper holdout evaluation. The module remains a reviewed-draft workflow pending content acceptance.

The public input and dossier contracts are in `optomind_research/runtime/upgrade3/module4/schemas/`. `validate_input` rejects missing original questions, identity conflicts, stale manifest or upstream hashes, malformed facets, unknown top level fields, and metadata only material. A `structured_partial` snapshot runs with explicit limits. Abstract or snippet material requires the approved background override and is recorded as `background_supplement`; it cannot claim full text processing.

The default delivery path is `whole_text` when the complete snapshot fits the configured context. A section or paginated run records its receipts and remains experimental and not ready for formal delivery until a paper level synthesis is supplied. A non-stop finish reason, invalid JSON, or failed verifier prevents ready delivery. Missing usage keeps the budget reservation uncertain; it is not counted as zero cost. This is a text reader: image interpretation has not been implemented, and the source material inventory is retained in coverage.

Source binding has two explicit modes. `--source-binding exact_quote` requires the model's quote to match the frozen source exactly. `--source-binding whole_block` asks the model to select supporting block IDs and lets the local program copy each complete original block, recording `source_binding=program_whole_block` on the anchor. This is paragraph-level provenance, not model-generated quotation. Both modes reject unknown/cross-snapshot sources and still require semantic verification: an existing paragraph does not automatically support a claim. Raw model responses remain available for audit.

The source-handle upgrade exposes short, snapshot-local source handles to the model and maps them exactly back to canonical block IDs before binding. The dossier's `audit_ref.source_handle_audit` retains the mapping and snapshot identity; unknown handles are not fuzzy-matched. This reduces identifier transcription burden, not scientific reading errors.

The v6 experimental reading/paper-map changes were rejected after five real Flash readings and independent source reviews. The default scientific instructions and paper-map behavior return to the pre-round v5 baseline; only the source-handle interface is retained, with distinct prompt versions to prevent cache confusion. The trial engine, original responses, scores and failure details are preserved under `outputs/module4/20260919/tuning_round1/`. Baseline reading remains pending content acceptance; the rollback is not a claim that its known omissions are fixed.

The reader creates a methods-and-conditions worksheet before answering Facets. Verification receives the entire available scientific text as a deduplicated source bank, so it can check captions or methods omitted by the reader's chosen citations. Factual statement support and whether a Facet coverage label is appropriate are separate review targets. Model-produced paper-map summaries are also reviewed. Missing, duplicated or unexpected review targets prevent a complete verification receipt.

Formula extraction gaps are retained explicitly. Only locally confirmed corrupt formula blocks or numeric layout labels may become nonblocking material limitations; ordinary unexplained omissions remain blocking. This does not reconstruct equations or interpret missing images. JSON normalization accepts only a well-shaped verifier review-array wrapper or mechanically unchanged quotation escaping; it cannot complete a truncated response or invent scientific fields.

## Offline contract and smoke run

The test suite uses injected transports only. The executable X1 smoke reads the real 204 block frozen snapshot, generates a deterministic injected reader and verifier response, and writes inspectable artifacts:

```powershell
python outputs/module4/20260919/run_x1_injected_stub.py
```

The current result is under `outputs/module4/20260919/x1_injected_stub_v2/`, including `PAPER_READING_DOSSIER.json`, `COVERAGE_REPORT.json`, `VERIFICATION_REPORT.json`, `COMMIT_MANIFEST.json`, and `OFFLINE_STUB_NOTICE.json`. The notice records `network_called: false`; the content is a transport and provenance smoke, not a paper quality assessment. The earlier smoke directory is preserved.

## CLI

Prepare a validated input from a raw task source, PLAN, cards or corpus references, and a prepared snapshot:

```powershell
python scripts/upgrade3/module4.py prepare `
  --plan outputs/upgrade3/CROSSDOMAIN_R2/X1_microbiome_ICI/PLAN.json `
  --question-ref outputs/upgrade3/CROSSDOMAIN_R2/CROSS_DOMAIN_REWORK_MANIFEST.json `
  --question-pointer /tests/0/research_question `
  --model qwen3.7-flash `
  --cards outputs/upgrade3/CROSSDOMAIN_R2/X1_microbiome_ICI/INVESTIGATION_CARDS.json `
  --snapshot outputs/local_materials_rescue/20260918/live_v3/snapshots/74786f8df0c7648840756833a4b4bc49ace34be6/snapshot-e35f364807fd601f43716288 `
  --canonical-paper-id 74786f8df0c7648840756833a4b4bc49ace34be6 `
  --output-dir outputs/module4/20260919/cli_prepare
```

Validate without making a model call:

```powershell
python scripts/upgrade3/module4.py inspect-input --input path/to/READING_INPUT.json --snapshot path/to/snapshot
```

Real reading requires an explicitly selected supported model, a positive shared budget and a persistent ledger. The authorized M4-only evaluation uses the shared 6 CNY ledger under `outputs/module4/20260919/paid6/`; consult that directory for actual attempts and acceptance results. The CLI has no implicit winning model before content acceptance:

```powershell
python scripts/upgrade3/module4.py read `
  --input path/to/READING_INPUT.json `
  --snapshot path/to/snapshot `
  --strategy whole_text `
  --model qwen3.7-flash `
  --thinking --thinking-budget 8192 `
  --source-binding whole_block --verifier-output-tokens 16384 `
  --key-file api_keys/qwen-api-key.txt `
  --real `
  --global-budget-cny $M4BudgetCny `
  --budget-ledger-path outputs/module4/20260919/BUDGET.sqlite `
  --output-dir outputs/module4/20260919/real_run
```

The direct runtime uses the configured Qwen credential resolver, disables ambient proxies, sends an explicit model and JSON response format, records raw responses before parsing, and refuses model or finish reason mismatches. A conservative reservation is made before every physical attempt; settled spend and uncertain calls remain in the shared SQLite ledger.

Set `$M4BudgetCny` only to the explicitly approved total, and set `OPTOMIND_ECONOMY_TEXT_CEILING=0` to keep explicit model routing. The supported models are `qwen3.7-flash` and `qwen3.5-plus`; JSON mode defaults to non-thinking. Plus thinking requires `--thinking --free-json`: this omits provider-forced JSON format while retaining local JSON parsing and schema checks. Thinking calls explicitly cap reasoning via `--thinking-budget` (default 8192); reservations cover visible output plus reasoning. `--revision-context path.json` provides a previous draft and provisional review feedback for a new, independently checked reading attempt; feedback does not become source evidence and retries are not unlimited or implicit.

A committed run verifies its stored artifact hashes and replays without calling either reader or verifier again. Changed task, model, prompt/schema, or runtime settings require a new output directory. A sibling `.module4.lock` prevents concurrent writers; after an interrupted process, inspect the recorded PID and directory before manually recovering the lock. M1–M3 and material snapshots are never overwritten.

Run the offline tests with:

```powershell
$tests = Get-ChildItem tests/upgrade3 -Filter 'test_module4_*.py' | ForEach-Object { $_.FullName }
python -m pytest -q @tests
```
