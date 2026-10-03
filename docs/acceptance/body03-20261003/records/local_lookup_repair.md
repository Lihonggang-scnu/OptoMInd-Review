# WO03 local lookup repair

Status: READY_FOR_ASTRA_03_LOCAL_LOOKUP — acceptance alignment recorded before production edits

Baseline: `91543f7c7e43515eb8e3c8cc86ee09ed54e486c5`
Branch: `body03-local-lookup-cloud-20261003`
Authorization: user message `Sentinel_6930140e637c8191aeb49ab4a1c81cbc`

## Alignment with actual local acceptance

Read README, ASTRA_ACCEPTANCE, OWNER_ROOT_CONTENT_REVIEW and compared the persisted owner payload, prior/current plans and response; reviewed the three retrieval phases, query/triage outputs, LOCAL_MATERIAL_LOOKUP, WO03_CODE_ACCEPTANCE and REPAIR_INSTRUCTIONS. Reconstructed owner messages are explicitly not an original provider envelope. Sanitized records omit source-paper prose; the synthetic selection fixture is not the original index.

- The owner really receives three feedback rows, including two previously lost action/reason rows. P0586 now supports an independent glucose-limited T-cell fuel paragraph; P0004 now supplies an orthotopic PDAC boundary example rather than only a source handle. Two units remain, with six to seven paragraph tasks. OMV detail is partly reduced and the PNP-expression claim is too strong; these accepted content limitations do not justify prompt edits here.
- Owner input has eleven source-material records plus P0004 in a separate late-material field; this must not be confused with twelve records in one field.
- All three local retrieval phases fail to answer the requested question and stop honestly. Their repeated calls cannot establish failure to reuse a fulfilled task. Generated concrete queries alone do not demonstrate successful external recall; external retrieval was disabled.
- The archived audit establishes that P0004 has 320 indexed segments, rank seven for the stored question, outside the six-paper candidate window. The global five-passage prefix then reduces actual paper coverage to P0597/P0087/P0479. Expanding only candidate count does not fix the second cutoff.
- Existing handles merely mark already selected hits. The repair must resolve explicit stable identities/current handles and inspect actual material in the judge request, without declaring it relevant or fulfilled in advance.
- The Windows path assertion was a test-only escaping issue; material and locators were present. Keep the baseline portability correction and production message style.

## Planned bounded repair

Use existing index/search and production adapter/message construction to (1) let explicitly identified local papers contribute relevant bounded passages and (2) permit one insufficient-first-read expansion with broader paper coverage and a bounded material budget. Preserve ordinary first-read size, satisfied reuse, prior partial material, failed attempts, owner feedback and BODY order. No new retrieval service, planner, scientific prompt, paid call, live retrieval/download, full planning or BODY run.

Implementation, before/after message evidence, commands and limits will be appended after verification.

### Provenance caveat

The archive README reports max_provider_retries=2/shared_deep_read_budget=5, while per-phase serialized retrieval configurations report 1/40. This repair does not infer which undocumented driver setting was intended. Per-triage model_calls=1/cost_cny=0 are not the overall ledger totals: the local receipt reports twelve retrieval-related calls (two local judgments and two refinements per phase). Top-level complete means execution returned, not the need was answered. No production change is based on the conflicting metadata.

### Baseline control check

Before production edits, the exact original socket-denied WO03 selection passed: 212 passed, 2 deselected in 3.81s. The two excluded cases are the same full simulated chain tests documented in records/03.md. The archive's metadata-only selection reproducer reports rank seven outside the first six; it is not a real index replay.

## Implemented path

1. `planning_material_search.search` accepts an optional stable paper-ID restriction before the existing BM25 candidate limit. Normal search scoring is unchanged. `nominated_paper_passage` supplies one existing substantive summary/opening segment only for a resolved explicit nomination with no lexical match; it skips reference/declaration material. This gives the judge an opportunity to reject or use the material, never a relevance verdict.
2. `prepare_local_reading` preserves the original ordinary first selection. Stable nominations add bounded passages using the current identity map, not an old P label. `read_local_capture` performs at most one additional judgment over previously unread candidate material: at most twelve ranked papers, with a 24,000-character incremental passage-text budget, original passages and context retained. Long intact segments have a spare-budget second opportunity; impossible/capped nominations are reported, not silently treated as absent research.
3. `run_gap_local_triage` resolves stable IDs and current handles, retains initial useful content through an expansion or failure, and records exact judge-input attempts. The checkpoint hashes actual constructed input, stable source identities, relevant model settings and the unchanged system instruction. A compatible successful or partial read reuses its result; explicit failure retry receives a new attempt directory. An infrastructure failure remains visibly failed and does not trigger a search for supposedly absent literature.
4. `make_retrieval_loop_runner` and the direct supplement adapter pass known-paper identity and shared local checkpoint information through the real local adapter. Existing cross-stage answer reuse remains separate from chapter ownership. New acceptance requirements receive the missing criteria and previous useful material. Resolved nomination/index-generation changes prevent stale stopped-journal bypass; content-compatible historical material is retained separately from answer-reuse eligibility. Empty WAL creation by a read-only SQLite connection does not invalidate acceptance coverage.
5. `run_retrieval_loop` uses its existing pending/provider-retry state for local judge failure. Failure-only diagnostic fields survive the existing tool-material projection into the chapter input. This changes data handoff only; no BODY scientific instruction, chapter/case sequence, arrangement or writer algorithm changed.

## Actual message comparison (synthetic fixture)

The two adjacent JSON files capture actual production-constructed requests with only model/network boundaries replaced:

- `local_lookup_before_synthetic.json`: exact archived production code loaded from a separate baseline extraction, not a manually reconstructed old message
- `local_lookup_after_synthetic.json`: repaired production code, real temporary SQLite FTS and real adapter/message constructors

Both fixtures use the same generic coating task, six neighboring sources and a seventh source with the distinctive synthetic observation. They are not the user's original index, original provider recording, or a scientific-quality result.

Before: top-six selection sends source handles `N0001, N0001, N0002, N0002, N0003` (465 material-text characters). Merely raising the candidate window to twelve sends the same five passages; the target observation is still absent. Both boundary-substitute judgments remain external_research.

After: the first request retains exactly that normal selection. The one expanded request adds the other candidates, including `TGT07`, and retains the original qualifier. Total material text in this small fixture grows to 1,218 characters, well below the incremental bound. The distinctive text actually arrives:

> SYNTHETIC_COUPON_RESULT: In the cycled coupon, a porous interlayer delayed crack initiation at 600 C.

The controlled model can then return that supplied observation; it appears in the actual chapter input for CH01 and CH02, with the current source identity retained in the material record. There are two local model-boundary calls total across both owners, not two calls per owner. A separate education fixture checks the same route with a delayed-recall/transfer distinction.

Failure test: first useful partial + expansion timeout gives two calls; replay without explicit retry remains at two; explicit retry reaches three and preserves the old failure attempt and initial useful material. A current-handle mapping never selects a different paper merely because an old label matches. A nominated unrelated source remains unanswered. An ordinary sufficient first read makes one call and no expansion.

## Verification scope

Baseline: original 212 controls passed, two documented complete simulated-chain cases deselected. New contract tests against the locked baseline show the intended defects (14 fail, 2 pass); failures include unavailable new adapter arguments as well as missing actual material, so the message comparison above is the direct evidence of the old selection defect.

Final counts and commands are recorded below. Tests replace model/network boundaries, not the SQLite search, local reader, message construction, journal or material handoff. No full suite, full planner or BODY generation is part of this run.

## Remaining limits

- This establishes bounded material delivery, retention and reuse. It does not establish whether a real model selects the best passages, synthesizes them accurately, or improves the final BODY. Local Astra owns that next test.
- Twelve candidates and 24K additional passage characters are operational bounds, not quality targets. The unchanged initial input can be larger; 24K is not a cap on the whole request. Explicit nominations and the later expansion each have a bounded incremental allowance. Oversized intact passages can remain omitted and are reported.
- A nominated zero-match fallback is one available substantive summary/opening. It may not contain the desired deep section; no full-paper reread or download is introduced.
- The existing global FTS candidate bound and lexical ranking remain. This patch cannot guarantee discovery of every relevant source in an arbitrarily large index.
- The existing query refiner feeds the external-retrieval path; it does not automatically replace the next local query. This local expansion uses the original question and any existing judge read-focus. No new query scheduler was added.
- SQLite/WAL generation is a conservative replay-invalidating signal, not a scientific material-equality proof. A checkpoint or unrelated index change may cause inspection again; unchanged actual judge messages still reuse their checkpoint. Content-compatible previous material is retained.
- Cross-task inherited material is supplied automatically by the existing retrieval-loop adapter. A standalone low-level caller must provide `reusable_material` when changing its requirements; no second prior-task registry was added.
- Quantitative triage may use the existing extraction-plus-interpretation calls. One additional judgment is not a promise of exactly one provider invocation for every intended-use mode.
- No paid model calls, live scholarly retrieval, downloads, credential access or original-paper uploads occurred. GitHub fetch/publication is the only external repository work.


## Reproducible commands

Run from the repository root with the project's normal test dependencies. The cloud used `PYTHONPATH=/tmp/optomind-wo01-deps:.`; that already-present dependency directory is environment-specific, not required on local Astra.

```python
import socket, pytest

def deny(*args, **kwargs):
    raise AssertionError("Network disabled for WO03 local lookup checks")
socket.create_connection = deny
socket.socket.connect = deny
files = [
    "test_directed_reading_real_store_contract.py",
    "test_directed_reading_adapter_contract.py",
    "test_progressive_review_plan_material_reuse.py",
    "test_progressive_review_plan_owner_recovery_contract.py",
    "test_progressive_review_plan_batch_recovery_contract.py",
    "test_progressive_review_plan_chapter_cache_contract.py",
    "test_progressive_review_plan_stage_cache_contract.py",
    "test_progressive_review_plan_transport_retry.py",
    "test_progressive_review_plan_chapter_capacity.py",
    "test_progressive_review_plan_chapter_recovery.py",
    "test_progressive_review_plan_case_chain.py",
    "test_planning_feedback_scope.py",
    "test_planning_retrieval_query_initialization.py",
    "test_planning_supplement_attempt_contract.py",
    "test_body03_retrieval_adapter_contract.py",
    "test_body03_local_lookup_reading.py",
    "test_body03_local_lookup_contract.py",
]
raise SystemExit(pytest.main([
    "-q", *["tests/upgrade3/" + name for name in files],
    "-k", "not test_run_consumes and not test_revision_chain_runs",
]))
```

Actual message capture is built into the independent test module's `__main__` (socket denied and controlled model boundary):

```bash
PYTHONPATH=. python tests/upgrade3/test_body03_local_lookup_contract.py /tmp/fixed_messages_synthetic.json fixed
```

For the before run, extract only baseline production packages/config into a new temporary directory and run the same test module from outside the modified checkout:

```bash
mkdir -p /tmp/wo03-baseline
 git archive 91543f7c7e43515eb8e3c8cc86ee09ed54e486c5 optomind_research config | tar -x -C /tmp/wo03-baseline
cd /tmp
PYTHONPATH=/tmp/wo03-baseline python /ABSOLUTE_REPAIR_CHECKOUT/tests/upgrade3/test_body03_local_lookup_contract.py /tmp/baseline_messages_synthetic.json
```

The two direct baseline demonstrations and baseline loop capture are separate experiments. The before JSON records four boundary calls in total (two demonstrations plus two loop calls); the like-for-like loop comparison is two before versus two after, followed by zero additional calls for the second repaired owner.

```bash
python -m compileall -q optomind_research/runtime/upgrade3/planning_material_search.py optomind_research/runtime/upgrade3/planning_material_triage.py optomind_research/runtime/upgrade3/planning_retrieval_loop.py optomind_research/runtime/upgrade3/planning_supplement.py optomind_research/runtime/upgrade3/progressive_review_plan.py tests/upgrade3/test_body03_local_lookup_reading.py tests/upgrade3/test_body03_local_lookup_contract.py
git diff --check
```

## Final review

- Root compared actual owner plans and input material, reviewed every production diff, and ran the final socket-denied bounded suite.
- Independent review verified all 25 archive-manifest entries, the unchanged ordinary first request, stat-only index reopening with zero new model calls, and thirteen nominations yielding twelve bounded reads plus an explicit cap omission.
- `_planner_instructions`, `_messages_for`, query-refiner instructions, `TRIAGE_SYSTEM_PROMPT`, and `ProgressiveReviewPlanner.run` retain their baseline AST. `chapter_arrangement.py` and `review_unit_writer.py` are byte-identical. Only optional failure/omission data extends the existing tool-material projection.
- Protected branches and the archive branch are untouched. No merge or force push is authorized. Final publication SHA and verified branch URL are supplied in the completion message; this record is committed with the code it documents.
- Stop here for local Astra: **READY_FOR_ASTRA_03_LOCAL_LOOKUP**. Work orders 04–07 remain closed.

### Final measured result

Final root socket-denied selection: **238 passed, 2 deselected** (original 212 controls plus 26 new cases). Compileall and `git diff --check` pass. The final two added cases verify scalar string and mapping `required_outputs` remain single executable requirements, not individual characters/keys. Independent review ran the earlier 24-new-case version and additional direct probes; root reran all 238 after the final parser edit.
