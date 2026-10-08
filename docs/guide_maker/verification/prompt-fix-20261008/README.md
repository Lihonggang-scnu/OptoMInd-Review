# Targeted maker fix: offline verification

Base: `5f66b0f`. All checks ran locally on Linux, without a provider call or publication.

- Original maker suite: **74 passed** (`BASELINE_TESTS.log`).
- Final maker/contracts/CLI/dedicated-budget/prompt suite: **91 passed** (`FINAL_TESTS.log`).
- Unchanged writer contracts/runtime/CLI regression: **60 passed** (`WRITER_REGRESSION.log`), via `python -m pytest -q tests/upgrade3/test_guided_body_contracts.py tests/upgrade3/test_guided_body_writer.py tests/upgrade3/test_guided_body_cli.py`.
- `git diff --check`: passed.

Command (the baseline omitted the two newly added test modules):

```text
python -m pytest -q tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_cli.py tests/upgrade3/test_guide_maker_dedicated_budget.py tests/upgrade3/test_guide_maker_prompt.py
```

The prompt uses existing guide fields for concrete main exposition/later incremental reuse, selective explanation depth, meaningful comparison dimensions and non-collapsible conditions, complementary roles, and transitions through new understanding. Completion requires usable writing decisions; reading is only for an unresolved choice. The schema, planning, source selection process, model configuration, stages and writer are unchanged.

The archived final dedicated-budget CLI patch is integrated, preserving the default legacy 60 CNY guard and the writer's entry point. Additional safeguards reject a missing previously marked database, empty/corrupt databases, invalid persisted cap/amount/status and nonfinite totals. Existing dedicated ledgers are checked read-only; spend and uncertain reservations survive resume. A preview that increases the logical call cap retains the live ledger binding. Hash keys use `as_posix()` for portable archive names; this run did not execute on Windows.

Outline projection factoring is deferred. No owner context or potentially unique scope/conditions were removed. Existing exact task_catalog deduplication is unchanged.

These tests validate wiring, budget boundaries, cache/provenance, existing cold-start and reading contracts, and textual prompt requirements. They do not demonstrate real-model writing quality. No new guide, model-selected readings, paid body or live balance verification was performed. The local handoff requires checking the original dedicated ledger at execution time; 29.482970 CNY is the archived remaining balance, not a newly granted or verified current allowance.
