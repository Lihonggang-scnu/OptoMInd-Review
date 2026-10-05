# BODY40 second-stop alias boundary review

Reviewed on 2026-10-05 against accepted first-stop e75c66c, in OptoMInd-Review-body40-alias-material. Read first-stop START_AND_SCOPE.md and INDEPENDENT_REVIEW.md, and second-stop LOCAL_ACCEPTANCE_ALIGNMENT.md. Native local acceptance is reported provenance only: this review did not inspect the not-yet-uploaded native evidence. No production edits, commits, paid calls, network retrieval, full BODY generation, or historical manuscript edits were performed by this reviewer.

## Outcome

No remaining concrete blocker in the reviewed consumer change after the completion tool-selection correction. This is a bounded engineering review, not scientific quality or full pipeline certification.

## Finding closed during review

Canonical-only task P0049 plus a chapter-wide usable tool citing declared alias P0605 initially retained the tool in build_unit_view but silently dropped it in build_completion_payload. The completion matching expansion was initially one-way. The worker corrected selection to include aliases of each selected canonical identity. The exact independent probe now retains one tool in both paths, and one canonical source. A committed-test candidate in the worker's new test file covers this case.

Initial test failures also included two simulation-banner assertions and false sources_without_any_material on usable tool-only sources. The latest tests account for the unchanged simulation banner; the warning predicate now recognizes actual supplement/linked-tool text while metadata-only records still warn.

## Independent tests

Environment: PYTHONHASHSEED=0; PYTHONPATH=docs/workorders/body-chain-20261003/records/body40_identity_citations/offline_bootstrap:/tmp/optomind-body40-deps:.

- 129 passed: tests/upgrade3/test_body40*.py plus test_review_unit_writer_completion.py, test_review_unit_writer_portable_paths.py, test_body07_arrangement_writer_short_chain.py, test_chapter_arrangement_identity_fallback.py, test_feedback_loop_chapter_tool_materials.py (all bare filenames under tests/upgrade3).
- 8 passed: /tmp/test_body40_alias_boundaries_review.py. These independently exercise restored brief aliases; unknown alias no-inference and completion rejection; source_identity_map DOI/paper/canonical-paper conflict guards; catalog key versus internal identity conflict; foreign card content exclusion; and symmetric completion tool matching.
- /tmp/body40_alias_review_probe.py reproduced and rechecked the completion issue and the inherited conservative locator behavior.

## Reviewed boundaries

- Only explicitly declared catalog aliases resolve. Same titles/DOIs without an alias do not restore missing source material.
- Alias-only, canonical-plus-alias, reversed order, and restored brief paths produce one canonical material row while retaining task handles and details.
- Alias ownership collisions, canonical-key collisions, self aliases, malformed nonempty declarations, inconsistent canonical source_handle, and contradictory available alias packet identities fail closed.
- Different historical packet paper IDs with equal normalized DOI remain eligible, as required by the actual producer's duplicate-record identity shape. DOI disagreement still blocks.
- Completion uses the selected material subset, recognizes canonical and alias citations, preserves existing-body prefix, and accepts explicit caller numeric mappings. Generated numeric mappings retain first-stop confirmation protections.
- Normal writer diagnostics recognize declared identities, unknown nondeclared citations remain unknown, and unused-source reporting compares canonical identity classes.
- Foreign locator-card material is not injected; canonical catalog material remains intact.
- Production diff is confined to review_unit_writer.py. No producer, prompt, ordering, science, case, owner recovery, or downstream reviewer changes observed.

## Explicit nonblocking inherited limits

1. _read_card_material already rejects a card with a different nonempty paper_id even when DOI matches. An alias-ID locator card therefore produces material_identity_conflict=foreign_locator_card_ignored and a locator_identity_conflict note; its extra fields are not merged. The independent probe retained canonical A material unchanged. This condition is present in e75c66c, and the current change consumes the merged canonical catalog rather than broadening locator identity authority. No new regression was established.
2. The producer's existing treatment of distinct duplicate A/B snapshots remains outside this consumer repair. Do not describe this stop as recovery of all historical duplicate material or silently rewrite the producer.
3. Packet identity checks use the available selected locator packet. Offline exports without that packet still consume the declared catalog aliases. This is catalog authority, not independent verification of unavailable source evidence.

## Reviewed snapshot hashes

- review_unit_writer.py SHA-256: 8987c064884ace76c7481834714897f4c215a8aa3fe6498b8dc089eabf8c2538
- test_body40_explicit_alias_material.py SHA-256: 66aad165fa07f15b34d2feda85c7a845d82afbf5915e38b0f3257634cf58560b
