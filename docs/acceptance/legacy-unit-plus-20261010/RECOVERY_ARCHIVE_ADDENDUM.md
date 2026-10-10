# Legacy-unit-plus recovery archive addendum

`ARCHIVE_CANDIDATE_MANIFEST.json` is the merged uploadable inventory, including the authorized recovery increment. `ARCHIVE_CANDIDATE_INCREMENTAL_MANIFEST.json` is the recovery-only subset and excludes `BUDGET.sqlite` (retained locally; do not upload). This addendum records the authorized `--waive` plus `--run --retry-failed` increment.

## Historical first-stop snapshots

The existing `FINAL_RUN_STATUS.md`, `LIVE_AUDIT.json`, `LEDGER_SUMMARY.json`, `CITATION_AUDIT.json`, `RECOVERY_GUARD_AUDIT.json`, and the old `ROOT_REVIEW.md` are retained as the first 21-unit stop records. Their counts and pending-unit statements are historical and must not be read as the final 29-unit result. The final records are `RUN_REPORT.json`, `RECOVERY_FINAL_AUDIT.json`, and `RECOVERY_CITATION_AUDIT.json`.

## Final run state

- 29/29 units have complete result/seal records. Eight new qwen3.5-plus direct calls were made from Ch6_U1 through Ch7_U04; successful 21 cache attempts were not rerun.
- The historical Ch6_U1 `attempt_001` timeout/error/partial stream is retained. Its replacement is `attempt_002`; the other seven recovered units have one new attempt each.
- The production source worktree remains at `054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512` and clean.
- `RUN_REPORT.json` is `restricted_draft`: all units are assembled, but writer diagnostics remain pending. `ASSEMBLY_SUMMARY.json` has 29 loaded units, three tables/14 data rows, 179 used papers, no unknown citations or table handles, and four pending diagnostic groups. No diagnostic was cleared or rewritten.
- The final actual settled total is 3.4918048 CNY. The original 1.936256 CNY settled total is preserved. The old 1.63964 CNY reservation is `user_waived` with `amount_cny=0` and `actual_cny=NULL`, with the original row and execution authorization summary retained in the ledger audit table and `BUDGET_WAIVER.json` (the verbatim user request is in `USER_RESUME_INSTRUCTION.md`); it is not recorded as settled 0.

## Review records

- `RECOVERY_FINAL_AUDIT.json` / `.md`: process, attempt, body-hash, message/request identity, raw SSE hash, usage, seal, assembly, citation, table, source, and ledger checks.
- `RECOVERY_CITATION_AUDIT.json` / `.md`: final 29-unit seven-chapter DOI/canonical identity and alias de-duplication audit; the older `CITATION_AUDIT.json` remains labeled as the first 21-unit stop snapshot.
- `RECOVERY_FREE_GUARD_CHECK.json` / `.md`: offline temporary-ledger proof that a normal new uncertain row still blocks and a reserve over the 30 CNY cap still blocks, with zero provider calls.
- `TIMEOUT_ROOT_CAUSE.md`: only the proven client-side 1800-second stream-read inactivity timeout is asserted; no server-side internal cause is inferred.
