# Final run account status

Date: 2026-10-02

## Process state

The production BODY run has ended. No planner, arrangement, writing, or assembly process matching this run was found during the final read-only process check. No front matter, back matter, or post-BODY experiment was started.

## Production 50 CNY ledger

Authoritative source: `budget.sqlite` in this run root.

- Ledger limit: 50.0 CNY.
- `settled`: 244 reservations; reserved amount total `150.2422348` CNY; recorded actual spend `27.669975` CNY.
- `reserved`: 0.
- `uncertain`: 0.
- `released_authorized`: 25 reservations. Their original reservation total was `28.633656` CNY and remains recorded in `budget_release_events`. The release records preserve that provider spend is unconfirmed; this is not a claim that the provider charged zero.
- The production ledger was not merged with the separate context-overflow experiment ledger.

The current SQLite grouping is authoritative. The older `RUN_STATE.json` `ledger_snapshot` still shows the pre-release/pre-continuation historical snapshot (`settled_count=129`, `settled_actual_cny=9.6389326`, `uncertain_count=25`), and should not be used for final accounting. Its historical `failure` field is also not a current-run failure report. `RUN_STATE.json` was intentionally left unchanged for this note.

## Separate 30 CNY tournament ledger

The context-overflow tournament used its own ledger at `outputs/context_overflow_tournament_20261002/budget.sqlite`:

- `settled`: 8 reservations; reserved amount total `5.9039448` CNY; recorded actual spend `4.7523674` CNY.
- `reserved`: 0; `uncertain`: 0.
- This spend belongs to the A/B/C context experiment and is reported separately from the production BODY run.

## BODY completion and partial status

The final BODY-only assembly is complete:

- Final draft: `body_assembly_case_restore_20261002/assembled/REVIEW_DRAFT.md`
- References: `body_assembly_case_restore_20261002/assembled/REFERENCES.json`
- Assembly report: `body_assembly_case_restore_20261002/assembled/ASSEMBLY_SUMMARY.json`
- Final detailed plan: `case_chain_restore_20261002/DETAILED_REVIEW_PLAN.json`
- Writer packets: `case_chain_restore_20261002/` and the arrangement/writer packet roots

The assembly report records `status=complete`, `expected_units=20`, `loaded_units=20`, `missing_units=[]`, `errors=[]`, and `arrangement_status=arranged`. All five chapters and all 20 BODY units were written and loaded.

`planning_status=partial` and `problems_resolved=false` describe recorded evidence/material limitations, not missing BODY units or an assembly crash. The planner's substantive stages, including case-group processing and whole-plan improvement, ran to the final plan. Remaining recorded items are:

- CH01_U02 could not resolve P0523 and therefore used P0520, which does not supply every requested longitudinal or direct strategy-comparison detail.
- CH04_U03 records limits on cross-cancer validation for sMAdCAM-1 and TOPOSCORE, plus the 16S/PICRUSt2 inference limitation for P0097.
- One citation handle, `P584`, remains unmapped.

These are preserved in the plan and assembly report for root's content review. No hand correction or additional paid review was performed.

## Erratum: recorded partial status cause

The preceding explanation of `planning_status=partial` as an evidence/material limitation is incorrect and is retained only as the original record. The actual run-state causes were `CH01 owner: QwenTransportError` and `CH05 updated_unit_sources_unavailable:P0574`. This correction does not characterize the issue as a writer failure; the original accounting and completion records above remain unchanged.


## Final offline parser repair

The malformed JSON-envelope repair was rerun from four preserved raw responses with zero model calls, and the assembly was regenerated offline. The final scan found no JSON wrapper leaks in any of the 20 UNIT_BODY files. The four affected units are listed in `body_writing_case_restore_20261002/JSON_WRAPPER_REPAIR_RECORD.json`; readable issue arrays were preserved, including the one nonempty issue array recovered from CH04_U02.

The 142 arranged source handles map to 141 catalog identities because P0402 and P0576 have the same normalized DOI and title and were merged by identity. The separate P0581 blank-row identity conflict is preserved in the audit and is not the cause of that count difference.
