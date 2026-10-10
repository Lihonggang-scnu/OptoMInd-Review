# Legacy Plus recovery final audit

- all_pass: `True`
- PID 3352 alive: `False`
- run: 29/29 complete, 8 new live calls, model `qwen3.5-plus`, no missing units; final status `restricted_draft` because pending diagnostics remain.
- new 8: request/message hash, native qwen3.5-plus profile, timeout values, usage, captured SSE raw-response ledger hashes and result seals all pass: `True`. Ch6_U1 retains historical attempt_001 and new attempt_002; the other seven have one new attempt.
- old 21: one attempt each and baseline body hashes unchanged: `True`. Historical Ch6_U1 failure remains with `ERROR.json`/partial and no result or seal.
- assembly: 29 loaded, 3 tables/14 data rows, 179 used papers, no unknown citations/table handles; deterministic production-normalized body text is present for all 29 units. Pending diagnostics: `4`; no diagnostic was cleared.
- references: `179` unique canonical DOI/identity references, `224` normalized handle/alias mappings, `221` catalog papers (`179` used, `42` unused).
- ledger: 29 settled, 1 user-waived with `actual_cny=NULL`, 0 uncertain; actual settled total `3.4918048` CNY; old settled total `1.936256` CNY.
- archive: `ARCHIVE_CANDIDATE_MANIFEST.json` now includes `292` uploadable inventory files and points to `ARCHIVE_CANDIDATE_INCREMENTAL_MANIFEST.json`; database included `False`; waiver JSON included `True`.
- source HEAD `054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512`, dirty `False`.

Per-unit and per-file details are in `RECOVERY_FINAL_AUDIT.json`.
