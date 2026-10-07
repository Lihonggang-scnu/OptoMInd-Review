# Chapter-writing candidates

These are three additive alternatives after an accepted chapter arrangement. They do not change the production BODY planner, arranger, existing unit writer, or legacy prompts. Implement and inspect the routes first; compare model writing quality only in an explicitly approved local run. No cloud paid run was used to establish the implementation checks.

## The three algorithms

- `chapter`: one writer sees the complete chapter task set, chapter/owner argument context, and full relevant source records; it drafts a coherent chapter in one call. It does not secretly become a unit writer and does not add an editor call.
- `units_edit`: each existing unit is drafted with its complete tasks and relevant evidence, while seeing the shared chapter argument and read-only task roles across the chapter. An editor then integrates the actual drafts with full relevant evidence.
- `hierarchical`: adjacent complete units are packed into context-fitting clusters. Only an oversized unit may be divided at complete task boundaries. Each cluster sees the shared chapter argument/task roles and full relevant evidence. Actual drafts are integrated afterward.

Both integration routes require `editor_pass: true`; setting it to false blocks the named route rather than silently changing it into concatenated drafts. For both integration routes, the editor first attempts one chapter-wide evidence-backed request. If it does not fit, the engine forms adjacent prose/task spans, keeps blocks sharing tasks together, and edits bounded windows. Neighbor prose is read-only when it fits; omitted neighboring blocks are explicitly reported. A single intact task/material set or prose/evidence span that still cannot fit is left pending. Its content is never silently truncated. Earlier useful drafts remain available even if a later editor fails. When only some windows succeed, `selected_kind: mixed_partial_edits` and per-stage `contributes_selected_blocks` expose the exact partial selection.

`--fallback-hierarchical` is an explicit opt-in capacity fallback. Without it, a named route stays that route. The report records requested route, effective route, partition, stages not executed, and the exact reason for a blocked stage. `client_invocations` counts client calls, while `paid_dispatch_count` distinguishes a known pre-dispatch zero from uncertain HTTP outcomes; these are not interchangeable billing counts. A conservative byte estimate may trigger a capacity warning even when the model would fit; this is not evidence of the model's actual token capacity.

## Common approved input

All candidates consume the same accepted `CHAPTER_ARRANGEMENT.json`, its `ARRANGEMENT_INPUT.json` (or the matching promoted writer-packet root), source identities, source-material variants, and complete paragraph/table tasks. The builder uses the real existing material consumer instead of a simplified fixture projection. It preserves complete owner task intent, aliases, complementary materials, review-mediated sources, and chapter tool materials. Exact duplicates may be removed; complementary scientific content is retained.

The candidates do not receive human-written scientific conclusions, desired scores, required comparison claims, per-paragraph sentence formulas, or forced output lengths. Tasks state the approved work; material is the evidence. Task IDs are structural provenance, not proof that the writing faithfully executes the science. A block can cover multiple tasks or units, so chapter-level coherence need not be split into artificial unit result files.

The provider response contract is JSON, with or without a Markdown JSON fence:

```json
{"blocks":[{"block_id":"argument_1","task_ids":["UNIT_A::PARAGRAPH_1","UNIT_B::PARAGRAPH_2"],"body_markdown":"A coherent passage with source citations [P0001]."}],"issues":[]}
```

`task_ids` use the builder's catalog keys (`unit_id::paragraph_id` or `unit_id::table_id`). Unknown references, missing coverage, malformed/partial responses, citation diagnostics, and missing actual table Markdown remain visible. A claimed table ID alone does not satisfy a table task. No guessed numeric-citation repair is applied. `complete` means structural and transport completion; every output remains `semantic_quality_unreviewed: true` until separate review.

## Offline preview (default)

Run from the repository root using the project's installed Python environment:

```bash
python scripts/upgrade3/writer_candidates.py \
  --arrangement /path/CH01/CHAPTER_ARRANGEMENT.json \
  --view /path/CH01/ARRANGEMENT_INPUT.json \
  --route chapter \
  --config config/writer_candidates/quality.json \
  --output outputs/writer_candidates/chapter
```

Repeat with `units_edit` and `hierarchical`, using separate output directories and the same input/config. Preview does not construct a provider, read a key file, or create/reserve a budget ledger. It saves exact currently knowable stage messages and request estimates. Editor requests depend on actual draft prose, so a preview reports that dependency rather than pretending to have executed or exactly priced an unseen editor call.

If the sibling arrangement input is absent, replace `--view` with one of:

```bash
--packet-root /path/to/promoted/revision
--packet-root /path/to/pipeline/CURRENT_PLAN.json
--packet-root /path/to/pipeline
```

A `CURRENT_PLAN.json` target is resolved relative to the pointer's directory, never the shell's working directory. Missing/cyclic pointers fail visibly. An explicit view is never silently substituted with a packet root. Keep the arrangement bound to the approved input; a different plan is not automatically scientific approval for a new arrangement.

### Token accounting

The CLI uses an existing `data/tokenizers/qwen3_5_9b/tokenizer.json` when available, or an explicit `--tokenizer /local/path/tokenizer.json`. It never downloads a tokenizer. An explicit missing/broken tokenizer fails rather than silently changing the meter. Without the default asset it reports `utf8_byte_upper_bound` and preserves the full input. Supply the local asset before interpreting conservative capacity rejections as model limitations.

The same counter and effective margin/multiplier are used by stage planning and the real client before dispatch. Local estimates are not provider usage; actual usage is retained separately. Meter identity and mode are recorded in `CLI_CONTEXT.json` and `CLI_RUN.json`, with the effective request meter in stage artifacts.

## Recorded-response execution

```bash
python scripts/upgrade3/writer_candidates.py \
  --arrangement /path/CH01/CHAPTER_ARRANGEMENT.json \
  --route units_edit --output outputs/writer_candidates/units_edit_recording \
  --responses /path/recorded_responses.json
```

The fixture can be a flat stage-to-response object or `{"responses": {...}}`. Stable stage IDs are `writer_chapter`, `writer_unit_001`, `writer_cluster_001`, `editor_chapter`, and `editor_window_001` (with increasing suffixes). A `writer` or `editor` role entry may be used as a fallback inside the fixture. Values can be the block JSON above, a provider envelope with `content`, `complete`, `finish_reason`, and `usage`, or recorded text. Missing entries fail; no provider fallback is possible. `--responses` and `--run` are mutually exclusive.

This executes the actual builder, route, parser, cache, and filesystem consumers, replacing only the model response boundary. It establishes implementation behavior, not comparative model quality. Any recorded usage is historical fixture data; this run's provider cost is zero. Fixtures and live results cannot share an execution output directory or be mixed into one apparently live BODY.

## Explicit local paid execution

Do not run this command unless the local experiment and spending have been approved:

```bash
python scripts/upgrade3/writer_candidates.py \
  --arrangement /path/CH01/CHAPTER_ARRANGEMENT.json \
  --route chapter --output outputs/writer_candidates/chapter_live \
  --config config/writer_candidates/quality.json \
  --tokenizer /local/path/tokenizer.json \
  --run --budget-ledger /path/to/comparison-shared.sqlite --budget-limit 200 \
  --key-file /local/path/qwen-api-key.txt
```

`200` is an example CNY ceiling, not a recommended spend or cost estimate. Use the same ledger path and cap for every compared route/chapter. A new ledger needs an explicit finite positive limit. An existing ledger can supply its persisted cap when `--budget-limit` is omitted; a conflicting new cap is rejected. There is no silent creation of a second budget for editing. The ledger reserves each actual call immediately before dispatch and settles known usage; it does not reserve every possible future stage at once. Unknown/interrupted billing stays visible rather than being labeled free.

The real client is `QwenDirectClient`, with explicit role models, `max_retries=0`, and one credential candidate per call. Streaming and both timeout settings reach the actual transport. Credentials and the key-file path are never written into candidate artifacts.

### Quality and balanced profiles

- Default `quality.json`: Max (`qwen3.8-max`) for both writer and editor; 65,536 answer tokens plus 32,768 thinking tokens, for a 98,304 combined ceiling
- Optional `balanced.json`: Plus (`qwen3.5-plus`) for both roles; 49,152 answer tokens plus 16,384 thinking tokens, for a 65,536 combined ceiling
- Both: `json_mode: false`, `stream: true`, 900-second request/read timeout, 3,600-second overall stream timeout, explicit estimator margin/multiplier

The runtime registry currently allows combined output ceilings of 131,072 for Max and 65,536 for Plus, with a 1,000,000-token context window and a 991,808 input ceiling. Validation uses that registry, including input plus reserved output fitting context. Answer allowance and thinking allowance are distinct from actual written length; combined output capacity is distinct from the CNY spending cap. A high ceiling is permission for a complete answer, never a request to pad the manuscript. Role-specific models are supported, but changing them is a separate experimental condition and must be recorded rather than confused with a route effect.

## Artifacts, recovery, and selection

The requested output directory contains:

- `CHAPTER_BODY.md` and `CHAPTER_RESULT.json`: the explicitly selected chapter output
- `RUN_MANIFEST.json`: current attempt, selected version, input identity, effective profiles, partition, stage lineage, skipped/pending work
- `IMPLEMENTATION_REPORT.json`: inspectable route and execution evidence, with quality limitations
- `CLI_CONTEXT.json`, `CLI_RUN.json`, `cli_invocations/`: execution mode, effective configuration, meter, fixture fingerprint, CLI result and invocation history
- `inputs/`: immutable input snapshots
- `runs/<run_id>/`: each run's reports and unchanged initial draft
- `stages/<stage_id>/<cache_key>/attempt_.../`: messages saved before calling, effective request, raw response, parsed result, usage, and transport evidence

Cache identity includes actual messages, task/material input, effective model profile, code/prompt hashes, route, dependencies, and execution/fixture identity. Repeating a matching run reuses successful stages without a new model call. A raw response saved before a parse interruption can be recovered locally. Failed, partial, or uncertain cached attempts do not automatically rerun; inspect them and the ledger first. `--retry-failed` explicitly permits another attempt, which can incur another charge. A changed scientific input, model profile, prompt/code, dependency, or fixture is a different request; inspect the preview before paid execution.

If a new run fails, an older complete selected chapter is preserved. Always inspect `status`, `current_run_version`, `selected_version`, and `selected_input_matches_current`; a preserved old final does not establish that the new run succeeded. Useful partial prose and original drafts are retained, but not silently promoted to completion.

CLI exit codes: `0` for a written preview, completed candidate, or explicitly requested assembly; `3` for a recorded/live candidate still pending or blocked; `2` for invalid arguments/input or a CLI-level failure. Preview success means its report was generated, not that every planned stage fits or scientific quality was accepted.

## Assemble selected chapters into BODY

This explicit adapter accepts chapter artifacts rather than fabricating legacy `UNIT_RESULT.json` files. Create a manifest with paths relative to that manifest or absolute paths:

```json
{"review_title":"Review draft","chapters":[{"chapter_id":"CH01","title":"First chapter","result_path":"chapter1/CHAPTER_RESULT.json"},{"chapter_id":"CH02","title":"Second chapter","result_path":"chapter2/CHAPTER_RESULT.json"}]}
```

```bash
python scripts/upgrade3/writer_candidates.py \
  --assemble-manifest /path/selected_chapters.json --output outputs/selected_body
```

The manifest is the exact chapter order and explicit selection. The adapter verifies chapter identity, duplicate IDs, adjacent run manifest, selected version, current input match, and completion. Each chapter's route/config and provenance remain in `BODY_RESULT.json`; intentionally selected different routes are permitted and visible. Recording/live mixtures are rejected. The full archived chapter input is verified against its canonical input hash before source identities are merged with the existing candidate identity rules. A reused citation handle with conflicting DOI or canonical paper identity is rejected; equal normalized DOI may legitimately have different historical local record IDs. Missing stable identity is reported explicitly. No source is renumbered. An explicit chapter title is not added twice when it exactly matches the body's leading Markdown heading. This is ordered Markdown assembly, not the legacy bibliography/citation-rewrite delivery pipeline.

Use `--allow-pending-draft` only to export useful pending prose with a visible draft warning. An older complete BODY is retained and the new incomplete export is written as `PENDING_BODY.md` / `PENDING_BODY_RESULT.json`. No useful body is discarded to manufacture completion. `ASSEMBLY_REPORT.json` gives the exact output path and any preserved prior selection.

## Verification and subsequent quality comparison

Run the offline checks before any local paid comparison:

```bash
python -m pytest -q tests/upgrade3/test_writer_candidates_contracts.py tests/upgrade3/test_writer_candidates_engine.py tests/upgrade3/test_writer_candidates_cli.py tests/upgrade3/test_writer_candidates_integration.py
```

The tests cover real input/material consumers, route-specific messages and partitioning, genuine task/table/citation parsing, resume/partial recovery, explicit input/selected-result handoff, stream wire payloads, and shared ledger behavior. Network calls and credentials are disabled or replaced only at the transport/model boundary. Passing these checks is an implementation result.

For the later approved quality experiment, start from the same accepted arrangement and material baseline. Using the same explicit model profiles makes the route comparison easier to interpret; practical model/profile changes are allowed when recorded, without requiring an exhaustive controlled experiment. First compare what each route actually received and executed. Then review the unedited writer draft and the selected edited chapter separately for evidence fidelity, conditions and uncertainty, task coverage, paragraph function, transitions, duplication, and chapter-level argument. Report missing editor stages or fallback partitions alongside the prose. Do not infer quality from token count, amount spent, the number of claimed task IDs, or an offline fixture's polished wording.
