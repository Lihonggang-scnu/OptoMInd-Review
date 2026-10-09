# Legacy unit Plus readiness checkpoint

Status: FREE_READY_PENDING_ROOT_START

This checkpoint records the no-network preparation only. No `--run` invocation and no paid Qwen call has occurred.

## Source and route identity

- Detached source worktree: `worktree/`, commit `054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512`.
- Input: `input/6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7/FULL_BODY_INPUT.json`.
- Input bytes/hash: 10,099,935 / `6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`.
- Provenance: byte-for-byte equal to the 75 archived parts at `f5103e99c624951ab549a90b443ab090876ca301`; the local raw input was selected, not a public truncated reconstruction.
- Route identity is in `RUN_IDENTITY.json`: book binding `94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa`, code hash `f2c9e5a48767a6b1b6d486d056f402af161656b2c7c6b466e742b5157f693000`, active prompt hash `689de01708c5a663a1ea20198147ccf8255ebba86f9e5f924edcf7fbecade3fd`.
- Actual system message in all 29 previews: 3,805 characters, SHA256 `8de9066e1eb790d0c76c195fa39d542560d24459673a11c621a2b9b1b79351f1`; exact byte/text match with archived 2026-10-05 Ch2_U01 system message.

## Material contract audit

The 29 saved `UNIT_MESSAGES.json` and `UNIT_PACK.json` pairs were inspected from the official CLI preview:

- 7 chapters, 29 units; 92 paragraph tasks; 3 table tasks and 14 table rows.
- All 29 units preserve the input paragraph tasks, table tasks, `owner_unit_context`, and `unit_notes` exactly.
- 566 source-record deliveries; 221 source identities in the closure; 194 direct task-bound identities after alias normalization.
- 328 paragraph/table `source_uses` entries, 12 paragraph tasks carrying `finding_conditions`, 101 chapter-tool-material records, 94 sibling-unit entries, and 174 other-chapter entries were delivered in actual user payloads.
- All 29 payloads include `owner_unit_context` (including intentionally empty contexts where applicable). Alias-bearing records remain present in the delivered source snapshots; chapter-local snapshots are retained.
- No `previous_body`, old-manuscript, actual-written-prefix, or GUIDE field/key/text was found in the 29 author payloads. The payload keyset is the legacy contract: chapter frame, task fields, sibling/other-chapter organization, source records, chapter tools, unit context, and citation map.

## Capacity and budget preview

The official invocation was:

```powershell
python -X utf8 scripts/upgrade3/legacy_unit_writer.py --input "F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny\input\6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7\FULL_BODY_INPUT.json" --output-dir "F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny" --model qwen3.5-plus --budget-limit 30 --tokenizer "F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json"
```

Results are in `PREFLIGHT.json`, `RUN_REPORT.json`, and `TOKENIZER.json`:

- 29 planned units, 0 model calls, 0 capacity-blocked units.
- Tokenizer: local `qwen3_5_9b/tokenizer.json`, SHA256 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`, implementation `0.23.2`.
- Per-unit prompt estimates: 35,168–161,376 tokens; reserved input after the 1.12 multiplier and 8,192 framing margin: 47,581–188,934 tokens.
- Every request uses `qwen3.5-plus`, max output 32,768, thinking budget 8,192, total context 88,541–229,894 tokens, within the 1,000,000-token context window.
- Maximum single-request estimated reservation: 1.738776 CNY (`Ch6_U3`); all 29 maximum reservations sum to 41.509032 CNY. This is diagnostic only; it is not pre-reserved and is not a requirement that the 30 CNY cap cover every maximum simultaneously.
- The dedicated ledger has not been initialized during preview. Planned path for the one official live start and every recovery is `BUDGET.sqlite` in this run root, with its adjacent ownership marker. Existing ledgers are untouched.

## Free verification

- `free_tests/FOCUSED_PYTEST.txt`: 36 passed, 1 failed at the test's own Windows `Path.unlink()` due SQLite file locking (`test_existing_ledger_damage_is_not_a_new_budget[delete]`); no source/test changes were made.
- The preview itself exited 0 with status `preview`, 0 calls, and complete exact request files for all 29 units.

Root acceptance is required before adding `--run` to the same command and same output/ledger/tokenizer paths.
