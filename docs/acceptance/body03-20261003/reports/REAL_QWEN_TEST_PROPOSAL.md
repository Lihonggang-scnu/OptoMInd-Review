# WO-03 bounded real-Qwen acceptance proposal

Date: 2026-10-03

This checkpoint targets the WO-03 implementation at the shared worktree
`review-v2-body-cloud03-acceptance-20261003`, HEAD `8810c7b`. The runner,
configuration, receipts, and comparisons will live only below
`outputs/cloud_body03_real_acceptance_20261003/`. It will not modify
production or test files, create a new wallet, reset the shared ledger, upload
anything, run the full planner, or run the full BODY chain.

## Offline-first gate

The first command will be `run_real_acceptance.py preflight`. It will verify
the selected worktree HEAD, the real packet/card/snapshot paths, the saved
whole-plan response, the local material index, the direct Qwen key file's
existence and non-empty-line count only, and the shared ledger state. It must
report `network_call=false`, `reserved_cny=0`, and the expected local input
hashes without printing any credential content. No model call is made by this
phase. Paid execution remains gated behind `--allow-paid` and the parent
checkpoint.

## Scenario A: saved whole-plan feedback into the current owner

The runner will use the current production functions
`ProgressivePlannerConfig`, `QwenProgressivePlanner`, and
`ProgressiveReviewPlanner._post_case_review`. The existing CH02 packet is
loaded from the full acceptance planning output and reduced only by the
already-established owner selection (CH02:U1 and CH02:U3, source P0004 and
their existing A/B rows). Those packet rows remain unchanged; the current
P0004 deep-read material is attached as a separate, source-bound field.

The prior owner-delta artifact is read from
`outputs/cloud_body02_real_acceptance_20261003/runs/live/owner_delta/`. The
runner accepts the saved raw whole-plan response as authoritative and unwraps
the old stage envelope before reusing it. It then supplies that response
through a local planner wrapper for the `whole_plan_improvement` stage and
lets the current `affected_chapter_revision` implementation make exactly one
real `qwen3.5-plus` call through the existing `GlobalBudgetLedger` and direct
Qwen client. The owner payload therefore contains the prior `chapter_feedback`,
the unchanged chapter packet/A/B material, and the real P0004 reading. The new
output root is separate from all prior caches.

Evidence to persist:

- exact owner input payload and SHA256 comparison against the unchanged packet
  and P0004 reader content;
- the reused whole-plan response and the current owner request/response;
- owner stage cache, updated plan, model telemetry, and ledger reservation;
- proof that `chapter_feedback` reached the owner and the resulting plan is
  usable enough for root's content review;
- no fabricated scientific answer or manual nudge in the input.

## Scenario B: empty-first-query retrieval through the production adapter

The runner will construct the current `make_planning_supplement_runner` and
`make_retrieval_loop_runner` against the existing local planning-material
index, using a new run root and the shared ledger. The request starts with an
empty `targeted_queries` list and names a narrow, source-bound need already
represented in local materials. The adapter is allowed to run its real local
triage and Qwen judgment/extraction paths as needed; external live search is
disabled in the first bounded acceptance unless separately approved at the
checkpoint.

The same answered need will then be rebound to a second chapter/stage without
another paid retrieval. A follow-up request retains the initial
`WO03-O-MECHANISM` requirement and adds `WO03-O-BOUNDARY`; the
runner will retain the first material, mark only the new requirement
outstanding, and route any new attempt directory returned by the adapter to
the next owner-facing result. The report will record query initialization,
reuse key, attempt paths, local/Qwen boundary counts, status (`fulfilled`,
`partial`, or `unmet`), and the useful source-bound content. It will not turn a
partial response into `fulfilled` by shape.

## Planned calls and cost

The expected live shape is 2–5 calls: one current owner call and one to four
reader/initializer/triage calls depending on what the existing local index
actually answers. The owner output is capped at 16,384 tokens with thinking
enabled through the existing stage contract. Conservative preflight estimates
will be written before execution; the provisional run ceiling is 5 CNY inside
the existing shared 30 CNY ledger, with an expected realized total below 2 CNY.
The ledger currently belongs to
`outputs/cloud_body02_real_acceptance_20261003/budget.sqlite`; it is reused as
is, with no historical reservation reset and no independent budget.

## Scientific checks

Root should inspect the persisted content, while the runner checks that:

1. the first query is either inherited or genuinely initialized once from the
   empty request, without invented topic keywords;
2. the local paper/card/snapshot content is useful for the named need;
3. owner input contains both the old A/B material and new P0004 material;
4. the saved whole-plan feedback is actually passed into the owner request;
5. an answered compatible need is reused with zero new paid retrieval calls;
6. changed output requirements preserve old material and expose the new gap;
7. partial/unmet feedback remains visible and never becomes fulfilled by shape;
8. the resulting owner plan retains the original chapter and source boundaries.

Planned entry points after checkpoint approval:

```text
python outputs/cloud_body03_real_acceptance_20261003/run_real_acceptance.py preflight
python outputs/cloud_body03_real_acceptance_20261003/run_real_acceptance.py run --allow-paid
```

