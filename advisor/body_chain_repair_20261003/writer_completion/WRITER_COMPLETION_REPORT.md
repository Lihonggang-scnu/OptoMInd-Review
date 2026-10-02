# Writer omitted-task completion report (2026-10-03)

## Scope

This is an explicit, opt-in completion path. It reads an existing body, selects task IDs, sends only those tasks plus their referenced material, and writes new artifacts beside the experiment output. It never edits the historical BODY file and it does not assert a quality pass.

## Changed files

- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\review_unit_writer.py`
  - Added task-scoped completion payload/message construction, source-handle closure, related chapter-tool filtering, one-call response handling, Markdown-table validation, pending/error preservation, and separate artifact writer.
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\scripts\upgrade3\review_unit_writer.py`
  - Added explicit `--existing-body` and repeatable `--complete-task` branch. Preview remains default; `--fake-client` is simulated; `--run` is the only network mode.
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\prompts\review_unit_writer.md`
  - Added a generic instruction that a table task must be rendered as actual Markdown table rows in `body_markdown`.
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\tests\upgrade3\test_review_unit_writer_completion.py`
  - Eight focused offline tests.

## Verification

- `python -m py_compile optomind_research/runtime/upgrade3/review_unit_writer.py scripts/upgrade3/review_unit_writer.py`
- `python -m pytest -q tests/upgrade3/test_review_unit_writer_completion.py` -> `8 passed`.
- Real CH03 preview (zero model calls): `CH03_U01_T01`, 8 referenced source handles in the completion payload, 1 selected table task, estimated total context 237,957 tokens.
- Real CH05 preview (zero model calls): `CH05_U02_P02`, 3 referenced source handles in the completion payload, 1 selected paragraph task, estimated total context 106,793 tokens.
- Fake CH03 replay: simulated valid Markdown table accepted and appended only to a new `COMPLETED_BODY.md`; original BODY prefix is preserved.
- Fake CH05 replay: simulated fragment appended only to a new `COMPLETED_BODY.md`; original BODY prefix is preserved.

## Output roots

- `F:\OptoMind-Review-2\outputs\body_chain_repair_20261003\writer_completion\CH03_preview`
- `F:\OptoMind-Review-2\outputs\body_chain_repair_20261003\writer_completion\CH05_preview`
- `F:\OptoMind-Review-2\outputs\body_chain_repair_20261003\writer_completion\CH03_fake`
- `F:\OptoMind-Review-2\outputs\body_chain_repair_20261003\writer_completion\CH05_fake`

Each output keeps `COMPLETION_INPUT.json`, `COMPLETION_MESSAGES.json`, `ORIGINAL_BODY.md`, `COMPLETION_FRAGMENT.md`, `COMPLETED_BODY.md`, and `COMPLETION_RESULT.json`. Fake raw response files are under each unit's `fake_response` directory.

## Historical observations

- CH03 raw output copied `table_tasks`/`row_tasks` and produced no Markdown table; the completion path therefore treats task metadata as insufficient.
- CH05 raw output contained only the first paragraph; the completion path selects the explicit second task and retains the existing body unchanged until a complete `appended` response is returned.

No model call, ledger reservation, historical BODY write, full test suite, commit, or push was performed. Simulated fragments are plumbing markers and are not scientific outputs.

- Normal CH03 preview control (no completion flags) retained the original 2 paragraph tasks, 1 table task, and 13-source payload; zero model calls.
- Byte-level check: CH03 fake COMPLETED_BODY.md starts with the unchanged 3,360-byte historical BODY; source historical file timestamp/content remained unchanged.
