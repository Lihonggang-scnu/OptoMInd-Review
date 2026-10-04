# Second-stop case material and batch reentry

## Result

The full offline consumer test reproduces the original loss: P0002 is independently read, has a citation identity and useful partial original-study content reported through a review, but has no own A/B card, abstract, full text, or snapshot. The actual case model input marks its material available; baseline attachment drops the accepted case and its only reading. Both legacy and BODY-revision modes fail before repair.

After repair the same existing reading reaches `supporting_studies`, the persisted writer packet, the validated arrangement's source catalog, and the actual standalone writer CLI messages. The long material suffix absent from the bounded selection projection remains present in the writer. A new planner instance resumes with no case call and byte-identical writer messages. This does not claim whole-run zero calls: in this legacy fixture, upstream level1 outline, final scope and whole-plan improvement still reenter because earlier theme context is not reconstructed; their behavior is unchanged by this case-only repair. Conditions, limits, partial status and citation DOI remain present; no additional source acquisition is required.

## Small production changes

- Share existing identity-aware case material resolution between selection and attachment, clipping only the selection projection
- Restore case-local material from explicit prior readings, already-returned tool results and the current read store (latest wins), including fresh-process legacy-cache resumes; the legacy tool retry policy is unchanged
- Reuse the existing `_cache_contract` with the complete actual batch payload before batch-cache lookup, covering live prompt text and effective model/output/thinking settings; no second cache framework
- Preserve already-substantive chapter-specific material; upgrade an identity-only target through the existing guarded material resolver, retaining local metadata

The actual cache repro justified the settings fix. Baseline incorrectly reuses model, output-token and live-prompt changes. It already invalidates selection-visible material changes. The case adapter fixes thinking at 2048, so changing the general thinking setting is deliberately a reuse. A material suffix beyond the selection clip also reuses the selection answer while freshly attached full material delivers that suffix to the writer. Metadata-only output-path changes do not require a new call. Old batches without the complete contract are retried locally; failed batches remain retryable.

## Evidence and verification

- `FIRST_SELECTION_RESULTS.json`: before/after actual selection and persisted packet
- `BATCH_REENTRY_RESULTS.json`: ten actual batch reentries, exact effective settings and persisted signatures
- `DEEP_HANDOFF_PROJECTION.json`: formal packet material, catalog, validator result and actual writer source
- `FRESH_WRITER_MESSAGES.json` / `RESUMED_WRITER_MESSAGES.json`: exact standalone writer messages
- `WRITER_MESSAGE_COMPARISON.json`: byte equality and SHA-256 checks
- `BASELINE_PYTEST.txt`: 10 expected failures, 5 controls pass (the exact same final 15-test file), against untouched `d82541f` snapshot
- `PATCHED_PYTEST.txt`: 83 dedicated and case-adjacent tests pass
- `FINAL_FOCUSED_PYTEST.txt`: 19 dedicated plus independent reviewer controls pass after the last small metadata-map adjustment

Dedicated suite: `tests/upgrade3/test_preflight_case_material_cache_consumer.py` (15 tests). The initial failure-first pass used 12 case/cache tests (8 failed, 4 passed); the final exact-file baseline comparison additionally includes wrong-DOI rejection, existing chapter-context preservation and thin-target metadata preservation (10 failed, 5 passed). The thin-target helper control fails on baseline because the new optional `read_materials` argument is absent; this is an API compatibility difference, not independent proof of the original loss. That loss is proven separately through full `run()` in both modes. It runs the real `ProgressiveReviewPlanner.run`, Qwen adapter, inner cache, packet writing, arrangement validator/export and standalone writer CLI. The provider is synthetic and sockets are denied. The adapter's provider ledger/tokenizer initialization is bypassed; its actual call/settings logic runs. One prompt-cache control modifies only in-memory case prompt text. No execution, material-resolution, attachment or cache functions are stubbed.

Environment: `PYTHONPATH=/tmp/optomind-stage2-deps:. PYTHONDONTWRITEBYTECODE=1`. To retain the complete synthetic replay set `BODY_PREFLIGHT_CASE_EVIDENCE_ROOT` to a fresh directory. The compact files here are selected evidence rather than that full replay tree.

Unchanged prompt ASTs (`_messages_for`, `_planner_instructions`) and `git diff --check` pass. No prompt file change, scientific rewrite, BODY-order change, post-case owner rewrite, second planner, live call, project credential read, real research download, commit or push was made. This is engineering handoff evidence, not scientific/full-BODY quality acceptance. The existing one-table/multiple-task policy is not changed.

## Commands

```sh
PYTHONPATH=/tmp/optomind-stage2-deps:. PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/upgrade3/test_preflight_case_material_cache_consumer.py \
  tests/upgrade3/test_progressive_review_plan_case_chain.py \
  tests/upgrade3/test_body04_late_source_routes.py \
  tests/upgrade3/test_body05_global_argument_handoff.py \
  tests/upgrade3/test_body07_retrieval_short_chains.py \
  tests/upgrade3/test_progressive_review_plan_body_boundary.py \
  tests/upgrade3/test_progressive_review_plan_chapter_recovery.py
```

Exact-file baseline: copy only the final dedicated test to a temporary path, keep the `d82541f` source snapshot unchanged, then run that test from the snapshot root with the same PYTHONPATH prefix. `EXACT_TEST_FILE_COMPARISON.json` records identical test bytes.
