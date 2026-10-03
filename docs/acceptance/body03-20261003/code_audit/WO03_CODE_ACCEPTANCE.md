# WO03 code acceptance — local read-only audit

## Result

**PASS for the scoped integration behavior.** Commit `8810c7bf38227f2987d7510937bdf9001ffd47ad` was audited against `4bb82be54d8d812c2d11b1e3a45e8ff863af0ee4` in the acceptance worktree. The query initializer, cross-stage compatible-need reuse, changed-output delta requests, independent supplement-attempt persistence, retry material preservation, and coordinator `chapter_feedback` handoff pass the bounded offline normal-path checks.

The earlier failure in `tests/upgrade3/test_body03_retrieval_adapter_contract.py::test_returned_attempt_paths_reach_actual_chapter_owner_message` was a Windows JSON-escaping false negative rather than a production handoff loss. That blocker diagnosis is withdrawn. Root applied a test-only portability correction: the fixture reader now specifies UTF-8 and the assertion inspects the parsed `supplement_material.judgment_path` and its `Path.parents`. No production code changed.

## Changed

The commit changes only the three scoped production modules:

- `optomind_research/runtime/upgrade3/planning_retrieval_loop.py`: durable first-query initialization/checkpoint, owner-independent need signatures, acceptance-sensitive need merging, partial/provider state recovery.
- `optomind_research/runtime/upgrade3/planning_supplement.py`: atomic attempt allocation, delta-input persistence, truthful fulfillment normalization, and preservation of partial/failed material.
- `optomind_research/runtime/upgrade3/progressive_review_plan.py`: gap/feedback compaction, cache/reuse and source fingerprints, pool/index updates, supplement and chapter handoff assembly.

No production or Git files were edited by this audit. Root's shared test-only portability correction is preserved in `LOCAL_FIX.patch`. Prompts and BODY stage order were unchanged in the diff; no live retrieval, model call, credential use, or network access was performed.

### Focused follow-up on the path assertion

The saved failing fixture contains a real attempt at `...\\run\\level2\\external\\...\\round_1\\supplements\\G1\\attempt-pss2eoeh`. Its `SUPPLEMENT_INDEX.json` reports `status=fulfilled`, `material_ready=true`, and useful material `Measured mechanism at 25 C.`. Its `PLANNING_POOL.jsonl` stores the absolute attempt descendant in `supplement_gap_material.judgment_path`; the chapter packet `run/stages/chapters/CH01.json` carries that same `supplement_gap_material` and `supplement_gap_materials` plus the original `card_path`.

The chapter-details `projected_source()` retains the supplement object, so the parsed path exists and `Path(judgment_path).is_file()` is true. The failing expression checks `actual['output_dir'] in json.dumps(messages[0]['source_materials'])`; on Windows `json.dumps` emits `<LOCAL_PATH>/\\Users...`, while `actual['output_dir']` contains `<LOCAL_PATH>/Users...`. Searching serialized text therefore fails even though the parsed nested path is present. Checking the parsed nested value (or escaping the expected path before searching) makes the assertion portable.

Downstream code preserves the locator: `chapter_arrangement._source_catalog()` reads `supplement_gap_material`, `supplement_gap_materials`, and `card_path`; `build_source_catalog()` emits packet/card locators; and `review_unit_writer._material_entry()` copies supplement material and resolves the card locator before writing. The minimum change was to the test assertion only. There is no concrete production harm: `relevant_tool_feedback` has `output_dir` and `derived_pool_path`, the owner receives the useful sentence, the packet preserves the attempt judgment path, and arrangement/writer retain the card locator.

## Verify

Exact bounded selection from `docs/workorders/body-chain-20261003/records/03.md`, with `socket.create_connection` and `socket.socket.connect` replaced by raising guards, and the two documented full fake-chain tests excluded:

```text
PYTHONPATH=. python - <<'PYTEST'
import socket, pytest
def deny(*args, **kwargs):
    raise AssertionError('Network disabled for WO03 offline checks')
socket.create_connection = deny
socket.socket.connect = deny
files = [
    'test_directed_reading_real_store_contract.py',
    'test_directed_reading_adapter_contract.py',
    'test_progressive_review_plan_material_reuse.py',
    'test_progressive_review_plan_owner_recovery_contract.py',
    'test_progressive_review_plan_batch_recovery_contract.py',
    'test_progressive_review_plan_chapter_cache_contract.py',
    'test_progressive_review_plan_stage_cache_contract.py',
    'test_progressive_review_plan_transport_retry.py',
    'test_progressive_review_plan_chapter_capacity.py',
    'test_progressive_review_plan_chapter_recovery.py',
    'test_progressive_review_plan_case_chain.py',
    'test_planning_feedback_scope.py',
    'test_planning_retrieval_query_initialization.py',
    'test_planning_supplement_attempt_contract.py',
    'test_body03_retrieval_adapter_contract.py',
]
raise SystemExit(pytest.main(['-q', *['tests/upgrade3/' + name for name in files],
    '-k', 'not test_run_consumes and not test_revision_chain_runs']))
PYTEST
```

After the test-only portability correction, the exact socket-denied selection completed with **212 passed, 2 deselected** in 11.93s, without `PYTHONUTF8=1`. The 64 new WO03 tests all passed. No production changes were made.

Additional bounded checks:

```text
python -m compileall -q \
  optomind_research/runtime/upgrade3/planning_retrieval_loop.py \
  optomind_research/runtime/upgrade3/planning_supplement.py \
  optomind_research/runtime/upgrade3/progressive_review_plan.py \
  tests/upgrade3/test_planning_retrieval_query_initialization.py \
  tests/upgrade3/test_planning_supplement_attempt_contract.py \
  tests/upgrade3/test_body03_retrieval_adapter_contract.py
```

Result: `compileall_exit=0`; `git diff --check`: exit 0.

The focused query/supplement/adapter selection (64 tests) also passed completely. The real archive owner-feedback test preserves both `chapter_feedback` rows plus `P0004`/`P0586` source material in the owner input and persisted cache.

## Blockers

1. The absent `/tmp/optomind-wo01-deps` path referenced by the cloud record was not needed for the local run; local pytest 7.4.4 and Python 3.12.4 executed the selected tests.
2. No full suite, live model call, credential use, or network access was performed.

Next real-test choke point: verify the live owner accepts the preserved supplement content and packet/card locator. This offline audit found no attempt-path integration blocker. Live quality, scientific correctness, and full BODY behavior remain unverified by this audit.

The exact test-only local correction is preserved at `LOCAL_FIX.patch` beside this report; it is not a production fix and was not committed by this audit.
