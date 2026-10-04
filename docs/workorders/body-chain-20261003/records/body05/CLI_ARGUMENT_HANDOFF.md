# Record: WO-05 — CLI global-argument handoff

- Status: READY_FOR_REVIEW (CLI subpackage)
- Baseline branch / commit: `body05-argument-identity-cloud-20261004` / `3d244f23166c9ba2e948fd8efd8257d00dbebc51`
- Package gate: root-assigned WO-05 after the user's approval following WO-04; no WO-06 authorization
- Paid model calls: 0
- Full planning/body/test run: not run

## Scope completed

- Forward the final-plan argument to arrangement, with packet-argument fallback when the plan lacks it
- Keep review scope, chapter purpose and global argument separate; missing argument stays empty and is explicitly marked `missing`
- Preserve argument source/status metadata through the runtime contract
- Remove feedback CLI purpose overrides so preflight and the concrete arrangement adapter consume the packet's global argument
- Verify that argument differences beyond the source-compaction limit alter the actual CLI cache signature

## Files and functions changed

- `scripts/upgrade3/chapter_arrangement.py::main`: reads `plan.review_argument`, forwards its provenance and separate `shared_scope`; the runtime supplies packet fallback
- `scripts/upgrade3/review_feedback_loop.py::FeedbackLoop.arrangement_runner` and `_preflight`: stop passing chapter purpose as argument
- `tests/upgrade3/test_body05_argument_cli_handoff.py`: 14 focused parameterized cases covering ordinary/revision previews, fallback, absence, differing arguments, real feedback messages and persisted views
- Coordinated runtime dependencies, implemented in the arrangement/writer subpackage: `ChapterView` metadata and shared-scope handoff; `arrangement_payload` keeps the whole argument while retaining source-material compaction

## Reproduced failure

- Finding: F12, global argument replaced by review scope or chapter purpose
- Fixture/evidence: `tests/upgrade3/test_body05_argument_cli_handoff.py`; aggregated actual field excerpts in `docs/workorders/body-chain-20261003/records/body05/CLI_ARGUMENT_HANDOFF_EVIDENCE.json`
- Before modification, all 13 initial cases failed in 6.61s. The real offline arrangement CLI persisted `review_argument="SCOPE_ONLY"` despite `plan.review_argument="FINAL_CALIBRATED_ARGUMENT"`. It also used scope when only a packet argument existed or both argument fields were absent
- Both real feedback preflight messages and the real adapter's persisted view/messages contained `review_argument="CHAPTER_PURPOSE_ONLY"`, including the missing-argument fixture
- A subsequently added long-argument case failed after the basic handoff repair: two 610-character arguments with a common prefix and distinct `ARGUMENT_A` / `ARGUMENT_B` suffixes produced the same actual CLI signature, `793c9ef953bdccd7`. That intermediate run had 1 failed / 13 passed in 8.55s

## Post-change output

- Final-plan fixture: persisted `review_argument="FINAL_CALIBRATED_ARGUMENT"`; stale packet argument no longer wins
- Packet-only fixture: persisted `review_argument="PACKET_CALIBRATED_ARGUMENT"`
- Missing fixture: persisted empty `review_argument`, `review_argument_status="missing"`, `review_argument_source="missing"`
- Every fixture preserves separate `shared_scope={"statement":"SCOPE_ONLY","exclusions":["outside this review"]}` and `purpose="CHAPTER_PURPOSE_ONLY"`
- Feedback messages and `ARRANGEMENT_INPUT.json` carry `CALIBRATED_A` / `CALIBRATED_B` or explicit missing argument as supplied. The preflight run records `status="preflight_only"`, `network_calls=0`
- Final payload includes the runtime's compact `task_identity_contract`; all after-field excerpts and signatures were refreshed after this change, with baseline snapshots retained
- Long-argument signatures now differ: `cf1a30a9a69e1ac2` versus `fc6691e1d3254179`; both previews report zero model calls

## Normal-path comparison

- Both default and `--planning-revision` previews still complete with `mode="preview"`, `model_calls=0`
- Chapter purpose remains present and unchanged; it is no longer relabeled as the global argument
- The controlled arrangement provider returns placement using the actual input unit/brief IDs. Real response parsing and arrangement validation still return `validation.ok=true`

## Checks

`BODY_REPAIR_TEST_DEPS` identifies the provisioned test dependencies. `BODY05_CLI_EVIDENCE_ROOT` identifies a fresh temporary fixture-output directory. Their machine-specific values are intentionally not part of this portable record.

```sh
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q tests/upgrade3/test_body05_argument_cli_handoff.py --basetemp="$BODY05_CLI_EVIDENCE_ROOT"
python -m py_compile scripts/upgrade3/chapter_arrangement.py scripts/upgrade3/review_feedback_loop.py tests/upgrade3/test_body05_argument_cli_handoff.py
git diff --check
```

- Final evidence-producing run after the identity/runtime freeze: **14 passed in 9.01s**
- Prior complete post-change runs: 14 passed in 7.89s and 14 passed in 8.26s
- Compilation and whitespace checks: exit 0
- Tests invoke the actual arrangement CLI as a subprocess. Feedback preflight uses actual CLI `main` with only a deterministic local token counter. The feedback adapter uses real view/message construction, `run_arrangement`, parsing, validation and persistence, with a **controlled provider response, not a recording**

## Unresolved items and risks

- Synthetic `calibrated` provenance labels verify transmission; they do not establish the quality of a scientific argument
- No credentials, paid model call, live retrieval, full planning/body execution or full test suite was used
- Historical original F12 real-run plans and messages remain `LOCAL_ONLY`, as described in `docs/workorders/body-chain-20261003/references/BODY_DESIGN_CONTINUITY_AUDIT (2).md`
- This subpackage does not independently validate the planner's scientific calibration, full-chain writer behavior or incremental text reuse; those belong to the other bounded WO-05 records

## Gate decision

- CLI subpackage is ready for WO-05 review; WO-06 remains closed
