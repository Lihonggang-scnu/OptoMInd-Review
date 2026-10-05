# Directed-read handle alias repair

## Result

The harmonized directed-read response used `handle`, while the local normalizer only accepted `source_handle`, `paper_id`, and `canonical_paper_id`. The resulting empty `paper_id` values were then silently discarded by `_merge_directed_tasks`, so the six coordinated reads did not reach the adaptive retrieval runner.

The normalizer now accepts `handle` as the final fallback. Existing identity fields retain precedence. Unknown handles remain unchanged and cannot bind to a different mapped paper.

## Changed files

- `worktree/optomind_research/runtime/upgrade3/progressive_review_plan.py`
- `worktree/tests/upgrade3/test_progressive_review_plan_material_reuse.py`

The production change is one fallback expression. The regression exercises the real `_tool_cycle` adaptive boundary and checks six mapped papers, chapter ownership, reasons, explicit-field precedence, and an unknown handle.

## Verification

- Red first: the new regression failed before the production change because handle-only rows resolved to empty IDs and did not queue.
- Green targeted test: `1 passed`.
- Related offline file: `14 passed`.
- `py_compile` for the production module and test: passed.
- Production import check: passed.
- `git diff --check`: passed.
- No network, model, ledger, store, cache, prompt, or live BODY process was changed by verification.

## Resume plan for root review

Keep PID 38516 paused at quality gate `07_level2_tools` until the existing stage is reviewed. It imported the pre-repair module, so it must not be treated as running the repaired code. If root elects to exercise this repair, stop or replace that process through the existing gated driver, reuse the same output root and `budget.sqlite`, and resume with the unchanged production arguments. Root should decide whether the persisted harmonized/level2 stage artifacts need a narrowly scoped replay so the repaired normalizer sees the existing response; this report does not remove or rewrite those artifacts. Do not add a retry flag or start a second uncontrolled run.
