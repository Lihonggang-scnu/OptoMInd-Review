# BODY offline handoff review

**Historical pre-fix comparison only.** This note compares the saved old BODY and the pre-`b3580fe` manuscript-parts recovery outputs; it is not acceptance of the restored code path. The actual offline replay of the current direct-append fix is recorded in `offline_restore_check/OFFLINE_RESTORE_CHECK.md`. No model, network, or paid call was used to produce either note.

## Counted stages

| chain | case additions | arrangement task source handles | final `REFERENCES.json` entries |
| --- | ---: | ---: | ---: |
| Old `run585` | 143 | 183 | 176 |
| New recovery | 96 | 76 | 71 |

The old case file is `outputs/progressive_review_plan/20260926_astra_repair/run585/stages/case_groups.json`. Its arrangement union is the six chapter outputs under `outputs/chapter_arrangement/20260927_astra_batch` plus the CH02 and CH05 re-exports under `outputs/chapter_arrangement/20260927_offline_repair`. The old final body is `outputs/full_review_draft/20260927_run01`.

The new case file is `outputs/manuscript_parts_acceptance_20260930/new_plan/stages/case_groups.json`. Its saved arrangement is `body_run/recovery_20261001/arrangement`; the offline writer handoff is `body_run/recovery_20261001/writer_offline`; the final reference list is `body_run/recovery_20261001/assembly/REFERENCES.json`.

## What the differences mean

- Old case additions overlap the arrangement task sources for 142/143 handles. The one absent handle is `P0576`; the old final reference map still carries it in the alias group `P0402/P0576`, so this is not evidence of a lost paper.
- New case additions overlap the saved arrangement for 59/96 handles. The other 37 were not selected into any saved arrangement paragraph task. The 59 selected case handles all had material-backed writer sources in the saved offline writer payload. This is selection breadth, not a packet-to-writer material drop.
- Old arrangement sources absent from the final bibliography are `P0026`, `P0076`, `P0271`, `P0298`, and `P0370`; they were arranged inputs without a final body citation. The old reference list has three handle merges (`P0584/P0333`, `P0413/P0588`, `P0402/P0576`).
- New arrangement sources absent from the final bibliography are `P0085`, `P0335`, `P0523`, `P0561`, `P0573`, and `P0587`; these were arranged inputs without a final body citation. The final assembly contains two handle merges (`P0607/P0614`, `P0092/P0611`). The other final-only handles came from the approved CH02_U02 retry payload, which replaced that unit's saved offline input; they are not evidence that the saved arrangement lost them.

Thus the large reduction occurs before final assembly: the new plan has fewer chapters/units and only 59 of its 96 proposed case handles survive into arranged paragraph tasks. The later packet→writer path preserves every selected handle with material. The old/new final reference counts also include ordinary unused arranged inputs and DOI/identity alias merges.

## Two material-backed examples

Both examples are in CH01 unit U01:

1. `P0575` (`Antibiotic Timing and Survival...`)
   - Arrangement use: `body_run/recovery_20261001/arrangement/CH01/CHAPTER_ARRANGEMENT.json`, `units[CH01_U01].paragraph_tasks[CH01_U01_P01].source_uses`: primary evidence for post-ICI 60-day exposure HR 1.27 and tumour-subgroup differences.
   - Writer material: `body_run/recovery_20261001/writer_offline/CH01_CH01_U01/UNIT_INPUT.json`, `materials[source_handle=P0575].study_summary_A` and `.review_planning_B`.
   - The writer body cites `P0575` in `.../CH01_CH01_U01/UNIT_RESULT.json`.

2. `P0081` (`Unraveling gut microbiome interferences...`)
   - Arrangement use: the same CH01 U01 paragraph task, as background meta-analysis evidence covering more than 41,000 patients and the OS/PFS association.
   - Writer material: the same `UNIT_INPUT.json`, `materials[source_handle=P0081].study_summary_A` and `.review_planning_B`.
   - The writer body cites `P0081` in the corresponding `UNIT_RESULT.json`.

In both cases the arrangement use, A/B material, and writer citation are connected through the same handle; the use text is not being counted as scientific source content.
