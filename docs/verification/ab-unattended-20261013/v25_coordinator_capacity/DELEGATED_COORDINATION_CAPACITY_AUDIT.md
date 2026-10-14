# V24 coordination capacity audit (read-only)

- Scope: `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\execution_sources\planning_resume_v24` at `be0abeaf2ae1bdc1e5a5188b471abb14faec775a`
- Binding: `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\RUN_BINDING_PLANNING_V24_OPTIONAL_FOCUS_20261014.json` (observed SHA-256 `0892fdda8ec8d236ad63cea59b7709b36124444c65e8725e5d59815fc53775c6`)
- Run state: `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\worktree\outputs\optics_e2e\07_progressive_review_plan_docling_20261013\RUN_STATE.json`; status `failed`, current stage `whole_plan_improvement`
- No source, gate, ledger, process, provider, or scientific artifact was changed.

## Failure observed

The persisted failure is `planner_context_preflight_exceeded:whole_plan_improvement:input=1069227:output=32768:thinking=16384`. The recorded preflight was input `1,069,227`, output `32,768`, thinking `16,384`; the V24 guard uses `ceil(tokens*1.12)+8192`, max input `991,808`, and total budget `1,000,000`. The call path is `_post_case_review` → `_stage` → `_call_record` → `_messages_for` for `whole_plan_improvement`.

The exact rejected transient request was not saved. I reconstructed the post-case shape in memory using the V24 refresh and projection helpers. The reconstruction has `3,400,731` compact JSON payload bytes, `3,407,287` message bytes, `893037 tokenizer tokens`, and estimated input `1008394`. Treat these as a reproducible near-bound measurement, not the exact transient request; root should rerun the production tokenizer after any `$ref` encoding.

## What is in the coordination payload

Five chapter packets contain 442 raw source rows (226 handle union). The coordinator filters these to 83 cited rows (59 handle union), with 19 handles used by more than one chapter and 24 repeated row occurrences. The packet-level repeated pool is therefore not the entire 442-row pool.

| chapter | raw rows | selected A/B rows | selected bytes | adaptive tool rows | tool bytes | relevant feedback rows | candidate rows |
|---|---:|---:|---:|---:|---:|---:|---:|
| CH1 | 102 | 16 | 125,115 | 7 | 58,258 | 21 | 6 |
| CH2 | 108 | 20 | 155,648 | 11 | 91,472 | 67 | 9 |
| CH3 | 109 | 19 | 154,381 | 21 | 153,803 | 83 | 7 |
| CH4 | 80 | 12 | 102,344 | 5 | 39,464 | 15 | 7 |
| CH5 | 43 | 16 | 138,234 | 8 | 45,319 | 8 | 10 |

The simulated exact refresh produced 18 late-material change entries totaling `650,415` bytes: CH1 4/132,273; CH2 2/70,595; CH3 5/185,222; CH4 6/223,755; CH5 1/38,534. These entries intentionally preserve current and previous material for audit, so different objects must not be collapsed.

Other reconstructed components: chapters `1,803,082` bytes; late changes `650,415`; compact tool results `833,361`; unassigned navigation `47,105`; 20-item theme inventory `54,695`.

## Exact duplicate opportunities

- Selected source rows: 83 occurrences, 24 exact full-object duplicates. The duplicate row bodies total `207,225` bytes; replacing later occurrences with pointers would add about `1,020` bytes of refs, for an estimated `206,205` compact JSON-byte reduction.
- Within those rows, exact repeated `study_summary_A` bodies total `91,660` bytes and exact repeated `review_planning_B` bodies total `96,064` bytes. These are diagnostics; a full-row ref is safer because it preserves all associated identity and supplement fields.
- Adaptive tool materials: 52 objects, 31 unique exact objects, 21 exact duplicates. Repeated bodies total `155,460` bytes; pointer overhead is about `843`, estimated reduction `154,617` bytes. Compare every field before ref, including conditions, limits, still-missing state, source identities and allowed use.

A safe encoder can keep the first complete object at its original path and replace only later canonical-equal objects with `{"$ref":"#/..."}`. The decoder must run before the planner/parser and deep-equality round-trip must equal the original payload. Paper ID or need ID alone is not an aggregation key; different content remains separate.

## Result / Changed / Verify / Blockers

**Result:** The failure is a real whole-plan capacity overrun. The main mechanically safe savings are 24 exact repeated selected source rows and 21 exact repeated adaptive tool objects; late-material change entries are another large component but require exact structural equality checks.

**Changed:** Wrote only `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\preparation\ab_unattended_20261013\DELEGATED_COORDINATION_CAPACITY_AUDIT.json` and `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\preparation\ab_unattended_20261013\DELEGATED_COORDINATION_CAPACITY_AUDIT.md`. No production file or runtime state was changed.

**Verify:** Re-run the same reconstruction and production tokenizer after root's minimal whole-plan `$ref` transform; record original/encoded message bytes, tokens, ref count, unresolved/conflict count, and `resolve_refs(encoded) == original`.

**Blockers:** The exact transient rejected payload was not persisted, so this report cannot certify the final token saving. The process/provider was not started by this audit.
