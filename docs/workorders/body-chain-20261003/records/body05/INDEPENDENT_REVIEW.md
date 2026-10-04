# WO05 independent bounded review

Verdict: PASS for the frozen production diff reviewed on 2026-10-04 03:48 UTC. No remaining blocking defect found within WO05 scope.

Base: accepted WO04 3d244f23166c9ba2e948fd8efd8257d00dbebc51.
Branch: body05-argument-identity-cloud-20261004.
Production review: read-only; no production edits by this reviewer.

## Verification

- All tests/upgrade3/test_body05* tests: 53 passed in 8.64s.
- git diff --check: clean.
- Independent sourcebrief_conflict.py: both attempts to bind an old explicit paragraph ID to the other source brief now reject with paragraph_id_brief_conflict.
- Independent resume_inventory.py: identical first and resumed material_theme_inventory; no repeated model calls on same-instance resume.
- Independent late_default.py: default and opt-in each route the late source before whole_plan_improvement, then revise only CH02 before formal cases. CH01 remains unrelated.
- Inspected final scope/argument propagation, complete arrangement payload fields, transparent missing/blank argument fallback, explicit/legacy unit IDs, derived split/merge paragraph identity, source-brief reconstruction at writer read boundary, owner remap validation, and unchanged-feedback written-byte reuse control.

## Findings closed during review

1. Old explicit paragraph IDs could be swapped via source_briefs while validation reported unchanged carried-over tasks. The final validation and writer read boundary reject conflicting relationships.
2. Default planning routed late sources after final coordination. Both modes now make late ownership/material visible before coordination and affected owner revision.
3. Same-instance resume incorrectly included previous-run deep-read state in the initial inventory baseline, dropping the delta. The baseline is now stable; excerpt serialization is canonical, avoiding key-order-only invalidation.
4. Whitespace-only finalized arguments could suppress real earlier arguments and be labeled calibrated. Blank text now falls back transparently.
5. Arrangement model payload clipping could remove the calibrated argument suffix. The complete global argument and shared scope now pass independently of the source excerpt budget.

## Limits

These are deterministic offline engineering fixtures. No full repository test suite, real BODY generation, paid calls, real scholarly search, paper downloads, private historical assets, or scientific-quality acceptance was performed. Historical raw replay artifacts remain LOCAL_ONLY. The review does not establish improvement in manuscript scientific quality or authorize WO06.
