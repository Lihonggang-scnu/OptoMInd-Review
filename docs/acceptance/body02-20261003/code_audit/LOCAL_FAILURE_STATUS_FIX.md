# WO-02 local failure-status fix

- Checkout: `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree`
- Source baseline: `5a8549200e5ee9e7be1992ca0cea19e1b7125487`
- Scope: `progressive_review_plan.py` directed-reader aggregate and directed retrieval closure, plus three focused adapter regressions.
- Paid calls, credentials, network access, commits, and pushes: none.

## Change

`make_directed_reading_runner` now exposes `provider_failed` and `failed_paper_ids` when failed rows leave no useful material, and reports aggregate `status: "failed"`. Existing attempted/consumed paper accounting is unchanged.

`make_retrieval_loop_runner` propagates failed/provider-error status into the existing retrieval loop retry contract. `provider_failed` is emitted only when the directed result has no useful material. Mixed useful partial material remains `partial`; its failed rows and materials remain visible.

## Added regressions

- All-failed reader output reports failure metadata without changing attempted/consumed IDs.
- Mixed useful partial plus failed rows remains partial and retains the useful material.
- The real retrieval closure keeps a persistent provider failure in `round_1`, using the configured one retry, without opening a later scientific round.

## Verification

Exact commands and observed results:

```text
python F:\OptoMind-Review-2\outputs\cloud_body02_real_acceptance_20261003\code_audit\probe_provider_failure_rounds.py
```

The probe's direct engine controls remained unchanged: ordinary `unmet` made three scientific-round calls for retry 0 and retry 1; explicit `failed` made one same-round call for retry 0 and two same-round calls for retry 1. The actual directed adapter made two calls, both under `round_1`, with `provider_retry` journal entries and no later scientific round.

```text
python -m pytest tests/upgrade3/test_directed_reading_adapter_contract.py -q
```

`19 passed`.

```text
python -m pytest tests/upgrade3/test_directed_reading_real_store_contract.py -q
python -m pytest tests/upgrade3/test_progressive_review_plan_material_reuse.py -q
python -m pytest tests/upgrade3/test_planning_feedback_scope.py -q
```

`26 passed`, `13 passed`, and `5 passed` respectively.

The recorded WO-02 control selection was run with the existing `-k 'not test_run_consumes and not test_revision_chain_runs'` filter:

```text
python -m pytest -q tests/upgrade3/test_directed_reading_real_store_contract.py tests/upgrade3/test_directed_reading_adapter_contract.py tests/upgrade3/test_progressive_review_plan_material_reuse.py tests/upgrade3/test_progressive_review_plan_owner_recovery_contract.py tests/upgrade3/test_progressive_review_plan_batch_recovery_contract.py tests/upgrade3/test_progressive_review_plan_chapter_cache_contract.py tests/upgrade3/test_progressive_review_plan_stage_cache_contract.py tests/upgrade3/test_progressive_review_plan_transport_retry.py tests/upgrade3/test_progressive_review_plan_chapter_capacity.py tests/upgrade3/test_progressive_review_plan_chapter_recovery.py tests/upgrade3/test_progressive_review_plan_case_chain.py -k 'not test_run_consumes and not test_revision_chain_runs'
```

`143 passed, 2 deselected`.

```text
python -m compileall -q optomind_research/runtime/upgrade3/progressive_review_plan.py tests/upgrade3/test_directed_reading_adapter_contract.py
python -c "from optomind_research.runtime.upgrade3.progressive_review_plan import make_directed_reading_runner, make_retrieval_loop_runner; print('imports ok')"
python scripts/upgrade3/directed_reading.py run --help
git diff --check
```

All four checks passed; the CLI advertised `--retry-empty-result`.

## Full upgrade3 run note

The full command `python -m pytest tests/upgrade3 -q` observed `238 passed, 11 failed`. The failures were delivery tests requiring historical assets absent from this slim worktree. They were not rerun against a fresh baseline, so their baseline status is unverified:

```text
tests/upgrade3/test_review_delivery_editing.py::test_text_edit_stage_applies_fixture_to_real_draft
tests/upgrade3/test_review_delivery_editing.py::test_text_edit_second_run_is_no_change
tests/upgrade3/test_review_delivery_editing.py::test_front_back_updates_existing_parts_and_inserts_missing
tests/upgrade3/test_review_delivery_editing.py::test_front_back_inserts_all_parts_into_plan_draft
tests/upgrade3/test_review_delivery_editing.py::test_intro_repeat_runs_do_not_accumulate
tests/upgrade3/test_review_delivery_editing.py::test_intro_changed_version_keeps_only_new
tests/upgrade3/test_review_delivery_editing.py::test_intro_located_by_role_when_title_lacks_the_word
tests/upgrade3/test_review_delivery_figures_citations.py::test_real_draft_map_and_reader_rendering
tests/upgrade3/test_review_delivery_figures_citations.py::test_stage_cross_refs_captions_and_map_all_agree
tests/upgrade3/test_review_delivery_integration.py::test_history_full_chain_via_cli_subprocess
tests/upgrade3/test_review_delivery_integration.py::test_plan_restricted_chain_via_entry
```

No production prompt, retry framework, or quota semantics were changed. No commit was created.
