# External recovery wrapper diff

Added only:

- `recovery_wrapper.py`: no-network audit, single explicit user waiver, backup
  and SQLite audit-table update, plus an optional live handoff to the unchanged
  `scripts/upgrade3/legacy_unit_writer.py` module with `--run --retry-failed`.
- `TIMEOUT_ROOT_CAUSE.md`: bounded evidence and conclusion for Ch6_U1.

The source worktree remains detached at
`054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512`; no production source, prompt,
model profile, input, successful cache, failed attempt, or ledger row is
rewritten by the free check. `--check` performs no provider call and no waiver
mutation. `--waive` is the only mode that mutates the original ledger, and it
creates a non-overwriting backup first. `--run` is intentionally separate and
is not run in this checkpoint.

The waiver transition is `uncertain -> user_waived`, with the ledger row's
`amount_cny` set to `0.0` only to release budget occupancy and `actual_cny`
left `NULL`. The audit table and `BUDGET_WAIVER.json` retain the original
amount (`1.63964`), original row snapshot, unique reservation ID, backup path,
and user authorization text. It is never represented as `settled` or actual
cost `0`.
