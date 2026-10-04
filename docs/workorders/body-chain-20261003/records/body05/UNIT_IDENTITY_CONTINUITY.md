# Record: WO-05 — Unit identity and source-brief continuity

- Status: READY_FOR_REVIEW, bounded runtime portion
- Baseline branch / commit: `body05-argument-identity-cloud-20261004` / accepted WO04 `3d244f23166c9ba2e948fd8efd8257d00dbebc51`
- Package gate: root-confirmed explicit WO05 approval after WO04 acceptance
- Paid model calls: 0; live retrieval/download: 0
- Full planning/body/test run: not run; no commit or push by this worker

## Scope completed

Carry authoritative owner unit/paragraph identities, explicit split/merge lineage, source-brief relationships and calibrated global argument/scope to existing arrangement and writer consumers. Preserve ordinary scientific prompts, source-material compaction and BODY ordering. No new planner, delivery algorithm or cache policy.

## Files and functions changed

- `optomind_research/runtime/upgrade3/chapter_arrangement.py`: `build_chapter_view`, `IdMap`, `ChapterView.to_dict`, `ChapterView.arrangement_payload`, `_restore_owner_paragraph_briefs`, `validate_arrangement`
  - Explicit `unit_id`/`id` and `paragraph_id`/`id` win over positional legacy maps; duplicates fail visibly. Legacy fallback IDs cannot take an explicit ID reserved elsewhere in the packet
  - Preserve new-unit to old-unit `unit_id_remap` with current-target validation
  - Determine paragraph ownership from actual IDs, never string prefixes
  - Derived paragraph IDs hash only an explicit source-brief relationship and split portion, never content similarity or new list position. Identical ambiguous relationships without distinct portions or explicit IDs fail duplicate validation
  - An existing owner paragraph ID may identify only that original task. Reassigning it to another brief, a merge or a split portion fails; a compact task identity contract is sent in the actual arrangement payload
  - Normal mode retains model `point`/`development`; when explicit references are supplied, it also carries full original brief details and source handles. Existing opt-in verbatim owner restoration remains
  - Preserve full `review_argument` and `shared_scope` as separate global fields, outside per-source compaction; retain argument status/provenance
- `optomind_research/runtime/upgrade3/review_unit_writer.py`: `select_unit`, `_checked_unit_tasks`, `build_unit_view`, `_view_from_packet`
  - Refuse ambiguous persisted unit/task IDs and foreign, unknown or conflicting source-brief links before model input
  - Rebuild explicit brief details from the authoritative arrangement input, keeping source relationships with their content
  - Packet fallback prefers actual plan argument; scope is never relabeled as argument. Writer frame carries separate scope, argument, status and source
- `tests/upgrade3/test_body05_unit_identity_continuity.py`: 26 focused offline cases, including persisted production messages

## Reproduced failure

- F16 baseline ID drift: explicit `UNIT_A` was exported as `CH01_U01`, then as `CH01_U02` after reorder. `A finding` moved from `CH01_U01_P01` to `CH01_U02_P02`, despite explicit `BRIEF_A1`
- Explicit paragraph-reference swap accepted the identity of one task while carrying another task's content; original-ID merge and split-portion variants were also accepted
- Normal arrangement discarded source-brief references/details. Duplicate owner and persisted output IDs were not rejected
- F12 writer fallback substituted `shared_scope.statement` for the plan argument. Source character limits could remove the distinguishing suffix of a calibrated global argument or scope exclusion
- Failure-first results: initial baseline 12 failures / 1 existing pass; subsequent identified edges 9 failures / 13 passes, then 3 failures / 22 passes, then 1 scope-clipping failure. Captures: `UNIT_IDENTITY_FAIL_FIRST.txt`, `UNIT_IDENTITY_FOLLOWUP_FAIL_FIRST.txt`, `UNIT_IDENTITY_CONTRACT_FAIL_FIRST.txt`, `SCOPE_COMPACTION_FAIL_FIRST.txt`. Temporary fixture paths are redacted; assertion content is retained

## Post-change output

`unit_identity/SUMMARY.json` links concrete before/after observations produced by `capture_unit_identity_evidence.py`. Accepted-baseline runtime modules are read from git and imported temporarily; both versions use the unchanged repository prompts. No model or network call is involved.

- Before and after contain fabricated packet, actual `ARRANGEMENT_INPUT.json`, `ARRANGEMENT_MESSAGES.json`, validated `CHAPTER_ARRANGEMENT.json`, `UNIT_INPUT.json`, `UNIT_MESSAGES.json`, simulated `UNIT_RESULT.json` and explicitly simulated body
- After reorder `UNIT_A` stays `UNIT_A`; `A finding` stays `BRIEF_A1`, and `A boundary` stays `BRIEF_A2`
- Actual writer message after reorder keeps `Independent boundary` attached to `BRIEF_A2`/`P0002`, and `A setting and limit` attached to `BRIEF_A1`/`P0001`
- Split/merge actual writer messages carry both original brief details under two distinct derived task IDs and explicit `settings` / `limits` portions. Reversing output task order leaves each ID attached to the same portion
- Owner unit split/merge focused fixture preserves `SPLIT_A -> [UNIT_A]`, `MERGED_AB -> [UNIT_A, UNIT_B]`, and the explicit paragraph IDs/content under their new units
- Global fields remain `CALIBRATED_ARGUMENT`, `{statement: SCOPE_ONLY}`, `calibrated`, `whole_plan_improvement`, separately in actual arrangement/writer payloads

These persisted simulated outputs show transport and identity, not scientific quality or cache reuse. Root's separate bounded feedback evidence verifies compatible unchanged writer text reuse. Existing full-message cache signatures are unchanged: reordering that changes position/sibling context may legitimately invalidate a cache; no weakened semantic cache was introduced.

## Normal-path comparison

Missing-ID legacy packet still produces `CH01_U01`, `CH01_U02`. Explicit IDs colliding with a legacy slot retain their authority while the missing-ID fallback is assigned a distinct legacy ID. Existing single-task self references, normal-mode model prose, tool-only source material, unit-scoped tools, completion behavior, owner recovery and BODY boundaries remain covered by targeted controls. Prompt files and editor/writer prompt-loader/message-builder functions are not changed.

Contract tightening is intentional: an old output ID cannot mean a different owner brief or a newly merged/split task. Older exports shaped that way must use a genuinely new task ID, or omit the new ID and supply explicit references/portion. Archived advisor fixtures were not edited; no archived-fixture pass is claimed.

## Checks

```bash
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q tests/upgrade3/test_body05_unit_identity_continuity.py tests/upgrade3/test_body04_writer_tool_handoff.py tests/upgrade3/test_review_unit_writer_completion.py tests/upgrade3/test_progressive_review_plan_owner_recovery_contract.py tests/upgrade3/test_progressive_review_plan_body_boundary.py
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python docs/workorders/body-chain-20261003/records/body05/capture_unit_identity_evidence.py
python -m compileall -q optomind_research/runtime/upgrade3/chapter_arrangement.py optomind_research/runtime/upgrade3/review_unit_writer.py tests/upgrade3/test_body05_unit_identity_continuity.py
git diff --check
```

Observed: 107 targeted tests passed; actual before/after evidence capture succeeded with sockets denied; syntax compilation and diff check passed. This is not a full suite or BODY run.

## Unresolved items and risks

- F16 wrong-task reuse in a historical real manuscript remains conditional, not a demonstrated scientific error. Original historical plans/messages remain `LOCAL_ONLY` per F12/F16 audit
- Missing-ID legacy inputs still cannot promise semantic identity through arbitrary reorder; explicit identities/relationships are required for that guarantee
- Source-brief and unit-remap correctness is structural. It cannot establish the scientific validity or completeness of generated text
- No writer completion/quality redesign, paid acceptance or WO06 work performed

## Gate decision

Runtime portion ready for root's combined WO05 verification. Stop at WO05; no merge/publication by this worker and no WO06 opening.
