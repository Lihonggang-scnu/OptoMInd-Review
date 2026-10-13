# Article runtime alignment audit

Date: 2026-10-13  
Scope: planner baseline comparison and gradient evaluation-budget review

## Planner baseline comparison

The exact saved Article object `97b0b061c186933e7bc14408db48fe6c0c702549` was
read with `git archive` into the external alias directory
`commit-97b0-object-20261013-a`. No repository checkout, reset, source edit, or
test edit was used. The narrow test was run from the tensor interpreter with
pytest plugin autoload disabled and external cache/temp directories.

| Source | Result |
| --- | --- |
| Current Article checkout (`0aca3e3b1debfa4fd14efd90095cfc1d47011463`) | `1 failed in 0.68s`, `KeyError: 'text'` at `test_tmm_strategy_planner.py:267` |
| Exact Git object `97b0b061c186933e7bc14408db48fe6c0c702549` | `1 failed in 1.35s`, same `KeyError: 'text'` at line 267 |

The captured outputs are `current-test.txt` and `baseline-test.txt` in this
directory. The test indexes `item["text"]` while the planner compaction result
contains the compact schema `text_head`, `text_head_chars`, `text_chars`,
`text_sha256`, and `full_text_pointer`. Thus the failure is an existing test/
compaction-contract mismatch already present at `97b0b061`; it is not caused by
the later spectral-ellipsis helper or source-scope changes. The failing test
body is unchanged between the saved object and the current checkout. No source
or fixture/test fix was made here.

## Gradient evaluation budget

`GradientThicknessAdapter.optimize` accepts
`maximum_forward_evaluations`, but the value is not consumed: it constructs
`DifferentiableThicknessOptimizer` and calls `optimizer.optimize(task)` without
passing a cap. The differentiable backend has no cap parameter. Its evaluation
counter increments by the number of rows evaluated, so one optimization step
can consume multiple evaluations for multi-start and robustness samples.
Adam steps, an optional Adam/L-BFGS phase, initial/final verification,
quantized candidates, and candidate scoring can all add forwards; an L-BFGS
closure can call the loss repeatedly. The adapter therefore cannot promise a
finite cap for arbitrary gradient tasks. The inspected adapter/backend behavior
is unchanged in the saved baseline for this issue.

The separate Article pilot context observed 64 reserved evaluations and 168
consumed before `budget_exhausted`. The current external pilot containment of a
64 ceiling with one start, eight Adam steps, and no L-BFGS/global optimizer is
separate from a production fix; it bounds that pilot profile but does not make
the adapter contractually budget-safe for arbitrary configurations.

The smallest safe immediate production remedy is to fail closed when a finite
cap is supplied to the gradient adapter until backend enforcement exists,
rather than silently ignoring the cap. Proper cap support should thread the
limit into the backend, count rows before every loss/forward dispatch, reserve
budget for required final/candidate evaluations, make L-BFGS closures
budget-aware or disable them under a finite cap, and return an honest
best-effort/budget-exhausted audit. Focused tests should cover single-start,
multi-start, robustness, L-BFGS, quantized evaluation, and final candidate
scoring. No implementation was made in this audit.

## Validation limits

Only the two narrow planner invocations and static source inspection were done.
No full regression, network/API, GPU, credential, dependency installation, or
old-environment mutation was performed.
