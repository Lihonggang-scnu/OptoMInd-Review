# Independent content review of live directed-reading outputs

## Scope and files read

I read the actual `DIRECTED_READING.json` files for the four live runs and compared them with the selected source snapshots. Bibliography payloads were omitted from this review.

- P0004 initial Q1: `F:\OptoMind-Review-2\outputs\cloud_body02_real_acceptance_20261003\runs\live\reader\paper1_initial\directed\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\dr-task-0a3c29a5c78a27910afc4081\DIRECTED_READING.json`
- P0004 changed outputs Q1: `F:\OptoMind-Review-2\outputs\cloud_body02_real_acceptance_20261003\runs\live\reader\paper1_changed_outputs\directed\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\dr-task-7a86c38d7162d2f1f5f4bffb\DIRECTED_READING.json`
- P0004 changed question Q2: `F:\OptoMind-Review-2\outputs\cloud_body02_real_acceptance_20261003\runs\live\reader\paper1_changed_question\directed\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\dr-task-3194beee4a894ee028538d86\DIRECTED_READING.json`
- P0001 other-domain Q1: `F:\OptoMind-Review-2\outputs\cloud_body02_real_acceptance_20261003\runs\live\reader\paper2_other_domain\directed\03c787f67836e02cfb9fdf0825b73a1f5d90b825\dr-task-d0b1f27d7a51c2b1c5112758\DIRECTED_READING.json`

Selected source cross-checks were made against P0004 `READING_VIEW.md` at `block-44ce2126a77858a248ad` (Pushalkar PDAC models and depletion/TME findings) and `block-be6e1d6ee0c7a33c4f14` (PDAC anti-PD-1 and human-to-mouse transfer limits), with the source notes in `source_reading_notes.md`.

## Result

### P0004 initial Q1

The initial Q1 asks for explicit microbiota-to-ICI routes, the immune change, outcome, and model/cohort condition. It is `status=partial`, `material_ready=true`, with four evidence rows. The rows are useful for writing because they include the direct PDAC depletion result, non-PDAC ICI examples, melanoma FMT cohorts, and the Riquelme long-term-survivor PDAC transplant result. Its main weakness is grouping several studies into one row, which makes provenance and route boundaries harder to cite cleanly.

### P0004 changed outputs Q1

The changed-output run keeps the same Q1 and is also `status=partial`, `material_ready=true`. It returns five smaller evidence rows: Pushalkar, Sivan, Routy, Tanoue, and Vétizou. This has a modest practical benefit over the initial output: the rows are closer to a chapter evidence matrix and retain model conditions beside the route, while the Vétizou anti-CTLA-4 example is separated from the anti-PD-1 examples. It is not a new scientific answer. It also drops the initial output's Riquelme and patient-FMT rows, so it should supplement the initial notes rather than replace them.

The practical writing material is sound at the level needed here: Pushalkar is a PDAC preclinical depletion-to-anti-PD-1 result with increased CD4+/CD8+ activation, whereas Sivan/Routy/Tanoue/Vétizou are other tumour-model analogies. The output occasionally turns this into interpretation (for example, saying depletion may mimic beneficial bacteria or remove suppressive signals); that wording should be treated as synthesis, not as a direct source statement.

There is a minor group-study naming flaw: the live outputs vary between `MCA205` and `MCA20`, and one grouped description associates the label with the wrong tumour category. This is a nonblocking LLM issue for root's quality read and does not require a prompt change.

### P0004 changed question Q2

Q2 is genuinely different and more useful for chapter structure. It asks what is actually established for PDAC, then classifies positive examples from melanoma, CRC, other tumour models, or patients. The seven rows preserve Pushalkar as the PDAC anchor and add clearer provenance for Sivan, Routy, Si, and Tanoue, plus the Davar and Baruch melanoma FMT cohorts. This supports a defensible transition such as “PDAC evidence remains preclinical; analogous microbiome-ICI gains are reported mainly in other tumour models and melanoma cohorts.” It overlaps with Q1 on Pushalkar but answers an evidence-boundary question that Q1 does not.

Q2 is `status=partial`, `material_ready=true`; its remaining points correctly identify the missing PDAC biomarker and unresolved molecular detail. The same minor `MCA20`/`MCA205` naming issue appears and is nonblocking.

### P0001 other-domain Q1

The solid-electrolyte output is `status=material_ready`, `fulfilled=true`, with no remaining points. It contains usable writing material for a comparison paragraph or table: named sulfides (Argyrodite, LGPS and related compositions) and oxides (LLZO, LATP, LLTO), conductivity and activation-energy values, measurement temperatures, synthesis or structural conditions, and the sulfide-versus-oxide transport/fabrication trade-off. Examples include Li6PS5X at 25 °C, LGPS near 10^-2 S cm^-1, Ta-doped cubic LLZO at 0.87×10^-3 S cm^-1, and LATP total versus grain conductivity.

This is a substantively independent domain and the question produces different useful information from P0004. The numbers remain review-derived claims and should be checked against the source before being used as hard quantitative statements, but the output is already a practical source-accounting scaffold. No broad conclusion about old BODY quality follows from this one second paper.

One numeric-context issue is nonblocking: the Rao conductivity range is rendered as `1.9×10^-9`–`7×10^-3` in the live answer, while source paragraph block `bb2cb4f...` gives `1.9×10^-4`–`7×10^-3`; the source table cell block `01b492...` does contain `1.9×10^-9` for a crystalline/amorphous row. This is a source paragraph/table inconsistency combined with merged conditions, not an invented exponent and not a reason to redesign the prompt. The LGPS, LLZO, LATP, and LLTO values checked by root remain usable with ordinary source cross-checking.

## Changed / practical assessment

- The second P0004 question adds genuinely different value: evidence provenance and PDAC-vs-other-tumour scope.
- The changed-output Q1 has a practical format/focus gain, but no new question-level information; it is narrower than the initial output and omits two useful rows.
- Partial P0004 notes remain valuable for writing because the direct PDAC result, model conditions, and evidence limits are present even when the status is not fully fulfilled.
- P0001 provides a clean independent-domain read with concrete named materials and conditions; it is suitable for bounded writing use after normal source cross-checking.

## Verify

The four JSON paths above exist and were parsed directly. Statuses observed were `partial/material_ready=true` for all P0004 runs, and `material_ready/fulfilled=true` for P0001. The content review was limited to these two papers and does not support a broad claim about the old BODY.

## Blockers

None. No calls were started, no waiting was required, and no source or code files were changed.
