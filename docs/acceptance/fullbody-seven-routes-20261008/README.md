# Full BODY seven-route archive (2026-10-08)

This is the incremental public archive for the seven full-BODY writer routes. The first publication contains the four advanced routes and the zero-charge plain_whole capacity decision; this update adds the chapter-concat receipt, chapters 1–3 bodies/raw returns, chapter 4 timeout, and hierarchical precondition rejection. The plain rows continue to update incrementally as their independent worker produces results; the archive is intentionally usable before all seven routes finish.

The advanced run is locked to source commit `344f21b39dc68708ffafc1513aec2577dc29c43c`, input manifest SHA-256 `98e1ee62c4bf239fc54ed133d57c8c6c666b7231fd86c6e2065e79fcddf777a7`, and the Plus-first configuration SHA-256 `6c20021eee3a46fd4d61f4802ad47dc3384d2644c17745f181c432313bbe7b62`. Its directory name retains the local historical `40cny` label; the final advanced cap is 60 CNY. The plain route source is commit `50cc633795fa28b7a42fea96b9946931f49e3ddd` and has a separate 40 CNY ledger.

All included files are public copies. Model bodies, generated plans, actual request/response records, usage, route manifests, reviews, and provenance are retained as far as practical. Raw SQLite ledgers remain local; `ledger/advanced_budget_ledger.json` and `.csv` are sanitized exports. PDF files and rights-bound full-source passages are not included. One complete generated BODY input snapshot is retained under `plain_whole/FULL_BODY_INPUT.json` after key-level inspection found no `full_text`, `rawpaper`, PDF, or copyright fields; duplicate route copies are omitted. The local originals remain at `F:\OptoMind-Review-2\outputs\fullbody_writer_local_20261008_40cny` for authorized audit.

Credential values, auth headers, API-key paths, signed URLs, email addresses, and other personal data are redacted as `[REDACTED_*]`. Relative route paths and SHA-256 values are retained for reproducibility. `PUBLIC_FILE_INDEX.json` gives the public file list, byte sizes, and hashes; `ROOT_RESULT_MANIFEST.json` gives the original 98-file advanced selection and source hashes.

## Route index

| Route | Budget | Status at this publication | Calls | Settled CNY | Reserved / uncertain CNY | Source | Public result |
|---|---|---|---:|---:|---:|---|---|
| `whole_author` | advanced 60 CNY | capacity blocked before paid call | 0 | 0 | 0 / 0 | `344f21b39dc68708ffafc1513aec2577dc29c43c` | `preview_whole_author/` |
| `continuous_author` | advanced 60 CNY | completed | 7 | 12.753632 | 0 / 0 | `344f21b39dc68708ffafc1513aec2577dc29c43c` | `live_continuous_author/` |
| `workbench` | advanced 60 CNY | `pending_first_window_unaccepted`; first window retained but accepted-window count is zero because the required table was omitted | 1 | 2.682624 | 0 / 0 | `344f21b39dc68708ffafc1513aec2577dc29c43c` | `live_workbench/` |
| `reader_revision` | advanced 60 CNY | partially completed; two revisions applied, second anchor batch failed | 3 | 5.709892 | 0 / 0 | `344f21b39dc68708ffafc1513aec2577dc29c43c` | `live_reader_revision/` |
| `plain_whole` | plain 40 CNY | capacity blocked before paid call; preview only | 0 | 0 | 0 / 0 | `50cc633795fa28b7a42fea96b9946931f49e3ddd` | `plain_whole/` |
| `chapter_concat` | plain 40 CNY | partial; chapters 1–3 complete and chapter 4 timed out without response or usage | 4 | 5.316296 | 0 / 3.330940 | `50cc633795fa28b7a42fea96b9946931f49e3ddd` | `plain_chapter_concat/` |
| `hierarchical_full` | plain 40 CNY | blocked_requires_complete_draft; partial independent draft rejected before paid dispatch | 0 | 0 | 0 / 0 | `50cc633795fa28b7a42fea96b9946931f49e3ddd` | `plain_second_batch/HIERARCHICAL_PREVIEW_REJECTION.json` |

Advanced settled total is 21.146148 CNY, with 38.853852 CNY remaining from the 60 CNY cap and no reserved or uncertain amount. The plain ledger has 5.316296 CNY settled and 3.330940 CNY uncertain from the independent chapter run, leaving 31.352764 CNY available; the hierarchical route was blocked before paid dispatch.

## Reading guide

Start with `ROOT_FULLBODY_TEST_REPORT.md`, `ROOT_CONTINUOUS_REVIEW.md`, `ROOT_WORKBENCH_REVIEW.md`, `ROOT_READER_REVISION_REVIEW.md`, and `ROOT_BODY_DIFF.md`. Then compare each route's `FULL_BODY.md`, `FULL_BODY_RESULT.json`, `RUN_MANIFEST.json`, stage request/response/messages, and `USAGE.json`. `preview_whole_author` records a real capacity decision with no paid response. The advanced preview directories include public run summaries and model plan records while omitting duplicated source-input caches; one complete generated input snapshot is retained for the plain_whole capacity record. The second batch's `ROOT_CHAPTER_CONCAT_REVIEW.md`, `PLAIN_CHAPTER_CONCAT_RUN_RECEIPT.json`, `ROOT_PLAIN_TEST_REPORT.md`, and `HIERARCHICAL_PREVIEW_REJECTION.json` document the plain outcomes.

The archive records what actually happened. A capacity block is not a model failure, a partial route is not a complete BODY, and a precondition rejection is not a paid model failure. Chapter 4's timeout remains explicit with its request, messages, error, and zero-usage record.

Large text artifacts are published as UTF-8 canonical `.part-NNNN` files with adjacent `.parts.json` manifests. Reassemble the listed parts in order to recover the original file; each manifest records the original byte length and SHA-256. The GitHub connector may add one trailing CRLF to a text blob; `VERIFY_CANONICAL_PAYLOADS.py` accepts that transport trailer, strips only the exact extra CRLF, and verifies the canonical `PUBLIC_FILE_INDEX.json` and reassembled hashes. This keeps every Git blob below hosting and connector transfer limits while retaining complete model messages.

