# Independent consumer/recovery context checks

These are synthetic offline regression artifacts, not real-source scientific acceptance or a full BODY run. No credentials or live provider calls were used. The test file is `tests/upgrade3/test_preflight_material_recovery_context.py`.

## Baseline and current behavior

`before_context_fix/` was generated with the unchanged `6aee581` versions of `progressive_review_plan.py` and `planning_retrieval_loop.py` loaded into a separate Python process. Other dependencies and unchanged prompt files come from this checkout. All three regression tests failed on their intended assertion:

- Local completion: returned `answered` but retained `OLD_ACTIVE_GAP`
- External completion: returned no current missing field but retained the resolved local gap in `limits`
- Additional local requirement: retained P1's prose but omitted P1's source, condition and limit from active material

`after_context_fix/` was generated against the repaired working tree: **3 passed**. Each record preserves active need state, delivered material and the exact next-outline messages rendered through `_model_stage`/`_messages_for`. Local-triage and external-service boundaries are controlled; `build_writer_material`, the adapter, journal, need cache and message rendering are real. The empty index marker is never searched. Real SQLite content-change tests are separately covered by `test_progressive_review_plan_tool_consumer_recovery.py`.

## Reproduction

From the repository root:

```sh
PREFLIGHT_CONTEXT_EVIDENCE_ROOT=docs/workorders/body-chain-20261003/records/preflight_consumers/review/after_context_fix \
PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-wo01-deps:. \
python -m pytest -q -p no:cacheprovider tests/upgrade3/test_preflight_material_recovery_context.py
```

Use the environment's already installed dependency path where `/tmp/optomind-wo01-deps` is unavailable. No installation or network access is required by these tests themselves.

## Independent verification

- F1 plus arrangement/writer and feedback controls: **41 passed in 16.40s**
- F2/F3 dedicated, these context tests, and selected existing provider-resume, budget, legacy owner-rebinding, changed requirements, useful partial and changed source controls: **20 passed in 0.99s**
- Extra actual validator/export/writer probe accepted an intentionally unused selected source and advisory issue while preserving a usable review-derived-only source
- Same-need partial → answered real-adapter probe now has one active cumulative row; old useful prose remains and the superseded gap is absent from actual chapter-detail messages

These focused checks do not cover deferred identity/history compatibility, newly selected case deep-material handoff, model scientific correctness or full BODY behavior. Prompts and stage order remain unchanged.

The legacy nonadaptive supplement/reading branch retains its old outer-cache behavior. F2 recovery claims apply to the adaptive collector only; no new dependency/retry guarantee is made for the legacy branch.
