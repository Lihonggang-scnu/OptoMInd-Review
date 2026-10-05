# BODY40 native Windows local acceptance

Runtime: `C:\Anaconda\python.exe` (Python 3.12.4), with `PYTHONHASHSEED=0`, `PYTHONUTF8=1`, and worktree `PYTHONPATH=.`. No credentials, network, paid model call, search/download, or full BODY generation was used.

## Result

- The bounded native control command completed with **537 passed, 2 failed** in 42.27s. The only failures were `test_archived_body_report_and_downstream_numbering` (LF archive bytes vs native CRLF output) and `test_actual_case_batch_reuses_moved_reading_but_invalidates_science_and_settings` (test expects an unescaped Windows path substring inside JSON-escaped payload text).
- The same two test nodes run from an isolated archive of base `e0615b0f83ed001042b12c539a08a316f3ef6ab6` produced the same two failures. This is a native Windows test-portability limitation, not a BODY40 production behavior failure.
- The boundary checker exited 0: AST prompt/order/cache/message builders unchanged, archive unchanged, and five production hashes recorded. Its tracked `SOURCE_AND_BOUNDARY_CHECKS.json` was restored byte-for-byte after capture (`boundary_restored=True`).
- Dedicated identity controls: **17 passed**. Numeric citation and diagnostic handoff controls: **28 passed**.

## Original local raw replay

`replay_cli_acceptance.py` uses the production writer CLI with a recording provider over the original local raw responses. The source roots are the original `body_full_staged_acceptance_20261004_40cny/writer_live` records; generated artifacts are separate under this acceptance root.

- `original_local_Ch6_U4`: original raw SHA-256 `183cf1429d0894c585619eec7f60547edf3ea658fff5bd660408d03d102ee60d`; body equals the production parsed raw body; `[P0011]` count 0; raw `[11]` retained; all 12 numeric citations `[1]` through `[12]` unresolved; 13 citation diagnostics; assembly status `complete`, `problems_resolved=false`.
- `original_local_Ch7_U02`: original raw SHA-256 `ee714eda0694f4911d06bc2776d029947b7ab0e692d5f3b1c7028752094fd2fd`; body equals the production parsed raw body; `[Q01]` occurs 5 times unchanged; `Q01` is recorded as a known tool identifier and one non-source diagnostic; assembly status `complete`, `problems_resolved=false`.
- Both replay cases report `live_calls=0`, `paid_calls=0`, and `network_calls=0`. Original `UNIT_INPUT.json` and `UNIT_MESSAGES.json` paths and hashes are recorded in `native_replay_report.json`. Generated messages are a separate CLI export and are not claimed byte-identical to the original files.

The controlled cross-domain case with a unique complete bibliography title converted `[1]` to `[P0011]` with `bibliography_exact_title` provenance and assembled with `problems_resolved=true`. Explicit caller mappings, ambiguity preservation, and informational nonblocking diagnostics are covered by the 28 dedicated handoff tests.

The historical TACITO passage/context stable identity, future-handle collision, checkpoint invalidation, normal registration, and packet merge controls are covered by the 17 passed identity tests. The second-stop alias material-consumption change remains unimplemented.

## All-29 consumer reassembly

`reassemble_all29.py` copied the exact original final `BATCH_JOBS.json` selection, overriding only `Ch6_U4` and `Ch7_U02` with the original-local replay `UNIT_RESULT.json` paths. Native production assembly loaded **29/29** units, retained all 29 selected jobs, and produced **173** references from **173** used papers. Assembly status is `complete`; `problems_resolved=false` with 5 pending problems (the pre-existing planning/writer issues plus the two replay citation diagnostics). Paid and network calls were zero. The permitted Ch6 textual change is the false `[P0011]` reversion to raw `[11]`; no prose was regenerated.

## Commands and artifacts

```powershell
$env:PYTHONHASHSEED='0'; $env:PYTHONUTF8='1'; $env:PYTHONPATH='.'
C:\Anaconda\python.exe docs/workorders/body-chain-20261003/records/body40_identity_citations/run_controls.py
C:\Anaconda\python.exe docs/workorders/body-chain-20261003/records/body40_identity_citations/verify_boundaries.py
C:\Anaconda\python.exe native/replay_cli_acceptance.py
C:\Anaconda\python.exe native/reassemble_all29.py
C:\Anaconda\python.exe -m pytest -q tests/upgrade3/test_body40_historical_identity.py
C:\Anaconda\python.exe -m pytest -q tests/upgrade3/test_body40_numeric_citation_identity.py tests/upgrade3/test_body40_citation_diagnostic_handoff.py
```

Artifacts are under `F:\OptoMind-Review-2\outputs\body40_identity_citations_local_acceptance_20261005\native\`, including `native_replay_report.json`, `all29_reassembly_report.json`, `ACCEPTANCE_RESULT.md`, raw command logs, replay `UNIT_RESULT.json`/assembly reports, and the generated boundary evidence copy. The production worktree remains clean.
