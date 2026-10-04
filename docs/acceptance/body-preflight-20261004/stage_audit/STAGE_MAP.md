# BODY full preflight stage map

Inspection target: `<PROJECT_ROOT>\outputs\lihonggang_full_acceptance_20261001_50cny\worktree`

Inspected commit: `1f171c4731f27f2f4d20107d37a05b805582529c` (`body07-local-acceptance-20261004`)

Inspection date: 2026-10-04 (Asia/Shanghai)

This is a read-only map of the current code and existing bounded evidence. No model call, provider/network call, credential read, paid execution, production edit, commit, or push was performed. `CODE-CONFIRMED` means the order/callable is read from the current HEAD. `OFFLINE-EXERCISED` means a bounded fixture or short chain exercised the boundary; it does not mean the current full BODY run passed. `FULL-EXPERIMENT-REQUIRED` means a current-HEAD full or live path remains unverified.

## Accepted current BODY order

The `planning_revision` branch in `ProgressiveReviewPlanner.run` establishes this order:

1. Load and validate pool/plan, refresh source handles, establish/resume run state.
2. `provisional_scope` model stage.
3. Level-1 tool cycle and material merge.
4. `level1_outline` model stage.
5. Full-pool source routing and cached chapter proposals.
6. `harmonized_scope` model stage.
7. Level-2 tool cycle and material merge.
8. `finalize_chapter_scope` model stage.
9. Optional chapter-need analysis and chapter retrieval tool cycle.
10. Per-chapter detail generation, including bounded oversized-source handling and optional explicit recovery.
11. Late source routing and late-material attachment.
12. Pre-case `whole_plan_improvement` coordinator stage.
13. Affected chapter owner revisions; successful owner outputs replace the authoritative chapter detail records.
14. `case_groups` collection for each chapter and routed batch.
15. Case attachment directly into formal `supporting_studies` in `planning_revision` mode, mapping `proposed_use` to `contribution` when required.
16. Final planner assembly and `writer_packets` export.
17. Separate per-chapter arrangement from finalized packets.
18. Separate per-unit writer calls from validated arrangements.
19. Deterministic batch assembly from complete unit outputs.

There is no current single command that performs steps 17–19 after the planner. The planner entry and the arrangement, writer, and assembly entries are separate. `run_review_harness.py` remains the formal project harness, but its general help/live path was not used here; existing bounded records report an unrelated `cv2` import limitation in that environment.

The restoration document `docs/BODY_CASE_CHAIN_RESTORATION_20261002.md` is partly historical. Its order statement agrees with current code, but its prose that cases require an attached A/B or deep-read result is not a safe universal admission rule for the later policy. Current code accepts a candidate only when `_attach_case_groups` can build usable material (`build_local_material_payload` / `_owner_material_has_content`); `_owner_material_has_content` is a presence check rather than a quality score, and review-derived findings use the same admission path without a down-rank. A forwarded study still needs usable substance; identity alone cannot pass. This map therefore treats material availability as the gate and does not impose the old A/B/deep-read-only gate. The `_attach_case_groups` docstring still mentions A/B/deep material, so that prose is stale relative to the helper's explicit review-derived finding aliases.

## Stage-by-stage map

| # | Stage and current callable | Current artifact(s) | Check and expected state | Observation boundary | Evidence status |
|---:|---|---|---|---|---|
| 0 | `scripts/upgrade3/progressive_review_plan.py:main` → `preflight` (without `--run`) | Sibling `<output-dir>.PREFLIGHT.json`; console budget/context report | Pool/plan load; local token estimate; missing card paths; context under 1M; no provider call; state is `preflight`, not a plan | Input scope and local sizing only; no model understanding or handoff | CODE-CONFIRMED; CLI-only check still required |
| 1 | `ProgressiveReviewPlanner.run` → `_model_stage("provisional_scope")` | `stages/provisional_scope.json`; `RUN_STATE.json.stage_inputs.provisional_scope` and `.stage_cache_contracts.provisional_scope` | Complete response, topic and planning revision match, `RUN_STATE` advances | First model understanding of topic/pool; compare input pool and returned scope | CODE-CONFIRMED; FULL-EXPERIMENT-REQUIRED |
| 2 | `_tool_cycle(phase="level1")` with supplement/direct runners | Level-1 tool stage files, source snapshots/handles, need journal and material index | Query/refinement, acquisition/card/judge, directed reads and merges retain material status and handles; partial work remains partial | Upstream retrieval fidelity: requested need → acquired material → planner-visible row | CODE-CONFIRMED; retrieval seam OFFLINE-EXERCISED with synthetic providers |
| 3 | `_model_stage("level1_outline")` | `stages/level1_outline.json` | Outline response cites known source/material identities; state advances | Model understanding after first material/tool pass | CODE-CONFIRMED; FULL-EXPERIMENT-REQUIRED |
| 4 | `_route_sources` over full pool/batches; `_propose_chapters` | `stages/source_routing/` and `source_routing_summary.json`; `stages/chapter_proposals/*.json`, merged `chapter_proposals.json` | Every routed item has a stable identity and chapter assignment; proposal cache contract matches current packet/pool | Handoff from global scope to chapter-specific material/need | CODE-CONFIRMED; routing/material construction OFFLINE-EXERCISED in short chain; full pool unverified |
| 5 | `_model_stage("harmonized_scope")` | `stages/harmonized_scope.json` | Shared thesis/scope/argument and chapter list are complete and internally consistent | Global-to-chapter interpretation handoff | CODE-CONFIRMED; FULL-EXPERIMENT-REQUIRED |
| 6 | `_tool_cycle(phase="level2")` and `_model_stage("finalize_chapter_scope")` | Level-2 tool artifacts; `stages/finalize_chapter_scope.json`; final shared outline/scope/argument | Material deltas are merged; final chapter IDs and unit identity contract are stable | Final owner input preparation; separates material change from model revision | CODE-CONFIRMED; tool seam OFFLINE-EXERCISED; full level-2/live unverified |
| 7 | Optional `_model_stage("chapter_need_analysis")` and `_tool_cycle(phase="chapters")` | Chapter need artifacts, chapter tool snapshots/index rows | Only present gaps trigger reads; answered/partial/unresolved status is preserved and source handles resolve | Retrieval handoff into chapter detail input | CODE-CONFIRMED; synthetic short chain exercised; full branch conditional/unverified |
| 8 | `_chapter_details` per chapter; `_chapter_details_adaptive_record` only when capacity fails | `stages/chapters/<CH>.json` (and chapter markdown where enabled), adaptive batch cache/failed seams | `_chapter_details_capacity` counts rendered message tokens with `planner.counter`, applies `ceil(tokens * TOKEN_MARGIN_MULTIPLIER) + TOKEN_FRAMING_MARGIN`, then adds capped output (`min(planner.output_tokens,16000)`) and fixed `thinking_tokens=2048`; fit requires estimated input `<= MAX_INPUT_TOKENS` and total `< 1,000,000`. If over, complete source rows are weighted/partitioned until every batch fits, each batch is called/cached, then a merge payload is capacity-checked and called/cached. Fixed-envelope, single-record, or merge overflow fails before/at that seam. | Chapter model understanding and source-to-detail handoff; compare every batch/merge input and output to detect lost caveats or source handles | CODE-CONFIRMED; oversized branch code-traced; FULL-EXPERIMENT-REQUIRED |
| 9 | Explicit `recover_compatible_chapter_details` when configured | `stages/chapter_recovery.json` plus recovered chapter records | Recovery is explicit (`--recover-chapters-from` / `--recover-chapter`), compatibility checked; no implicit historical recovery | Recovery handoff; compare recovered packet identity/material signatures | CODE-CONFIRMED; not current full exercised |
| 10 | `_route_late_sources` → `_attach_late_route_materials` | Late routing/attachment stage artifacts; changed-material summary | Late rows are attached to the matching chapter/detail; `late_material_changes` records the delta | Last upstream material handoff before owner coordination | CODE-CONFIRMED; late route/material carryover OFFLINE-EXERCISED; full unverified |
| 11 | Planning-revision `_post_case_review(... case_record={"response": {}})` → `_stage("whole_plan_improvement")` | `stages/whole_plan_improvement.json`, owner feedback/global improvement | Coordinator sees final pre-case material, chapter records and late changes; status complete/cache contract matches | Global understanding before any formal case attachment; this is the owner-before-cases boundary | CODE-CONFIRMED; pre-case owner branch exercised in offline reading short chain; live quality unverified |
| 12 | `_post_case_review` → `revise_one` / owner cache contract; `_apply_improvements` | `stages/affected_chapter_revision/<CH>.json`, `successful/<CH>.json`, history, merged `affected_chapter_revision.json` | Only complete successful owner revisions replace authoritative details; failed/unresolved revisions remain visible; source materials and identity map are carried forward | Handoff from coordinator to chapter owner; compare owner input materials against final chapter packet | CODE-CONFIRMED; owner persistence/cache/resume OFFLINE-EXERCISED; current full owner coverage unverified |
| 13 | `run_case_groups()`; per-chapter routed batches and `_stage("case_groups", ..., resume=False)` | Case stage files, per-batch records/cache, case response and errors | Collector runs on every resume; inner complete batch caches require chapter/batch/route/unit-material/task signatures; failed/partial batches are retried and retained | Case generation boundary; distinguish case model output from missing upstream material | CODE-CONFIRMED; case input construction and deliberate boundary stop OFFLINE-EXERCISED; no current full case model run |
| 14 | `_attach_case_groups(... planning_revision=True, body_case_additions=True)` | Updated chapter details; `supporting_studies`, `contribution`, source material/identity maps; final case-enrichment summary | Each addition has usable source material and stable identity; direct formal attachment after the owner-revised plan; no `case_suggestions` owner adoption step and no post-case owner rewrite | Owner-revised plan → case output direct append. New cases receive no second owner approval; inspect case inputs/outputs and later writer use for quality and caveat preservation | CODE-CONFIRMED; historical current-policy replay/short-chain evidence exists, but full current run unverified |
| 15 | `_assemble_final` and `_write_final_outputs` | `writer_packets/<CH>.json/.md`, `DETAILED_REVIEW_PLAN.json/.md`, `RUN_STATE.json` | All chapter details assembled; status `complete` only when unresolved owner work does not block it; `initial_draft`/partial remains visible on case-stage problems | Final planner handoff to arrangement; compare chapter source catalog and unit identity contract | CODE-CONFIRMED; packet merge/view construction OFFLINE-EXERCISED; current full planner unverified |
| 16 | `chapter_arrangement.build_chapter_view` → `arrangement_payload` → `run_arrangement`/`validate_arrangement` | Per chapter `ARRANGEMENT_INPUT.json`, `CHAPTER_ARRANGEMENT.json/.md`, `INPUT_SOURCE_USAGE.json`, `SOURCE_USAGE.json`, raw response, `ARRANGEMENT_RUN.json` | Every existing paragraph/task ID is referenced or explicitly unused; source briefs/details/conditions preserved; unknown handles, missing placement, duplicate tasks, identity changes become validation issues | Planner packet → arrangement model input; this is a separate understanding/organization handoff | CODE-CONFIRMED; input/message/validator/exporter exercised offline; current live arrangement unverified |
| 17 | `review_unit_writer.build_unit_view` → `unit_payload` → `run_unit_writing` / `write_unit_output` | `UNIT_INPUT.json`, `UNIT_MESSAGES.json`, `UNIT_RESULT.json`, `UNIT_BODY.md`; raw response | Unit identity and task IDs match arrangement; source material/caveats and table tasks are present; missing/unknown citations and pending table issues persist | Arrangement → writer generation boundary; compare actual message with `UNIT_RESULT` and body citations | CODE-CONFIRMED; writer messages/output exercised with controlled responses; current live writer unverified |
| 18 | `full_review_draft.load_manifest`/`collect_documents` → `assemble_documents` | `MANIFEST.json`, `BATCH_JOBS.json`, `ASSEMBLY_SUMMARY.json`, `RUN_REPORT.md`, handle draft, numeric draft/references | All requested units load; simulated/fake outputs rejected; identity and citation handles resolve; no missing units unless diagnostic `--allow-partial` | Writer outputs → deterministic final document; no new model understanding occurs here | CODE-CONFIRMED; assembly exercised offline; current full batch unverified |
| 19 | Optional `review_delivery` history/plan delivery | Delivery run records and reassembled draft | Replay-only or history import; missing recorded response is pending; no planner or live client | Downstream delivery only; not a substitute for current full BODY execution | CODE-CONFIRMED; bounded delivery exercised offline; outside full BODY acceptance |

## Material-to-input-to-generation checks

The run should record these as separate observations for every chapter and unit:

1. **Material acquisition:** source handle, locator/card, material status, content/limitation/conditions, tool or deep-read provenance, identity aliases, and unresolved question. A candidate with only a title/identity and no usable material must not pass `_owner_material_has_content` into formal case attachment.
2. **Planner/owner input:** chapter packet and owner revision messages must contain the material row and its stable identity. A source can be present in the pool while absent from a chapter packet; those are different states.
3. **Arrangement input:** `ChapterView.arrangement_payload` compacts the packet into task/source opportunities. It preserves source briefs, point/development, conditions, task IDs, and shared argument; full material remains in the local catalog. Record `ARRANGEMENT_INPUT.json` and `INPUT_SOURCE_USAGE.json` before calling the model.
4. **Writer input:** `build_unit_view` resolves arrangement uses back to the packet/source catalog and emits material per source, task source briefs, caveats, and chapter tools in `UNIT_MESSAGES.json`. This is the last inspectable input before prose/table generation.
5. **Generated output:** compare raw response, `UNIT_RESULT.json`, `UNIT_BODY.md`, and assembly citation report. A material reaching an input proves handoff, not that the model used its caveat or finding in prose.

## Oversized chapter input: current implementation

The current HEAD uses `_chapter_details_capacity` and `_chapter_details_adaptive_record`; this inspection found no basis to name it an older “tournament” implementation or assign it a historical SHA. The branch is explicit:

- It renders the actual chapter-details messages with the planner counter. On this HEAD, `MAX_INPUT_TOKENS=991,808`, `TOKEN_MARGIN_MULTIPLIER=1.12`, and `TOKEN_FRAMING_MARGIN=8,192`; `estimated_input = ceil(message_tokens * 1.12) + 8,192`. It adds `output_tokens = min(planner.output_tokens, 16,000)` and `thinking_tokens = 2,048` (the chapter-details call setting). The request fits only when `estimated_input <= 991,808` and `estimated_input + output_tokens + thinking_tokens < 1,000,000`.
- If the normal payload fits, it makes one ordinary chapter-details call. If it does not, it first checks the fixed envelope with no source rows. It then weights each complete source row using the same counter, estimates an initial batch count from the available input budget, partitions in source order toward equal weight, and increases the count until every batch fits. A single source row that still exceeds capacity raises `chapter_details_source_record_exceeds_capacity`.
- Every batch is persisted with a signature containing the v2 contract, model, output limit, thinking limit, and rendered messages. The merge payload contains the batch chapter plans and source handles, clears the original source/candidate lists, and is capacity-checked with the same input/output/thinking accounting before a merge call. Merge overflow raises `chapter_details_merge_context_preflight_exceeded`.
- Failed batch/merge seams are written under `failed_seams`; successful batch/merge caches are reused only under matching signatures and substantive-response checks. This is code-traced; no current full oversized chapter call was run.

## Resume and cache audit

`ProgressiveReviewPlanner._stage` reuses a completed stage only when `resume=True`, the stage path exists, and the supplied cache contract matches. It writes `RUN_STATE` as `in_progress` before invocation and persists the result, inputs, contract, and completed stage afterward. Stage calls without `cache_inputs` do not get a contract-based old-output shortcut. The `case_groups` wrapper deliberately passes `resume=False` on every planner resume; its inner batch cache is allowed only when chapter/batch/route/unit-material/task signatures match, so failed batches remain retryable.

Owner revision caches require the owner input/material/identity contract and preserve only successful complete results. Arrangement reuses only completed validated results with matching input/config signatures; `--force` bypasses that cache. Standalone writer output has no full BODY cache contract in its CLI, so a batch driver must make its unit manifest and output root explicit. Assembly rejects fake/simulated unit outputs and fails missing units by default.

The planner cache contract is versioned and hashes effective inputs, live prompt text, model, output limit, and thinking limit, but it does not include the repository commit SHA. A current run can therefore accept an old result when those effective fields happen to match even if implementation code changed elsewhere. This is the main cache-related operational risk. Before a full run, use a fresh run root or record the exact `--resume` root and verify `RUN_STATE.json`, stage contracts, source/material signatures, and output commit. Do not point a current run at historical `case_chain_restore_20261002` or body07 fixture outputs as if they were current execution. Historical/fixture artifacts are evidence of boundaries only; they cannot silently satisfy a missing current stage unless an operator deliberately supplies that root and matching contract.

## Proposed command sequence

Placeholders below are intentional. They avoid reading keys or choosing an unapproved budget. The first command is offline; commands with `--run` require the parent-authorized bounded/live budget and key handling.

```text
# A. Offline planner preflight (no model/network call)
python scripts/upgrade3/progressive_review_plan.py \
  --preflight --pool <POOL_JSON> --plan <PLAN_JSON> --output-dir <BODY_RUN> \
  --topic-id <TOPIC_ID> --planning-revision \
  [--local-material-index <LOCAL_INDEX>] [--deep-read-limit <N>] \
  [--prior-reading <READING_ROOT>]

# B. Full planner, only after the approved bounded run is ready
python scripts/upgrade3/progressive_review_plan.py \
  --pool <POOL_JSON> --plan <PLAN_JSON> --output-dir <BODY_RUN> \
  --topic-id <TOPIC_ID> --planning-revision --run \
  --key-file <KEY_FILE> --budget-ledger <LEDGER> --budget-limit-cny <LIMIT> \
  --chapter-workers <N> --reader-workers <N> --deep-read-limit <N> \
  [--local-material-index <LOCAL_INDEX>] [--no-external-supplement] \
  [--resume]

# C. Arrangement after DETAILED_REVIEW_PLAN/writer_packets are finalized
python scripts/upgrade3/chapter_arrangement.py \
  --all-chapters --packet-root <BODY_RUN> --output-root <ARRANGE_ROOT> \
  --planning-revision --run --model <ARRANGEMENT_MODEL> \
  --key-file <KEY_FILE> --budget-ledger <LEDGER> --round-cap-cny <CAP> \
  [--force]

# D. Writer, once per unit (the CLI accepts repeatable --unit; no full-body writer driver exists)
python scripts/upgrade3/review_unit_writer.py \
  --arrangement <ARRANGE_ROOT>/<CHAPTER>/CHAPTER_ARRANGEMENT.json \
  --unit <UNIT_ID> --packet-root <BODY_RUN> --output-root <WRITER_ROOT> \
  --planning-revision --run --model <WRITER_MODEL> \
  --key-file <KEY_FILE> --budget-ledger <LEDGER> --global-budget-cny <LIMIT>

# E. Deterministic assembly; check first, then assemble without --allow-partial
python scripts/upgrade3/full_review_draft.py \
  --manifest <BATCH_ROOT>/MANIFEST.json --batch-root <BATCH_ROOT> \
  --output-root <ASSEMBLY_ROOT> --check-only
python scripts/upgrade3/full_review_draft.py \
  --manifest <BATCH_ROOT>/MANIFEST.json --batch-root <BATCH_ROOT> \
  --output-root <ASSEMBLY_ROOT>
```

`review_feedback_loop.py --run` is a bounded owner→arrangement→writer seam exercise with three controlled calls. It is useful for the parent’s limited live sample, but it does not perform the planner’s retrieval, owner-before-case order, formal case attachment, full chapter set, or assembly manifest.

The four current-HEAD downstream CLI help checks (`progressive_review_plan.py`, `chapter_arrangement.py`, `review_unit_writer.py`, and `full_review_draft.py`) exited 0. They verify parser/entry continuity only; they do not exercise a model or claim a full BODY rehearsal.

## Existing evidence classification

- `docs/workorders/body-chain-20261003/records/body07/reading/RECORD.md`: offline production-boundary reading/owner/cache/resume chain, baseline `841e914`; no cases or full BODY.
- `.../retrieval/RECORD.md`: six synthetic retrieval/material/case-input and arrangement/writer-input chains; the probe intentionally stops at the first case model boundary; no generated case result or post-case review.
- `.../writer/RECORD.md`: five synthetic owner→arrangement→writer→assembly boundary chains, controlled responses, 0 paid; no current full planner or scientific quality claim.
- `docs/BODY_CASE_CHAIN_RESTORATION_20261002.md`: historical order restoration and zero-call material replay; the order agrees with current code, but it is not a current HEAD full execution and its old A/B/deep-read wording must not be used as the sole admission policy.
- Historical outputs under `outputs/.../case_chain_restore_20261002`: retain as provenance only; do not count as current HEAD stage execution.

The only complete acceptance evidence still needed is a bounded, explicitly authorized current-HEAD run that reaches the chosen planner stages, records the actual case batches and owner handoff, then runs enough finalized packets through arrangement, writer, and assembly to establish the intended full-run observation points. This map deliberately does not claim that evidence exists.
