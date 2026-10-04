# Independent second-stop review

## Verdict

Accepted for bounded offline second-stop handoff. No remaining blocker found in the reviewed F4A/F4B, first-case independent-reading handoff, and actual case batch cache scope. This is engineering compatibility evidence, not scientific-quality, full-pool, or full-BODY acceptance.

The reviewer made no production edits. Production changes are confined to the existing planner and practical reader; general planner/reader prompts, citation policy, BODY ordering, and the one-table/multiple-task policy remain unchanged. No paid call, real research download, project credential access, commit, or push was performed.

## Independent checks

- 134 focused tests passed (see `FOCUSED_CONTROLS.txt`): F4 and case consumers, three permanent reviewer regressions, stage cache, case chain, material handoff, real reader/store, adapter, and material reuse controls
- The exact three reviewer regressions failed against baseline `d82541f04341558698657086667f43c5fb3865cd` and pass after repair. `replay_baseline.py` loads only baseline production modules without modifying the checkout; tests and unchanged dependencies stay current
- Ten additional independent temporary probes passed during review. These overlap the final checked-in suites and are not added to the 134 count
- `_messages_for`, `_planner_instructions`, and `build_practical_reader_messages` ASTs are unchanged from baseline; syntax and diff checks pass
- Actual before/after reader user payloads, artifacts, current cache material, chapter messages, and packet source material are in `baseline/` and `after/`. All examples are synthetic

## Defects established and resolved

1. Initial chapter detail previously sent explicitly foreign A/B under the current source title. Refresh could retain stale foreign A/B with only a conflict flag. Current material construction removes the contradictory channel; missing legacy card identity remains eligible, and compatible independent readings are not disqualified by a bad A/B card
2. Lower practical reader/store and adaptive journal could exempt a task after source body or referenced identity changed. Current identity and the exact cleaned body/bibliography are now bound at the actual reusable consumers. Same content moved to a new directory or renamed snapshot remains reusable. Useful unverified history is not treated as fulfilled current work
3. The first patch combined current source identity with the old stored title. Independent actual-message inspection caught this; current caller identity now remains coherent through source binding, message, and artifact
4. The first patch could splice an explicitly foreign DOI answer, or superseded same-identity source content, into the current material. The final actual adaptive-reader-to-chapter tests show only `CONTROLLED_ANSWER_2` in active current material and messages; old artifacts stay byte-identical on disk. Same-source new-question history behavior remains covered by adjacent controls
5. Independent partial original-study content reported through a review previously reached case selection but disappeared at attachment. It now reaches the formal case, packet, arrangement catalog, and standalone writer messages without requiring its own A/B, abstract, or fulltext. See the separate `../cases/` actual writer evidence
6. Actual case batches previously reused old answers after effective model/output/prompt changes. The final cache uses the existing effective request contract. Selection-visible material changes already invalidated baseline and continue to do so; fixed case thinking settings and location-only changes correctly reuse
7. Independent review caught a temporary regression overwriting the target chapter's substantive same-handle material from another chapter. The final case suite permanently protects chapter-specific context and guarded upgrades of identity-only rows

The root-review-note source exemption still separately requires a matching task contract and fulfilled current answer before suppressing work. A note alone does not fulfill an incompatible task. Review-derived original content retains ordinary substantive eligibility with its supplied citation identity.

## Evidence map

- `BASELINE_FAILURES.txt` / `AFTER_TESTS.txt`: exact three-test before/after comparison
- `baseline/CURRENT_TITLE_CONSUMER.json` / `after/CURRENT_TITLE_CONSUMER.json`: old stored title versus actual current lower-reader messages/artifact
- `baseline/SOURCE_CORRECTION_CONSUMER.json` / `after/SOURCE_CORRECTION_CONSUMER.json`: same-identity body correction, actual adaptive cache and chapter consumer
- `baseline/DOI_CONFLICT_CONSUMER.json` / `after/DOI_CONFLICT_CONSUMER.json`: same-ID explicit DOI/body correction without foreign-history reuse
- `VERIFICATION.json`: prompt AST checks, production-file hashes, actual call counts, old disk preservation, and active-message exclusion
- `tests/upgrade3/test_preflight_material_review_regressions.py`: permanent reviewer tests; case-specific regressions live in `test_preflight_case_material_cache_consumer.py` to avoid duplication

Before/after artifacts intentionally include earlier material in their labeled `before`/`before_material` fields for comparison. That is separate from `current_material`, `chapter_messages`, and `packet_source_materials`, which contain no superseded answer after repair.

## Reproduce

From repository root, with normal dependencies available:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/optomind-stage2-deps:. python docs/workorders/body-chain-20261003/records/preflight_materials/review/replay_baseline.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/optomind-stage2-deps:. python -m pytest -q -p no:cacheprovider tests/upgrade3/test_preflight_material_review_regressions.py
```

The baseline command is expected to exit with three failures. Set `BODY_PREFLIGHT_REVIEW_EVIDENCE_ROOT` to a fresh directory to retain actual synthetic before/after consumer records. The selected evidence does not imply the earlier real-data scientific limitations have been repaired, nor authorize a paid/full-BODY run.
