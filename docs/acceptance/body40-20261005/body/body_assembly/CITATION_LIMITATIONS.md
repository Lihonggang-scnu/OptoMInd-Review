# BODY assembly citation and provenance notes

- Planned units: **29**; assembled units: **29**; missing: **0**.
- Assembly used the existing offline production assembler and made **0 model calls**.
- Explicit source selection: Ch2 and Ch3 from `writer_repaired`; Ch1, Ch4, Ch5, Ch6, and Ch7 from `writer_live`.
- Cross-chapter identity union: **173** used paper identities from **222** catalog identities.
- Comparison: planning identity count **190**; old successful baseline citation count **176**. The assembly reference list contains **173** entries after identity/DOI alias merging.

## Retained quality limitations

- `Ch6_U4`: unresolved numeric citations `[1]`–`[10]` and `[12]` are retained exactly in the handle manuscript. No order or identity was guessed, so these are not claimed as resolved references.
- `Ch7_U02`: `[Q01]` occurs 5 times as a non-paper tool-need marker. It is excluded from research-reference identity counting.
- The assembler reports `complete` because all 29 unit result files loaded. That status does not mean scientific review or citation cleanup is complete.
- Writer-level carried issues remain visible in `ASSEMBLY_SUMMARY.json` and `RUN_REPORT.md`; no body text was hand-edited.

## Primary and derivative outputs

- Handle manuscript (primary for unresolved citations): `REVIEW_DRAFT_HANDLES.md`
- Numeric derivative: `REVIEW_DRAFT.md`
- References: `REFERENCES.json`
- Machine-readable provenance: `ASSEMBLY_PROVENANCE.json`
- Explicit unit/result selection: `batch_input/BATCH_JOBS.json`
