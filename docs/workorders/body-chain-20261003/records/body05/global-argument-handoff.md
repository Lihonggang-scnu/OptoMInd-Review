# WO-05 global argument, material inventory, and owner identity handoff

Status: implemented and bounded offline validation passed; no commit or push. Accepted input revision: `3d244f23166c9ba2e948fd8efd8257d00dbebc51`.

## Diagnosis and actual before/after

F12 is a confirmed handoff omission, not a claim that the previous manuscript had no local argument. The accepted `_post_case_review` supplied neither explicit `review_argument` nor `shared_scope` to the final coordinator. `_assemble_final` discarded the returned argument; final writer packets also lacked the global guidance fields. The accepted first-level payload carried the provisional plan and tool feedback but omitted compact original facets. The provisional material inventory was never refreshed.

`global_handoff_io.json` records an actual function-boundary probe against the accepted Python file loaded from Git, followed by the current implementation, and rereads final JSON/packet files:

- Input L1 argument: `INITIAL_ARGUMENT`; initial scope: `SCOPE_ONLY`
- Coordinator response: `CALIBRATED_ARGUMENT`; finalized scope: `FINAL_SCOPE_ONLY`
- Accepted baseline: coordinator explicit argument/scope absent; final argument absent; packet argument/scope absent
- Current implementation: coordinator receives argument and scope distinctly; final and packet retain `CALIBRATED_ARGUMENT`, `{statement: FINAL_SCOPE_ONLY}`, `review_argument_status=calibrated`, `review_argument_source=whole_plan_improvement`
- Two actual writer user messages with identical scope retain distinct arguments: `Conditions explain the divergence` and `Measurement choices explain the divergence`, under `chapter_frame.review_argument`

The baseline probe uses a synthetic model response and no research tools. Historical real-run replay material remains `LOCAL_ONLY`.

## Narrow changes

Production file: `optomind_research/runtime/upgrade3/progressive_review_plan.py`.

- `_review_guidance` carries independent scope/argument fields. It prefers coordinator `finalized_review_argument`/`review_argument`, then harmonized/L1 argument. Missing argument stays empty with `missing`; an early fallback is `carried_forward_uncalibrated`. Whitespace-only fields are not promoted. Structured argument values are consistently serialized JSON, rather than Python object repr.
- Existing final coordinator prompt gains only a generic calibration requirement: calibrate supplied argument against actual material/chapters, return complete `finalized_review_argument`, and never substitute a scope statement. No topic-specific prompt content or extra planner call was added. This prompt change was reported and approved before editing.
- L1 sees the compact original question, scope, facet asks/exclusions, and current inventory. Search execution queries and the complete candidate corpus are not repeated.
- Initial semantic theme entries are preserved unchanged. New/updated material appends compact attributed, observed deltas: source, paper, known chapter IDs, content excerpt, truncation flag, content signature. No semantic theme taxonomy or heuristic theme relabeling is added. Excerpts are capped at 1,800 characters; the actual affected-owner material remains complete in its existing payload. Sorted JSON makes excerpts stable after cache serialization. The initial snapshot comes from the loaded pool, not retained tool results in a reused planner instance.
- The existing incremental late router now runs before both existing coordinator paths. Only newly routed usable material is attached to its chapter. The pre-attachment snapshot is compared by the existing content-delta logic, extracted without changing its comparisons into `_chapter_material_changes`.
- Legacy mode uses its existing affected-owner queue; opt-in mode keeps its existing complete owner revision path. Neither gains a new planner. The second helper invocation after owners is idempotent for already routed sources and preserves an opportunity for newly owner-admitted, tool-only identities. Formal cases remain after coordinator/owner and before arrangement/writing. No whole-chapter post-case rewrite is reintroduced.
- `_seed_owner_unit_ids` uses an unambiguous, exact explicit paragraph relationship before position. A merged/split ambiguous relationship leaves the old identity unknown; it does not assign the same derived identity to multiple old units. Position remains only for a legacy input with no explicit relationship. Owner validation accepts pure explicit-ID reordering without inventing a remap, rejects duplicate new IDs, and requires split/merge remaps to cover introduced/removed identities.

## Actual late-material observations

The four saved fixtures in `global_handoff_io.json` cover known and unknown late ownership in default and opt-in modes. In each, P0002 carries an observed comparison: outcome Q only under condition Z, with reversal elsewhere.

- Coordinator `late_material_changes` contains CH01 only, including actual material
- CH01 owner receives that material and the calibrated argument; no CH02 owner file is created
- Known ownership adds no router model call; unknown ownership routes only P0002 in one incremental batch
- Coordinator and affected owner precede formal case calls; there is one coordinator call
- Same-instance identical resume adds zero model/router calls
- Normal no-late run adds no route or owner work, and identical resume remains zero-call
- Sources already delivered to the initial owner are not forced through another owner call solely because of a late label; actual content comparison remains the trigger

A separate test uses the real `_tool_cycle` with an offline retrieval-runner boundary. Directed material remains in the inventory on same-instance and fresh-instance resume, with zero repeat L1/model/tool-runner calls.

## Validation

New focused file: `tests/upgrade3/test_body05_global_argument_handoff.py` (12 passed). Most tests are explicitly coordinator/control-flow fixtures substituting model and tool-cycle boundaries. The separate resume test executes the real tool cycle. Writer checks execute actual `build_chapter_view`, `build_unit_view`, `unit_payload`, and `unit_messages`, rereading the written artifacts.

Command (focused controls, not the full suite):

```sh
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q \
 tests/upgrade3/test_body05_global_argument_handoff.py \
 tests/upgrade3/test_body04_late_source_routes.py \
 tests/upgrade3/test_progressive_review_plan_owner_recovery_contract.py \
 tests/upgrade3/test_progressive_review_plan_case_chain.py \
 tests/upgrade3/test_progressive_review_plan_body_boundary.py \
 tests/upgrade3/test_progressive_review_plan_stage_cache_contract.py \
 tests/upgrade3/test_progressive_review_plan_chapter_cache_contract.py \
 --basetemp="$BODY05_GLOBAL_EVIDENCE_ROOT"
```

Result: **100 passed in 1.37s**. `py_compile` and `git diff --check` passed. No live/paid model call, retrieval, download, or full real planning/body run was performed. No WO-06 work was started.
