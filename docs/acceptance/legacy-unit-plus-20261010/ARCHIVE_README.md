# Legacy-unit-plus archive index

This file is a recovery index for the proposed archive enumerated by `ARCHIVE_CANDIDATE_MANIFEST.json`. The manifest is an inventory only; it does not copy or upload files.

## Input recovery

- Raw selected book: `F:\OptoMind-Review-2\outputs\guided_body_compact_auto_metadata_20261009_12cny\LIVE\inputs\94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa\FULL_BODY_INPUT.json`
- Raw bytes: 10,099,935; copied run input SHA256: `6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`.
- The raw book is byte-identical to the 75-part archive at source commit `f5103e99c624951ab549a90b443ab090876ca301`, indexed by `docs/acceptance/guide-content-units-20261010/input/FULL_BODY_INPUT.json.parts.json` and reconstructed from the corresponding `FULL_BODY_INPUT.json.part-*` files. The archive intentionally omits a second raw-input copy; use the source path, commit, part index, and SHA above for recovery.

## Included evidence

Actual attempt records are retained under `units/**/attempt_001/`: `UNIT_MESSAGES.json`, `REQUEST.json`, successful `RAW_RESPONSE.json`, `UNIT_BODY.md`, `UNIT_RESULT.json`, `RESULT_SEAL.json`, and the blocked unit's `ERROR.json`, `RUN_ERROR.json`, and partial stream evidence. The native partial assembly is under `assembled/`. `LIVE_AUDIT.json`, `EFFECTIVE_REQUEST_AUDIT.json`, `CITATION_AUDIT.json`, `TABLE_TASK_AUDIT.json`, `LEDGER_SUMMARY.json`, `RECOVERY_GUARD_AUDIT.json`, and `HISTORICAL_REFERENCE_STATS.json` are derived read-only audits.

## Deliberate exclusions

`BUDGET.sqlite`, credentials/key files, the tokenizer data, the source worktree, duplicate input bytes, standalone `UNIT_INPUT.json` material payloads, and duplicate successful SSE captures are excluded. The failed Ch6/U1 partial stream is included because it is the only transport evidence for the uncertain call. `UNIT_INPUT.json` is retained only as a recorded exclusion because its material payload is already represented in the actual request message and separate transmission is unnecessary.

## Outcome

The run has 21 complete units, 22 physical attempts, one uncertain reservation, and eight missing units. See `FINAL_RUN_STATUS.md` and `RUN_REPORT.json` for the authoritative outcome.
