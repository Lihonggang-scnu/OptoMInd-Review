# Controlled cloud continuation prompt: BODY WO-03

You are continuing the BODY repair sequence from this sanitized local-acceptance handoff. Read the actual contents before doing work; do not rely on file counts or the summaries alone.

## Required reading order in this same turn

1. Read this package `README.md`.
2. Read `ASTRA_ACCEPTANCE.md`.
3. Read both independent content reviews: `material_selection/INDEPENDENT_CONTENT_REVIEW.md` and `material_selection/OWNER_DELTA_CONTENT_REVIEW.md`.
4. Read all four actual reader records under `reader/`, including their questions, required outputs, model/config skeletons, and parsed material. These are transformed copies with explicit omission markers; read the actual material that remains.
5. Read `owner/initial_whole_plan_improvement.json` and `owner/final_whole_plan_improvement.json` for global-coordination parser outcome and telemetry, then read `owner/initial_owner_CH02.json` and `owner/final_owner_delta_CH02.json` in full enough to inspect the actual A/B inputs, existing and new deep material, complete parsed `updated_plan`, and parsed owner response. Pay special attention to the P0004 material that entered the owner input but was not fully surfaced in visible plan prose.
6. Read the recovery and runtime records under `evidence/`, then read `code_audit/LOCAL_FAILURE_STATUS_FIX.md` and `code_audit/LOCAL_FIX.patch`. Distinguish the original cached/superseded failure-control attempt from the final successful controlled failure/recovery.
7. Write `records/local_acceptance_alignment.md` from the template, clearly separating usable conclusions, not-yet-established claims, and accepted minor LLM imperfections. Do not say that the cloud agent read something until it has actually read it.
8. Read the repository's actual `docs/workorders/body-chain-20261003/README.md`, `START_HERE.md`, and `03.md` before editing. Do not infer a work-order path or scope from this prompt.

## Authorized continuation

After the required reading, continue the original work order 03 in the current repository and current BODY sequence. The authoritative work-order file is:

`docs/workorders/body-chain-20261003/03.md`

Its scope is exactly:

- `optomind_research/runtime/upgrade3/planning_retrieval_loop.py`: `run_retrieval_loop` and query initialization/state handling.
- `optomind_research/runtime/upgrade3/planning_supplement.py`: supplement attempt allocation, result persistence, and feedback classification.
- `optomind_research/runtime/upgrade3/progressive_review_plan.py`: `make_retrieval_loop_runner`, supplement adapter, cross-stage need assembly, and owner feedback handoff.

The six bounded actions and acceptance points are:

1. On the first retrieval round, initialize an effective query from the current need/plan or call the existing refiner once, persist it, and cross the retrieval boundary once.
2. If initialization fails, report “query not formed” with the need and reason; never report “no literature found” and never inject topic-specific keywords or loop without a bound.
3. Key cross-stage reuse by research content and acceptance requirements; a compatible answered need gets one supplement/retrieval and two separate owner bindings, with owner changes alone unable to repeat the supplement.
4. When `required_outputs` changes, retain reusable old material, mark only the new portion outstanding, and supplement only that missing portion; shape alone cannot promote partial material to answered.
5. Allocate every supplement retry in a new attempt directory, preserve the failed attempt, and route the current returned path into both the owner input and material pool.
6. Preserve useful `partial`/`failed`/`unmet` feedback even when optional `gap_id` or fulfillment fields are absent; keep the existing quota, reservations, and budget accounting.

The record must show query lifecycle, the one-retrieval/two-owner-binding reuse case, changed-output supplementation, actual retry directories and returned paths, and missing-optional-field feedback. Use the actual WO-03 fixtures and targeted checks; do not substitute a summary-only claim.

The uploaded code state already contains the tested local failure-status repair; use that code directly and do not manually reapply `code_audit/LOCAL_FIX.patch`. The patch is retained for provenance/comparison. Preserve the established BODY sequence: global coordination and owner revision → formal case attachment → arrangement → unit writing. Keep production prompts general. A review-reported original-research identity-only row remains valid when review/tool content supplies the study substance; identity alone never licenses invented content. One merged table may satisfy several comparison tasks when its content and conditions satisfy them. Preserve useful partial material, unresolved gaps, evidence boundaries, and existing stores.

## Hard limits and stop point

- Do WO-03 only; do not start WO-04–07 or rewrite the whole BODY chain.
- For this handoff, root has explicitly opened only WO-03 after the local 00–02 pass recommendation. The repository `QUEUE_STATE.md` may still show its earlier initial gate; update the local queue/record to reflect this handoff, then stop after WO-03 with the exact marker `READY_FOR_ASTRA_03`. WO-04–07 remain closed.
- No paid calls, external/live retrieval, credentials, full planning run, full BODY run, PDF generation, or full-suite experiment.
- Use bounded offline fixtures and targeted local checks only, as the actual WO-03 file requires.
- Do not introduce a fixed 176-paper quota or any unbounded retry loop.
- Preserve the existing quota and budget accounting. The local remaining 29.5223964 CNY is an observation from this run, not permission for any cloud call.
- Do not replace established prompts, create a second planner, change paper-count policy, or erase failed attempts.
- Record actual files/functions, fixture IDs or `LOCAL_ONLY` boundaries, query lifecycle, attempt directories, reuse key, partial/unmet state, and budget observations.
- Start from the latest explicit SHA of the uploaded acceptance branch by creating a separate work branch named `body-chain-repair-cloud03-20261003` (if it exists, choose a new distinct name). Commit only WO-03 work and its record on that work branch; it may be pushed for local acceptance. Do not merge or modify `main`, `review-v2`, any `lihonggang` branch, or the original acceptance/archive branch.
- Stop after WO-03's bounded implementation and checks with the exact marker `READY_FOR_ASTRA_03`; do not widen scope from this prompt. WO-04–07 remain closed.
