# Record: WO-07 — Retrieval short chains 1 and 2

- Status: READY_FOR_REVIEW
- Baseline commit: `841e9143972c408a3b0f42f04e955efa2137c9b4`
- Package gate: parent-assigned WO07 bounded acceptance following WO06; no authority for a later live run
- Paid model calls: 0
- Live retrieval / full planning / full BODY / full test suite: not run

## Scope completed

Six new offline checks connect actual repaired boundaries rather than returning completed fake upper-level tool results. Engineering and education fixtures carry different synthetic findings and study-setting limits. Socket connections are prohibited by the test fixture.

- Empty query initializes through the existing query-refinement client boundary. Search verifies that a real `query_ready` journal record already exists. The request receives the real facet binding.
- An explicitly nominated new synthetic source goes through real supplement request loading, candidate identity, acquisition snapshot parsing, paper-card input/response parsing, fulfillment-judge message construction/normalization, immutable attempt output, pool merge and SQLite index extension.
- A one-chapter planner probe invokes real tool cycle, late-source routing, chapter material construction and case input construction. It raises a test-only `BaseException` at the first actual `case_groups` model boundary. The probe preserves the production order, including pre-case coordination, and never completes the planning run or starts a post-case review.
- A fresh adapter instance reuses the complete need across stages and owner changes. Real handle binding changes the source from old `P0001` to current `P0009`, while unrelated material occupies `P0001`. The reused attempt files remain hash-identical.
- Real packet merging, chapter view, arrangement model-message construction, arrangement parsing/validation, catalog construction, unit view and writer message construction carry the source-bound result, limitation and original question.
- Partial material retains `UNJUDGEABLE_PROVIDER_OUTAGE` in the actual case source material and in writer `chapter_tool_materials.still_missing`; query exhaustion leaves the need `partial`, not answered.

## Files and functions changed

- `tests/upgrade3/test_body07_retrieval_short_chains.py`: six new bounded integration checks, synthetic model/search/acquisition boundaries and an optional evidence collector
- `docs/workorders/body-chain-20261003/records/body07/retrieval/`: actual persisted-output capture, boundary calls, summary, commands and logs
- Production diff for these chains: none. Other WO07 work in the shared worktree is independently owned.

## Reproduced failure / repaired and unrepaired findings

No new production defect was reproduced in these six chains. This is integration evidence for already-repaired boundaries, not a claimed new repair. During fixture development, assertions were aligned with existing contracts: the writer carries this question inside chapter-tool material rather than a top-level `research_question`; the indexed partial path legitimately makes one local-triage call and one exhausted follow-up query round. These were test expectation corrections, not product fixes.

Observed repaired properties: initialized query survives to the supplement request; newly acquired scientific-content sentinels reach pool, index, late route and case input; complete work is reused across stages with current owner/identity; pending material remains explicitly partial at the writer input boundary.

Unrepaired/untested: live model fidelity, scientific validity, real-provider availability, broad/big-batch planning, missing private source payload replay, and whether a live writer actually retains supplied caveats in prose. The published WO06 evidence describes output limitations despite successful transport; this package makes no contrary claim.

## Post-change output

All JSON evidence files contain captured boundary calls, observations and `persisted_file_contents` keyed by paths relative to the synthetic run root. Captured files include request, supplement index, source unit, parsed card, journal, pool, stage records, need cache and, for writer chains, packet, arrangement input/output and actual writer messages. SQLite table row counts are included separately.

The temporary absolute root string is replaced with `RUN_ROOT`; a root prefix cut short by the production 4,000-character late-route excerpt becomes `RUN_ROOT_TRUNCATED`. No other content is transformed. This is an explicitly path-normalized evidence bundle, not a byte-exact raw-request replay. The original attempt hash comparison is asserted before normalization. Production `model_call`/`network_call` flags describe the executed provider-facing path; every such edge here is synthetic, and socket connections are denied.

### Short-chain appendix

| Evidence | Input / output and assertion | Boundary calls |
| --- | --- | --- |
| `chain1_engineering.json` | Empty query; `ENG_RESULT` and `ENG_BOUNDARY` in real card/pool, `P0002` late route and actual case input; need answered | search 1, acquisition 1, query model 1, card model 1, judge model 1 |
| `chain1_education.json` | Empty query; independent `EDU_RESULT` / `EDU_BOUNDARY` path; need answered | same counts |
| `chain1_education_partial.json` | Useful result plus unjudgeable qualifier; new source indexed, then actually read in local triage; qualifier remains in case material; need partial/query exhausted | search 1, acquisition 1, query model 2, card model 1, judge model 1, local-triage model 1 |
| `chain2_completion_reuse.json` | `level2/CH01` to `chapters/CH02`; unchanged semantic reuse key; current `P0009` citation catalog; no foreign `P0001` content; actual writer input retains finding/limit/question | search/acquisition/query/card/judge each 1 total across both stages; arrangement model 1 |
| `chain2_pending_qualifier.json` | Partial supplement with existing query; writer gets useful material, actual question and explicit unresolved qualifier | search/acquisition/query/card/judge/arrangement each 1 |
| `normal_unchanged.json` | One baseline source, no tool requests/new material/recovery/oversized batch; resumed case payload equals original and routing file bytes are unchanged | all retrieval/arrangement boundaries 0; resume calls only the deliberately uncompleted case boundary |

Engineering completion reuse key: `edae5c4aa83b82b98e89499bc447c7c1ad7cad063119f4cac73562fbfaed2329`.
Education semantic key: `94d49e5aad25410e6e34befe4ec17b4f0f8b737387ef86a41a09853eb9c6fa91`.

The complete cache records preserve both owner bindings (`level2/CH01`, `chapters/CH02`), source/material fingerprints and the current state. Dynamic need IDs also include source/index context and are captured in the JSON rather than assumed constant.

## Normal-path comparison

The same one-source input with no supplement request runs to the identical case boundary twice. On resume, all completed upstream stages use their actual disk caches; the case boundary alone is called again because the harness intentionally never completes it. Routing bytes and complete case input are unchanged, no late routes are added, and there is one route batch. This is a bounded ordinary-path check, not a full planning result.

## Checks

Commands from the repository root:

```sh
BODY07_RETRIEVAL_EVIDENCE_DIR=docs/workorders/body-chain-20261003/records/body07/retrieval PYTHONPATH=/tmp/optomind-wo01-deps:. PYTHONUTF8=1 python -m pytest -q tests/upgrade3/test_body07_retrieval_short_chains.py --disable-warnings --maxfail=1
PYTHONPATH=/tmp/optomind-wo01-deps:. PYTHONUTF8=1 python -m pytest -q tests/upgrade3/test_body07_retrieval_short_chains.py tests/upgrade3/test_planning_retrieval_query_initialization.py tests/upgrade3/test_body03_retrieval_adapter_contract.py tests/upgrade3/test_body04_writer_tool_handoff.py --disable-warnings
python -m py_compile tests/upgrade3/test_body07_retrieval_short_chains.py
git diff --check -- tests/upgrade3/test_body07_retrieval_short_chains.py docs/workorders/body-chain-20261003/records/body07/retrieval
```

- `TARGETED_TESTS.txt`: 6 passed
- `RELATED_TESTS.txt`: 70 passed, including the same six new tests; these counts must not be added together
- Compile and whitespace checks passed

## Unresolved items and risks

- Full source cards, tool bodies, original complete requests and local acceptance assets excluded by the WO06 archive remain `LOCAL_ONLY`. This package uses no private scientific source material.
- The fixture's partial/unjudgeable label is synthetic input at the model completion boundary. It proves qualifier delivery and pending-state preservation, not that a real outage was induced or diagnosed.
- Writer messages are persisted and inspected; no writer prose generation is performed by these chains. Arrangement output is synthetic model output processed by real validation.
- Early source and single-chapter scaffolding are deliberately tiny. No claim about complete chapters, all cases, long documents or publication readiness follows.

## Gate decision

Ready for root's bounded WO07 integration review. No production repair is requested for these chains. Any later finite real run needs separate authorization; no full BODY or auto-publication is implied.
