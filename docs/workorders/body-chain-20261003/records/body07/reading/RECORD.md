# Record: WO-07 — Directed reading, owner update, and resume

- Status: READY_FOR_REVIEW
- Baseline branch / commit: WO07 checkout, `841e914`
- Package gate: root's WO07 short-chain assignment; root explicitly approved the false-unresolved handoff repair after its reproduction
- Paid model calls: 0
- Full planning/body/test run: not run

## Scope completed

- Bounded chain 3 uses production `make_retrieval_loop_runner` → `_tool_cycle` → `make_directed_reading_runner` → `run_directed_reading` and `DirectedReadingStore`; changed questions and changed required outputs produce distinct task identities and task-owned directories for the same paper
- Production owner material resolution, classification, persistence and cache comparison consume actual reader material. A fresh planner and fresh adapter/queue constructors resume persisted stages in their original order and retain the owner's updated plan without another model call
- Fulfilled tasks reuse the existing SQLite reading/directory; partial tasks retain useful content and explicit uncertainty but have no committed reading. Original fulfilled raw/result/input bytes remain unchanged
- Source, snapshot, model replies and topic are synthetic `LOCAL_ONLY`. Real archived scientific material, real network/provider behavior, tokenizer fidelity and scientific quality are not validated
- The bounded `_post_case_review` method is the existing opt-in **pre-case** owner revision branch, invoked with an empty case record. No case run or later whole-chapter review is started

## Files and functions changed

- `optomind_research/runtime/upgrade3/progressive_review_plan.py`: final writer-material assembly within `make_retrieval_loop_runner` now honors an explicitly present `need_state.still_missing`, including an empty value, before falling back to old local triage
- `tests/upgrade3/test_body07_reading_owner_short_chain.py`: four offline integration checks and optional evidence export
- `docs/workorders/body-chain-20261003/records/body07/reading/`: this record, five JSON evidence aggregates and two check logs

The production change does not alter prompts, model decisions, reading fulfillment/status logic, cache identities, or historical artifacts. Only the model boundaries are substituted: the reader's `QwenDirectClient` transport and the owner's documented callable model seam. `ProgressiveReviewPlanner`, adapter, retrieval loop, reader, SQLite store, material closure, file persistence, owner classification and resume behavior remain real. The owner seam records the actual production `_messages_for` output and asserts supplied answer content before returning an update. No upper workflow function is mocked to succeed.

## Reproduced failure

- Finding: WO07-READING-01, fulfilled material reintroduced a false unresolved question at the owner/writer handoff
- Evidence: `before.json`, artifacts `FULFILLED_TOOL_RESULT.json`, `OWNER_MODEL_INPUT.json` and `pytest.txt`
- Reader result: `fulfilled`; retrieval need: `answered`; final need's `still_missing`: empty
- Old downstream material and actual owner input nevertheless contained `still_missing: "What changed at 10 K?"`, inherited from the earlier unmet local lookup
- The pre-repair run with acceptance assertions produced **3 failures and 1 pass**; all three failures were the false unresolved handoff. This was not a model/network error

## Post-change output

Aggregate paths in this section are relative to `docs/workorders/body-chain-20261003/records/body07/reading/`. Each aggregate contains a `summary` plus a relative-path → persisted-text mapping. Original and redacted SHA-256 hashes are recorded for every artifact. The sole export normalization removes the temporary fixture-root prefix. SQLite tables are captured read-only as rows in each summary; no database file or credential is exported.

- `question.json`: baseline task `dr-task-5e29131d26c6271f1d9bb05f`; changed-question task `dr-task-2bbd49d9cc4a43252648bd59`
- `required_outputs.json`: same baseline task; changed-output task `dr-task-27a8a6c64988caf1b134e22a`
- Each changed-task case has **2 reader model calls, 1 pre-case coordinator model call, 1 owner model call**, and no additional calls on fresh resume, cross-stage fulfilled reuse, or direct retained-partial reads
- Each real store has **1 paper/core, 2 tasks, 1 committed reading**. Changed tasks stay partial and never commit as fulfilled
- The original answer (`Sample increased from 2 to 4 at 10 K`), new useful partial (`Replication used three synthetic samples`) and true unresolved limit (`Temperature comparison remains unavailable`) all occur in actual owner source inputs, actual owner messages, the updated plan and the resumed persisted result
- `run/stages/affected_chapter_revision/CH01.json` and its `successful/CH01.json` counterpart contain the successful complete owner revision and owner cache contract
- `run/OWNER_CHAIN_RESULT.json` is the persisted resumed result. The test compares its complete chapter plan to the first accepted revision
- Fulfilled material has empty downstream `still_missing`, including after another stage assembles a reused fulfilled reading. True partial material keeps its nonempty unresolved question and the specific temperature limit in its answer content

## Normal-path comparison

- `normal.json`: no reading request, no new material, no recovery, no oversized batch. The original chapter plan is unchanged; 0 reader/owner calls and 1 pre-case coordinator call, then 0 more calls on resume
- `normal_fulfilled.json`: an ordinary fulfilled read keeps its original plan through a validated owner `no_change`. The earlier local lookup is still inspectably unmet, but its question does not override the final answered need or owner input. Calls: 1 reader, 1 coordinator, 1 owner

## Checks

Exact bounded commands, from the repository root:

```sh
PYTHONPATH=/tmp/optomind-wo01-deps:. PYTHONUTF8=1 python -m pytest -q --basetemp=/tmp/body07-reading-owner-before tests/upgrade3/test_body07_reading_owner_short_chain.py
```

Pre-repair result: **3 failed, 1 passed**, preserved in `before.json`.

```sh
BODY07_READING_EVIDENCE_DIR=docs/workorders/body-chain-20261003/records/body07/reading PYTHONPATH=/tmp/optomind-wo01-deps:. PYTHONUTF8=1 python -m pytest -q --basetemp=/tmp/body07-reading-owner-after tests/upgrade3/test_body07_reading_owner_short_chain.py
```

Post-repair result: **4 passed**, `pytest-after.txt`.

```sh
PYTHONPATH=/tmp/optomind-wo01-deps:. PYTHONUTF8=1 python -m pytest -q --basetemp=/tmp/body07-reading-owner-contracts tests/upgrade3/test_directed_reading_adapter_contract.py tests/upgrade3/test_directed_reading_real_store_contract.py tests/upgrade3/test_progressive_review_plan_owner_recovery_contract.py
```

Focused prior-contract regression result: **70 passed**, `pytest-regression.txt`. These are bounded check counts, not a full-suite or scientific acceptance claim.

## Unresolved items and risks

- Existing historical cached tool artifacts can still contain the old false unresolved question. They are not rewritten or globally invalidated; the fix applies when production assembles the downstream material again
- The useful new answer deliberately remains partial. Temperature comparison remains unavailable; successful owner persistence does not imply that the underlying question is fulfilled
- Partial retrieval's generic `still_missing` text remains the requested question; the more specific temperature limit remains in the actual answer and owner plan. This repair does not redesign unresolved-text extraction
- Fresh-process resume is exercised in the existing phase order. Skipping an earlier phase without restoring its chapter tool material changes the owner input and is not claimed as compatible-cache reuse
- The fresh-instance owner check takes a validated cache hit; its failure flag is never invoked. It proves cached resume without an unnecessary call, not recovery from an actually exercised owner outage
- No live model, tokenizer asset, paid search/download, archived paper analysis, full planner/BODY, publication, or complete test suite was run

## Gate decision

- Chain 3 and its focused regression checks are ready for root/cloud review
- Root owns overall WO07 acceptance and any separately authorized finite real run
