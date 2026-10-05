# BODY40 bounded identity and citation repair — first stop

Status: **READY_FOR_LOCAL_BODY40_IDENTITY_CITATIONS_01**. Stop for local GPT 6.1 sol acceptance. Second-stop alias-material consumption is not implemented.

Branch: `body40-identity-citations-cloud-20261005`.

Base archive: `e0615b0f83ed001042b12c539a08a316f3ef6ab6`; inherited actual tested source: `7e293b63603157e741ec9b68f30565fcb4065f8a`. The final published source commit is recorded in `PUBLICATION.json` when publication completes. No other branch is changed or merged.

## Decision and concrete changes

The previously reported first-stop causes reproduce. No reinterpretation as a pure model-quality defect is needed. Implementation uses five production files, retaining the original scientific prompts, planning order and material eligibility.

| Seam | Reproduced before | Actual after | Evidence |
|---|---|---|---|
| Foreign historical P labels | A TACITO passage enters a current request as unoccupied P0593; a later current pool assigns P0593 to allopurinol | Passage and paper-context labels come only from the current stable-paper map; otherwise they use stable paper IDs. Useful text stays identical. Normal registration on the archived 594-row stage snapshot assigns TACITO P0595 and retains allopurinol P0593 | `identity/` |
| Tool material binding | Unknown or handle-only historical labels may attach material to an unrelated current paper | Stable paper identity or compatible source-unit provenance is required; contradictory source-unit identity cannot override it. Unresolved handle-only material remains chapter-level with its old label diagnostically retained | Dedicated real binder→packet merge regression; independent review |
| Numeric dialect collision | Real Ch6_U4 raw [11] becomes P0011, although the returned [1]–[12] sequence does not establish that identity | Generated suffix aliases are not identity evidence. Raw body is preserved, no P0011 invented, all 12 numbers unresolved. Explicit caller maps and unique complete full-title evidence still work | `citations/REAL_REPLAY_BEFORE_AFTER.json` |
| Tool marker mistaken for reference | Five [Q01] occurrences in real Ch7_U02 have no citation problem reported | Typed input question metadata establishes Q01 as a non-paper identifier; all occurrences remain unchanged, with a deduplicated diagnostic. No prefix guessing or generic bracket prohibition | Real Ch7 response replay |
| Persistence and assembly | Runtime repair provenance is omitted by CLI; archived Ch6's 11 unresolved references do not enter pending assembly state | Normal and completion reports retain repair origin and ambiguity. UNIT_RESULT, run report and assembly expose unresolved identities. Useful restricted drafts still assemble; successful repair metadata alone does not block | `handoff/` actual emitted results and handle drafts |

An independent review found an additional instance of the same first-stop identity fault: retaining a handle merely because the current pool contains it. This was fixed before publication, with a real binder/merge counterexample. No source material was removed to make tests pass.

### Numeric conversion policy

- Caller-confirmed mapping wins, including an explicit empty map. Model-returned mappings are not trusted.
- Auto-generated numeric suffix aliases may remain in the unchanged input for compatibility; they are not used to infer a model response's numbering system.
- Automatic trailing-bibliography matching uses unique complete titles with only NFC/case/whitespace normalization. Scientific symbols remain significant. Conflicting/unknown duplicate definitions, partial title evidence and ambiguous/mixed automatic numbering preserve the text and report uncertainty.
- Exact local title correspondence is lexical evidence, not independent verification of the paper or scientific claim.
- Unresolved source identity is a pending delivery issue. Ordinary informational diagnostics and successful repairs alone are not blockers. Existing usable prose is never discarded.

## Execution and restoration contract

Read `START_AND_SCOPE.md` for original entry points, actual parameters, recovery history, stage-specific pools and selected final artifacts. The preserved historical order is:

`provisional → level1 tools/outline → routing/proposals → harmonization → level2 tools → scope finalization → needs/chapter tools → details → global coordination and owner revision → formal case append → arrangement → writer → assembly`.

`_post_case_review` remains before case selection. `SOURCE_AND_BOUNDARY_CHECKS.json` checks unchanged planner run/order/cache methods, writer input/message builders, prompt constants and archive files against the fixed base.

### Cache and recovery

The existing local-judge checkpoint contract already includes the actual request payload/source identities. Changing a foreign handle to the stable identity invalidates that specific old successful result; both attempts survive. Repeating the corrected request reuses its corrected checkpoint. No new cache system, forced global invalidation, automated paid retry or stage bypass is introduced.

This is **not** a migration of old model answers containing `TACITO(P0593)` in prose. Higher-level historical planner/owner/writer caches may already embed that error and are not certified safe by rebinding structured source fields. Use the archive only for inspection/replay. For a fresh authorized experiment start from upstream materials with fresh planning outputs; if selectively recovering an old affected run, local review must identify and regenerate the affected answer and its dependent consumers. Do not globally replace P0593: valid allopurinol uses must remain.

No full planning or historical cache-recovery command was run here. The bounded replay scripts always write separate evidence products. Historical final BODY selection remains the explicit BATCH_JOBS list, not a recursive search for whichever UNIT_RESULT is found first.

## Verification and commands

Final bounded aggregate: **539 passed**, `FINAL_CONTROLS.txt`. Untouched-base matching prior control set: **458 passed**, `BASELINE_CONTROLS.txt`. These are overlapping scoped controls, not the complete repository suite and not measures of scientific quality. New dedicated tests reproduce the failures first, then validate actual request content, output text, persisted results and assembly status; see per-seam records for counts and commands.

From repository root, with the normal application dependencies installed:

```bash
PYTHONHASHSEED=0 python docs/workorders/body-chain-20261003/records/body40_identity_citations/run_controls.py
python docs/workorders/body-chain-20261003/records/body40_identity_citations/verify_boundaries.py
```

This cloud executor lacked pytest and the unrelated eager AgentScope runtime facade. Lightweight test dependencies were installed in `/tmp/optomind-body40-deps` from the package registry. The actual cloud command was:

```bash
PYTHONHASHSEED=0 PYTHONPATH=docs/workorders/body-chain-20261003/records/body40_identity_citations/offline_bootstrap:/tmp/optomind-body40-deps:. python docs/workorders/body-chain-20261003/records/body40_identity_citations/run_controls.py
```

The opt-in bootstrap bypasses only `optomind_research.runtime.__init__`; all tested upgrade3 modules, storage, message builders and consumers remain real. It also denies network connections, including subprocesses. This is not a full installed-application integration test. Root compilation and `git diff --check` passed.

An unseeded prior-control run exposed nondeterministic raw JSON key ordering between subprocess messages; parsed content was equal. Fixed PYTHONHASHSEED makes the comparison reproducible without production formatting changes (`BASELINE_ORDER_CHECK.json`). A separately run pre-existing harness-help case uses unavailable `F:/OptoMind-Review-2` as cwd; it remains an environment-limited failure, documented in `handoff/assembly-regression.txt`. No claim that the entire test suite passed.

## Local acceptance and remaining limits

1. Replay the actual Ch6_U4 and Ch7_U02 responses through the repaired local CLI using original local inputs; compare raw text, diagnostic fields, UNIT_RESULT and assembly report. Do not regenerate scientific prose for this check.
2. Check a local historical unoccupied handle through passage/context, corrected local-judge request, normal registration and packet consumption. Compare stable identities and actual useful material, not just source counts. Unknown material remains available without guessed identity.
3. Validate explicit caller maps, complete unique bibliography titles and ambiguous numbers on a second subject. Identity uncertainty should remain visible; informational records should not make otherwise resolved drafts pending.
4. Full BODY quality, native Windows execution in the installed dependency stack, omitted original fulltext requests and higher-level old-cache migration remain unverified. Scientific errors, missing cases and cross-chapter repetition remain downstream work.
5. The additional 40,665-byte Ch6 arrangement raw is not needed for this stop; no new local asset is required for the offline findings. Local exact-provider-message replay still requires omitted fulltext materials on the local machine.

No paid model call, real research search/download, full BODY rerun, scientific manuscript editing, credentials access, protected-branch update or merge occurred. Package installation and Git publication are network operations and are not described as “zero network.” Review-derived original studies retain equal eligibility from usable reported content and citation identity; no own A/B/fulltext requirement is introduced. One sufficient table may still serve several tasks.

Packaging note: pytest log display-only trailing whitespace was stripped for diff-check; assertions and outcomes are unchanged. Body replay files and original archive bytes were not normalized by this packaging step.

Publication boundary: derived manuscript copies are kept locally but excluded from this new commit. REPLAY_BODY_HASHES.json in the parent record directory gives hashes and sizes; existing archived input/raw/result paths and replay tests retain verifiability. Handoff evidence omits body_markdown values with explicit hash/length markers. No omitted text is indirectly re-uploaded.

Failure-log publication: full traceback logs can echo manuscript text, so published failing logs contain only node IDs, outcomes and full-log hashes. Original logs stay local; REPLAY_LOG_HASHES.json records their provenance.
