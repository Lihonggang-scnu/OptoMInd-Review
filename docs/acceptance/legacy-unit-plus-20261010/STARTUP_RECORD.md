# Legacy unit Plus live startup record

Run authorization: root accepted `READINESS_CHECKPOINT.md`; one complete live run authorized. No automatic `--retry-failed`, no model switch, no Max, no previous-body prefix, no GUIDE input, and no source edits.

Source worktree: `worktree/`, detached commit `054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512`.

Input binding: `input/6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7/FULL_BODY_INPUT.json`; SHA256 `6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`; route book binding `94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa`.

Tokenizer: `F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json`; SHA256 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`.

CLI: `F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny\worktree\scripts\upgrade3\legacy_unit_writer.py`.

Ledger: `F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny\BUDGET.sqlite`; ownership marker: `BUDGET.sqlite.legacy-route.json`. Existing ledgers are untouched.

Exact live command:

```powershell
python -X utf8 scripts/upgrade3/legacy_unit_writer.py --input "F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny\input\6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7\FULL_BODY_INPUT.json" --output-dir "F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny" --model qwen3.5-plus --budget-limit 30 --ledger "F:\OptoMind-Review-2\outputs\legacy_unit_plus_acceptance_20261010_30cny\BUDGET.sqlite" --tokenizer "F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json" --key-file "F:\OptoMind-Review-2\api_keys\qwen-api-key.txt" --run
```

Logs: `live_stdout.log`, `live_stderr.log`; PID record: `LIVE_PID.json`. The ledger zero snapshot is `LEDGER_0.json` once initialized.
