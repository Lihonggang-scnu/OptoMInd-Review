# Snapshot selection and replay boundary

The public archive replay (`public_archive_Ch6_U4` and `public_archive_Ch7_U02`) was a separate successful consumer check against the worktree's published acceptance copy under `docs/acceptance/body40-20261005`; it is retained only in the private acceptance root and is excluded from this package. It does not establish that the original local request records were selected.

The first original-local attempt then used the stale historical chapter arrangement at `arrangement_repaired/{Ch6,Ch7}/CHAPTER_ARRANGEMENT.json`. It stopped before the recorded provider with `brief_reference_unknown:Ch6_U4:Ch6_U4_P01`; that failed attempt is described in the root review and no failed output is presented as an accepted result. The corrected driver used the final assembly arrangement below.

The corrected driver used these exact selections:

- Ch6_U4 response: `body_full_staged_acceptance_20261004_40cny/writer_live/Ch6/Ch6_Ch6_U4/raw_responses/Ch6_Ch6_U4_20261005T081431.raw`.
- Ch7_U02 response: `body_full_staged_acceptance_20261004_40cny/writer_live/Ch7/Ch7_Ch7_U02/raw_responses/Ch7_Ch7_U02_20261005T082327.raw`.
- Original local input and message records: the corresponding `writer_live/.../UNIT_INPUT.json` and `UNIT_MESSAGES.json` files. Their SHA-256 values are retained in `evidence/native_replay_report.json`; the files themselves are omitted because they contain source material and request context.
- Correct chapter arrangement snapshot for the production CLI reconstruction: `body_full_staged_acceptance_20261004_40cny/body_assembly_final/arrangements/Ch6/CHAPTER_ARRANGEMENT.json` and the analogous Ch7 file. The arrangement view metadata came from `body_full_staged_acceptance_20261004_40cny/arrangement_repaired/{Ch6,Ch7}/ARRANGEMENT_INPUT.json`.
- Correct final assembly selection: the original `body_assembly_final/batch_input/BATCH_JOBS.json`, copied into `all29_reassembly/batch_input/BATCH_JOBS.json`; only the Ch6_U4 and Ch7_U02 `reused_result` paths point to the corrected replay results.

The generated CLI message files are deliberately not claimed byte-identical to the original message files. The driver records both original and generated message hashes and records the byte-equality result as false. This reflects rebuilt local metadata and paths; it does not imply a new model response. The saved raw response body was consumed unchanged: both replay reports show equality with production raw parsing, with Ch6_U4 retaining `[11]` and Ch7_U02 retaining five `[Q01]` markers.

Omitted from this publication stage are raw response files, copied input/message payloads, arrangement/input copies, provider request records, downloaded papers, full-text passages and caches, and any credentials or signed URLs. The package retains result bodies, diagnostics, assembly reports, hashes, and the exact batch-selection record needed to review the acceptance boundary.
