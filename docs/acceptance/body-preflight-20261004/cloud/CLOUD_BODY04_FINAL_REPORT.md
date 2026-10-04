# BODY04 bounded acceptance result

Result: the three-call local chain completed. This is one local owner adoption, arrangement, and writer chain.

## Changed

- Owner stage: `updated`; arrangement: `arranged`; writer: complete.
- Managed handle: `P0602`, assigned by production `_canonicalize_feedback_materials`; raw candidate entered without a handle.
- Material: Pushalkar et al. (2018), R21, from `<PROJECT_ROOT>\outputs\cloud_body02_real_acceptance_20261003\runs\live\reader\paper1_initial\directed\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\dr-task-0a3c29a5c78a27910afc4081\DIRECTED_READING.json`.
- Scope: original CH02 packet 432 rows cropped to six U3 rows, then seven after owner adoption.
- Candidate used production `deep_read_material` with A/B omitted; finding and conditions survived to writer material without truncation.

## Verify

- Actual added cost: `0.1018188 CNY`; shared ledger total after: `0.7121084 CNY`; occupancy `0`; no uncertain rows.
- Actual usage: owner 53,056 prompt + 2,624 completion; arrangement 6,383 + 2,431; writer 39,154 + 2,713. Each had 1,024 reasoning tokens.
- Each client used `max_retries=0`, `max_keys=1`; no retry or key rotation.
- Offline replay passed 11/11: six source rows plus raw candidate -> saved owner response -> seven rows; candidate digest unchanged and writer message gained P0602.
- Tool-only message and unit isolation evidence: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\offline_replay\OFFLINE_REPLAY_AND_TOOL_ONLY_AUDIT.json`.

## Artifacts

- Live report: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\LIVE_CHAIN_REPORT.json`
- Owner: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\OWNER_RESULT.json`, `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\OWNER_MESSAGES.json`
- Formal packet: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\FORMAL_PACKET_AFTER_OWNER.json`
- Arrangement/writer: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\CHAPTER_ARRANGEMENT.json`, `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\UNIT_MESSAGES.json`, `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\live_u3\WRITTEN_BODY.md`
- Offline replay: `<PROJECT_ROOT>\outputs\cloud_body04_acceptance_20261004\runs\offline_replay\OFFLINE_REPLAY_AND_TOOL_ONLY_AUDIT.json`

## Blockers / notes

- No engineering blocker. Root should retain scientific prose review notes already recorded for causal and clinical overstatement.
- `max_material_chars_per_source=2400` truncated six pre-existing card channels; P0602 was not truncated and its finding/conditions were preserved.
