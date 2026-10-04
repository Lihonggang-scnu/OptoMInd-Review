# Record: WO-07 — Arrangement, writer and bounded delivery

- Status: READY_FOR_REVIEW (this slice only; root owns the package gate)
- Baseline: `841e914`
- Package gate: WO07 explicitly assigned by root after WO06; no later real-run authorization inferred
- Paid model calls / network requests: 0 / 0
- Full planning, BODY, publication or test-suite run: not run

## Scope completed

Five bounded integration cases connect the repaired owner/arrangement/writer boundaries to actual persisted production delivery outputs. Only the owner/model responses and network transport are controlled. Both `FeedbackLoop.arrangement_runner` and `FeedbackLoop.writer_runner` are real adapters; neither parser, validator, handoff, exporter nor assembler is mocked. Normal token estimation uses its ordinary local fallback. Socket connection attempts fail the tests immediately.

## Files and functions changed

- `tests/upgrade3/test_body07_arrangement_writer_short_chain.py`: five offline integration cases plus synthetic input/provider fixtures
- `docs/workorders/body-chain-20261003/records/body07/writer/capture.py`: bounded five-test capture and artifact consolidation
- Files in this record directory: captured evidence and verification logs
- Production diff from this worker: none

## Reproduced failure

No new production defect was confirmed in this slice. Negative controls intentionally produce unfinished arrangement or table work and verify the repaired paths keep it unresolved. Root separately owns any downstream publication gate findings.

## Post-change output and short-chain appendix

Archive files are path-to-content containers for actual persisted files, not fabricated expected results. `EVIDENCE_MANIFEST.json` indexes all 136 files across five archives. Each file has original and archived SHA256 hashes. `$TEMP` and `$REPO` replace absolute capture/repository roots throughout all UTF-8 content, including embedded JSON strings. These are derived path-normalized deterministic-fixture records, not byte-original supplier requests. Original-byte assertions ran before consolidation. Production `mode=run` is retained because the real assembler rejects simulated results; every provider response nevertheless came from an offline substitute, as the archive metadata explicitly states.

1. `COMPLETE_ARTIFACTS.json`: original owner units `UNIT_A` and `UNIT_B` merge into `MERGED_AB`. Owner brief order becomes `BRIEF_B`, `BRIEF_A2`, `BRIEF_A1`; arrangement merges the last two under a new derived task ID. Actual writer messages preserve all three original source-brief IDs, original development text, both source records, global argument, calibrated provenance and scope. Selecting the original `UNIT_A` resolves through explicit remapping. Independent provider `body_markdown` and `table_markdown` survive production export and real assembly. One loaded unit, one table, no unknown citations or pending issues. Owner/arrangement/writer calls: 1/1/1; unchanged feedback resume adds zero calls and changes zero tracked output bytes
2. `PENDING_TABLE_ARTIFACTS.json`: same chain with usable prose and only a table specification. Writer persists `complete=true` for transport, `output_consumption_status=pending_table`, and `markdown_table_missing_or_invalid`. Feedback status remains partial. Actual history delivery loads the body, produces no table, and persists `problems_resolved=false` with `writer_issues:1` and the original issue details. Assembly `status=complete` means all units loaded; it is explicitly distinct from content acceptance
3. `PENDING_NEEDS_ARRANGEMENT_ARTIFACTS.json`: selected existing `P0003` lacks placement. Real validation marks `needs_arrangement`; owner update is retained and feedback never dispatches writer. The same actual provider response crosses the plan-delivery entry through its replay edge and stops with `arrangement_validation_failed`, no writer, and no assembly. Identical feedback resume adds zero calls
4. `PENDING_CONTRACT_FAILED_ARTIFACTS.json`: wrong provider chapter identity gives `contract_failed`. Both feedback and real plan-delivery entry stop before writer; chapter mismatch is retained in pending errors. Owner update survives. Identical feedback resume adds zero calls
5. `NORMAL_ARTIFACTS.json`: two original units, no feedback/recovery, no new material, at most two original paragraph tasks per unit. Existing inline prose/table content remains byte-identical in writer results and exported handle draft on resume. First delivery consumes three replay responses; second consumes only arrangement and reuses both complete writer outputs. Original packet bytes stay identical. Source pool is unchanged

## Normal-path comparison

The normal case keeps the inline-table provider representation accepted, while the reordered/merged case consumes an independent provider table. Both use real writer and delivery functions. Scope, argument, source materials and output content remain intact. No scientific-content review is claimed (`content_review_status=not_reviewed`).

## Checks

Run from repository root. `$DEPS` denotes the task-provided dependency directory; `$REPO` denotes this checkout. Neither variable is a credential.

- `PYTHONPATH="$DEPS:." PYTHONUTF8=1 python -m pytest -q tests/upgrade3/test_body07_arrangement_writer_short_chain.py`: **5 passed**
- `PYTHONPATH="$DEPS:." PYTHONUTF8=1 python docs/workorders/body-chain-20261003/records/body07/writer/capture.py`: **5 passed**; regenerates five aggregate archives from a fresh temporary run
- `PYTHONPATH="$DEPS:." PYTHONUTF8=1 python -m pytest -q tests/upgrade3/test_body07_arrangement_writer_short_chain.py tests/upgrade3/test_body05_unit_identity_continuity.py tests/upgrade3/test_body06_writer_output_consumption.py tests/upgrade3/test_body06_feedback_arrangement_gate.py tests/upgrade3/test_review_delivery_entry.py -k 'not test_harness_parser_has_delivery_branch_and_help_is_offline'`: **128 passed, 1 deselected**
- Same bounded regression without the filter: **128 passed, 1 failed** because the existing harness help test hardcodes a Windows-only checkout. See `REGRESSION_PYTEST.txt`; this is LOCAL_ONLY
- `PYTHONPATH="$DEPS:." PYTHONUTF8=1 python run_review_harness.py --help`: unavailable here because unrelated visual modules import missing `cv2`; see `HARNESS_HELP.txt`. No installation or live run attempted
- Archive checksum and archived absolute-path scan: all 136 files verified

## Unresolved items and risks

- LOCAL_ONLY: existing Windows-checkout harness help fixture. Equivalent cloud CLI help is independently blocked by absent `cv2`; direct production delivery integration is verified
- Model quality, factual accuracy, full scientific review, paid-provider behavior and publication are outside this slice
- Pending controls remain intentionally unresolved. No additional generation or recovery was used to erase them
- Deterministic fixture response content does not imply byte-stable timestamps in generated production metadata

## Gate decision

This bounded slice is ready for root review. Preserve case order and stop after the authorized short chains; no post-case whole-chapter review or auto-publication.
