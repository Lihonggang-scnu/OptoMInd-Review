# Repaired guide-first B preparation

This is the free preparation record for the detached worktree at
`F:\OptoMind-Review-2\outputs\guided_body_b_repair_20261009_40cny\worktree`,
locked to `145d68788037b4aa31295253d0b0d06f7de88685`. The worktree is clean.
No production file, guide, source manifest, old output, ledger row, or provider
response was changed.

## Fixed budget boundary

The original ledger and marker are reused by identity:

`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`

The initial read-only snapshot found 24 rows, settled `18.3609232` CNY,
reserved/uncertain `0`, and stored lifetime limit `60`. The fixed campaign
ceiling is `min(60, 18.3609232 + 40) = 58.3609232` CNY. The original stored
limit and marker were not changed. `CAMPAIGN_BUDGET_START.json` preserves all
24 baseline reservation and call IDs, the old-row projections, absolute ledger
and marker paths, source identity, and the policy that any open reservation or
uncertain amount stops the campaign.

The wrapper was corrected before any paid call. The campaign records the
wrapper identity migration from `55c7f27016f2069b07c5ac0f1ac37e2e0e35da63f884df7c85c22e10ead35a12`
to `8a6cb6f0781a77e0c31b0290decd6812fd69beb951fd3603b780a43d7a53968a`;
S0, the `58.3609232` ceiling, and the 24 baseline rows are unchanged.

## External wrapper

`run_guided_body_b_repair.py` is the only new execution wrapper. It does not
modify the production CLI or runtime. It validates the fixed B guide, manifest,
Plus config, tokenizer, source commit, and original ledger/marker. Before a
paid run it installs a process lease and wraps the dynamically imported
`module4.runtime.GlobalBudgetLedger`; each instance is narrowed to the fixed
campaign ceiling, while the production `BEGIN IMMEDIATE` reserve remains the
atomic guard. The official shared factory is wrapped before importing the
official CLI alias, and rejects any changed model/profile before provider
construction. It preserves the official `max_retries=0` and `max_keys=1`.

The wrapper also rejects Max/other models, refuses a changed original ledger or
marker, and never releases or resets old reservations. Its self-tests use only
temporary SQLite files.

## Free checks

- `tests/upgrade3/test_guided_body_contracts.py`, `test_guided_body_writer.py`,
  and `test_guided_body_cli.py`: **78 passed in 8.68s**.
- `compileall`: exit 0.
- `git diff --check`: exit 0; locked worktree remains clean.
- Wrapper self-tests: **5 passed**, zero network calls:
  atomic over-cap reserve blocking; restart preserving S0 and stored 60;
  non-Plus config refusal; factory profile refusal before provider creation;
  persistent campaign S0/cap recovery after a new settled row. The held/open
  reservation recovery case also blocks as required.

Logs are under `records\OFFLINE_TESTS.log`, `records\COMPILE.log`,
`records\DIFF_CHECK.log`, and `records\SELFTEST.log`.

## B preview

The official CLI was invoked through the wrapper with no `--run`:

```powershell
C:\Anaconda\python.exe -X utf8 F:\OptoMind-Review-2\outputs\guided_body_b_repair_20261009_40cny\run_guided_body_b_repair.py --manifest F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json --guide F:\OptoMind-Review-2\outputs\guided_body_b_repair_20261009_40cny\worktree\docs\guided_body_writer\guides\guide_B.json --config F:\OptoMind-Review-2\outputs\guided_body_b_repair_20261009_40cny\worktree\config\guided_body_writer\plus_first.json --tokenizer F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json --output F:\OptoMind-Review-2\outputs\guided_body_b_repair_20261009_40cny\B
```

Preview run `20261008T175505Z-63cfea3328` completed with zero model calls and
zero paid dispatches. Input hash is
`94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa`; B guide
SHA is `2b119747742b147b57b3a9ebfb64ec48ed6e254b590106074dd2b1d427e7cc9b`;
source manifest SHA is
`37242cc51bc9b6b96a7e92543a6092c17bfdbd0d8f8ab068a2e3d9c1e3db58e7`.
All seven chapter IDs and writing arrangements are visible, the first
`accepted_body_markdown` is empty, the four top-level fields are exact, and
the actual tokenizer measured the request. Materials contain 1309 evidence
atoms, 55 source identities, 3 tool materials, and 221 navigation records.

The exact recursive `outline_action` key count is **0**. The payload contains
15 `outline_action_recovered_by_policy` metadata flags; these are separately
reported in `records\B_PREVIEW_AUDIT.json` and are not the prohibited exact
`outline_action` field. Root must decide the scope interpretation before any
paid call.

Preview artifacts: `B\CLI_RUN.json`, `records\PREVIEW_STDOUT.log`, and
`records\B_PREVIEW_AUDIT.json`. No paid-start command is authorized by this
record; root must review the wrapper, campaign snapshot, and preview audit
before approving one.
