# WO04 owner material admission and adoption

## Basis and bounded decision

Read the R2 acceptance handoff, acceptance review, all three unedited results, request metadata and real-run summary; then WO04, the F05/F08/F09/F11 records and referenced audit artifacts. R2 already improved actual local reading. Its remaining scientific misstatements are not repaired or certified here. No prompts, scientific answers, model policy, BODY stage order, retrieval/download behavior or budget were changed.

Current code inspection confirmed F05 at the tested R2 base (`1b4f7b6a7c0dafb6f7dfe59f46980480f44d993f`):

- Owner closure accepted candidate channels in its signature but did not consume them
- A current-pool source without its own A/B was rejected before its usable supplement or directed reading was considered
- Identity-only source rows satisfied validation, while nested paragraph references escaped validation
- An owner could read a candidate but could not adopt it because validation only considered the earlier closed source slice
- The feedback loop did not copy returned owner source rows into its persisted writer packet
- Same-handle identity conflicts and foreign saved cards could splice one study's identity to another study's A/B

Already present and preserved: chapter-detail packets append candidate materials, explicit selected pool handles can be closed, chapter-owned supplements attach only to their chapters, and owner recovery keeps a compatible earlier success. Historical route counts did not prove all late papers were lost. F11 and the downstream citation seam have separate records.

## Repair

The existing closure checks supplied source/candidate/navigation, saved A/B, directed, local-passage, supplement and source-bound tool channels. It reads pool cards only for selected or already present handles. A narrow content-presence check recognizes actual material fields; it does not score evidence, require an original paper's own A/B, reduce review-derived material weight, or infer scientific findings from titles, status, proposed use, unanswered questions or bibliography.

The same recursive structured-handle collector drives selection and nested validation. An updated response is closed against material actually supplied to that owner, not any findings invented in the response. Successful adopted rows and identities reach the authoritative packet; invalid responses keep the previous plan. Post-case owner input retains its compact projection and only adds handles explicitly selected by feedback/case suggestions. Material cache signatures include the newly accepted channels. Feedback replay fingerprints the complete persisted material, avoiding a second call for an unchanged adopted candidate.

Paper-ID aliases and DOI identity conflicts are explicit. Saved-card refresh and direct pool payload construction preserve old usable content while declining foreign A/B. Legacy cards without identity metadata still work. Tool source identity is resolved before folding, and unit-scoped/multi-source narratives remain chapter-level with their complete source relationship.

## Actual synthetic boundary evidence

`capture_owner_handoff.py` loads the tested R2 progressive module from Git and the current implementation, with current local dependency modules. It denies network access and supplies a deterministic owner callback; it is not a historical full-request replay or a real model-quality test.

- `before_owner_messages.json` / `after_owner_messages.json`: actual owner message-builder output; both already contain the review-derived candidate and its identity
- `before_owner_result.json`: identity-only row accepted; adopted candidate rejected as unavailable; feedback partial and no writer input created
- `after_owner_result.json`: identity-only row rejected; candidate adopted; unchanged replay reuses after one total owner callback
- `after_formal_packet.json`: actual persisted updated packet contains `SOURCE_BOUND_RESULT`, original-study DOI and source handle
- `after_writer_input.json`: actual feedback writer-callback input, not a unit LLM request. Separate writer-handoff tests/records establish the real unit-message projection

The synthetic study reports classroom randomization and a score result. The comparison checks that content and original-study identity survive, not merely inventory counts. No table-count criterion was introduced.

## Offline checks and limits

- Initial red controls: 8 failed / 1 passed, before production changes (`OWNER_FAIL_FIRST.txt`)
- Final owner-specific controls: 23 passed (`OWNER_TARGETED_TESTS.txt`)
- Existing bounded R2 controls: 251 passed / 2 deselected (`OWNER_BASELINE_CONTROLS.txt`)
- Shared owner/writer/late-routing controls at this checkpoint: 46 passed (`OWNER_SHARED_TESTS.txt`)
- Python compilation and `git diff --check` passed

Commands: `python -m pytest -q tests/upgrade3/test_body04_owner_material_handoff.py`; `python docs/acceptance/body03-local-lookup-real-20261003/records/evidence/run_offline_controls.py`; `python docs/workorders/body-chain-20261003/records/body04/capture_owner_handoff.py`. Use the authorized local dependency environment. The bounded control runner and evidence capture deny socket connections.

One existing assertion now expects `study_material_missing`, because absence of one's own A/B is no longer the failure definition. The existing owner-case fixture also now keeps its overridden pool ID consistent with its planning-view ID; the old fixture accidentally described two papers. No production content requirement was weakened to satisfy that fixture.

No paid/live calls, credentials, original-paper downloads, complete BODY run or scientific accuracy validation were performed. Local real-model quality acceptance remains required. This stage does not prove every model-selected source is scientifically adequate or every final paragraph accurate.
