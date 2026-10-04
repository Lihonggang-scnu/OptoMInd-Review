# Pending-to-delivery seam

This is a one-unit synthetic offline batch, not a full review run. Production history import, assembly, config loading, delivery entry and no-change editing are used without replacement. There are no source downloads/model calls. No front/back fixture is provided, so the normal control stops at the existing missing-parts gate. No scientific manuscript is published.

## Failure first

`FAIL_FIRST.txt`: 8 failures, one normal control passing against baseline `841e914`. A complete loading summary correctly reported `problems_resolved=false` for a missing table or truncated writer result. Delivery still invoked editing because the handle draft existed. The same bypass applied to explicit missing/pending/restricted results and a stale draft.

## Small fix

`review_delivery.run_downstream_delivery` now checks current assembly readiness before looking up a draft. Partial/missing/restricted/error/task-pending reports stop with `halt_reasons=["assembly_pending"]`, no stages run. Existing draft/unit files are not deleted or changed. The official entry writes the combined report as usual.

Bracketed citation identity gaps alone retain the existing explicit-catalog downstream path. They must have no pending task problems or bare unknown table handles. An independent probe established that stage04 can overlook bare source-column handles, so unmapped table handles remain blocking. This package does not redesign citation rendering.

## Before/after persisted evidence

`capture.py` imports baseline code from git, invokes each production version and saves actual reports and emitted artifacts. `BEFORE.json` and `AFTER.json` contain UTF-8 file contents; `$TEMP` substitutes only temporary root paths.

- Missing table: before edit ran; after no downstream stages. Body and original result unchanged in both.
- Length truncation: before edit ran; after no downstream stages. Body and original result unchanged in both.
- Normal complete batch: same no-change edit and missing-front/back stop before and after. No new material, recovery, batching or source changes.

13 focused gate tests cover these and stale/mixed reports. Existing synthetic integration separately verifies complete bracketed identities can still resolve through explicit catalogs. `problems_resolved` and this gate are engineering states, not scientific review certification.
