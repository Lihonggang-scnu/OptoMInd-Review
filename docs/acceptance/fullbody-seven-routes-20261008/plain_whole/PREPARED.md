# Plain full-BODY preparation (independent 40 CNY campaign)

- Source commit: `50cc633795fa28b7a42fea96b9946931f49e3ddd` (detached worktree).
- Immutable input: `SOURCE_INPUTS.json`, SHA256 `98e1ee62c4bf239fc54ed133d57c8c6c666b7231fd86c6e2065e79fcddf777a7`.
- Scope: Ch1–Ch7, 29 units, 95 tasks; generated full input has 286 source records, 221 source identities, 3 aliases. 240/241 manifest files exist; the single absent path is the known formal packet `chapter_arrangement/ID_MAP.json`, while the full input remains readable.
- Tokenizer: `F:\OptoMind-Review-2\data	okenizers\qwen3_5_9b	okenizer.json`, SHA256 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`.
- Config: `config/fullbody_writer/plus_first.json`; no production edits.

## Offline checks

`OFFLINE_TEST_RESULT.json`: 219 passed, 6 failed. Three failures are Windows long-path classification. Three plain archive replay failures stop at the fixture assertion that at least one source file is absent; in this environment all prepared archive source rows exist. Detail: `OFFLINE_ARCHIVE_REPLAY_FAILURE_DETAIL.json`. No source fix or paid call was made.

## Previews

`PLAIN_PREVIEW_EVIDENCE.json` records the exact preview hashes and stage requests.

- `plain_whole`: 0 calls; capacity blocked; 1,367,491 reserved input tokens, max estimate 7.042828 CNY.
- `chapter_concat`: 7 planned stages, all 95 tasks, all stages fit; summed maximum estimate 23.737700 CNY. No call or ledger reservation exists.
- `hierarchical_full`: deferred until a complete live `chapter_concat` draft exists; no draft is fabricated and no paid call is authorized yet.

## Next authorized step

After root approval, create `plain_only_budget.sqlite` in this campaign with absolute cap 40 CNY, then run the official `chapter_concat` command using the same input/config/tokenizer and direct Qwen key path. Do not start `hierarchical_full` until the complete live concat result is verified and its draft is passed explicitly with `--draft`.

