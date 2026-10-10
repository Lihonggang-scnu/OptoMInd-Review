# Optional account budget on the formal legacy CLI

`scripts/upgrade3/legacy_unit_writer.py` can reserve each physical provider call
against both the existing project ledger and a shared account ledger. Existing
commands without these flags keep their single project ledger behavior.

```powershell
python scripts/upgrade3/legacy_unit_writer.py `
  --input "outputs/new_topic/FULL_BODY_INPUT.json" `
  --output-dir "outputs/new_topic/full" --run `
  --ledger "outputs/new_topic/budget.sqlite" --budget-limit 60 `
  --budget-scope "new-topic-experiment" `
  --account-ledger "local_accounting/shared_account/budget.sqlite" `
  --account-budget-limit 300 `
  --key-file "api_keys/qwen-api-key.txt" --key-index 1
```

`--key-index` is **1-based within the specified file's parsed candidates**.
It selects one candidate, ignores environment credential ordering, and does not
fall back to another key. No key or fingerprint is saved. Without this flag,
the provider retains its existing credential resolution. Preview and cache
replay do not read the credential file.

For a new account ledger, `--account-budget-limit` must be finite and positive.
For an existing ledger, it can be omitted; if provided, it must equal the stored
cap. It is a lifetime cap, not an increment. Account initialization never copies
old project charges. Project and account totals are overlapping dimensions and
must not be added as distinct spending.

The account marker `<account-ledger>.account-route.json` and output
`ACCOUNT_BUDGET_BINDING.json` are published atomically. An account-bound output
requires the same account and mapping directory on paid resumes. Missing
previously bound ledgers, changed caps, and replacement ledger paths fail rather
than resetting the budget. `ACCOUNT_BUDGET_SNAPSHOT.json` records the account
state after a completed CLI run; the project binding and project report remain
in their existing format.

Mappings default to `<account-ledger>.dual-calls/`, independent of output and
input paths. `--account-mapping-dir` allows an existing receipt directory to be
reused. The mapping schema matches the original experiment launcher. Recovery
finishes only persisted settlement receipts for this project/account pair;
other project mappings are skipped. Already recorded identical settlements are
idempotent, conflicting receipts fail, and unknown costs retain both full
reservations. Neither a saved response nor a successful text parse waives an
unknown charge. The existing single-ledger `--reconcile-receipt` operation does
not reconcile both sides of a dual receipt.

Both ledgers must reserve successfully before HTTP. If either refuses, no HTTP
request occurs; only a counterpart reservation known to precede dispatch may be
settled at zero, with its reason recorded. Interrupted mappings or settlements
retain holds conservatively. Saved known receipts can recover without a model
call, including a run whose model stages are all cached.

For the 20261010 experiment, keep the existing project ledger, scope, output,
and ordinary writing/quality/edit options. Add these flags to the **formal CLI**:

```powershell
--account-ledger "F:/OptoMind-Review-2/local_accounting/qwen_first_20261010_300cny/budget.sqlite" `
--account-budget-limit 300 `
--account-mapping-dir "F:/OptoMind-Review-2/outputs/outline_to_body_astra_20261010_60cny/accounting_meta/physical_calls" `
--key-index 1
```

The historical launcher remains an experiment archive. Do not combine it with
the formal dual-budget flags, which would wrap accounting twice. Other projects
can share the same account ledger through these flags without new scripts.
Processes which omit them are **not** constrained by this account cap; this is
local cooperative accounting, not provider-wide enforcement. Article editing
retry behavior and manuscript algorithms are unchanged.

Offline verification: `python -m pytest tests/test_dual_budget_cli.py
tests/test_legacy_unit_route.py tests/test_legacy_unit_route_review.py
tests/upgrade3/test_qwen_effective_token_transport.py -q`. Tests use temporary
synthetic key files and fake transport, with no real credential reads or API.
