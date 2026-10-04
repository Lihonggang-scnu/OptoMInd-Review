# LOCAL_REAL_PROBE

## Result

The bounded actual-data probe for `b423646` completed under the authorized `outputs` root.

- **F1 cache export -> resume -> writer payload:** passed with zero model calls. The resumed arrangement validated as `ok=true`, `status=arranged`; all 4 CH02:U3 paragraph tasks and source briefs were carried over, and the rebuilt writer messages are byte-identical to the existing live writer messages.
- **F3 formal adaptive local judge -> production level1 outline:** completed with two logical stages and three provider calls. The formal collector used the matching B pool and P0004 local material with `allow_external=false`; the production `ProgressiveReviewPlanner._model_stage("level1_outline", ...)` then produced a structured outline. The local result is partial (`answers_requested_question=false`) because the available PDAC patient-level prospective evidence remains missing. The outline carries the source-bound mouse-model route and the patient-level boundary into `unresolved_limits`; this is a pipeline acceptance result, not a scientific quality pass.

## Changed

Only new probe outputs and this record were written. No production source was edited.

- Probe root: `F:\OptoMind-Review-2\outputs\body_preflight_consumer_acceptance_20261004\runs\real_probe`
- Script: `runs/real_probe/probe.py`
- Preflight inputs, hashes, entrypoints, model settings, and token estimate: `runs/real_probe/PRE_FLIGHT.json`
- F1 result and resumed artifacts: `runs/real_probe/f1_cache_resume/RESULT.json` and `runs/real_probe/f1_cache_resume/resumed_export/`
- F3 raw collector, compact feedback, level1 payload, and outline record: `runs/real_probe/f3_formal/FORMAL_COLLECTOR_RESULT.json`, `COMPACT_TOOL_FEEDBACK.json`, `LEVEL1_OUTLINE_PAYLOAD.json`, and `LEVEL1_OUTLINE_RECORD.json`
- F3 accounting: `runs/real_probe/f3_formal/RESULT.json`

The probe was first run in its temporary worktree output directory while the live call was active, then copied with per-file SHA-256 verification to the authorized parent `outputs/.../runs/real_probe` root. JSON and text path metadata were updated to the final root, and the temporary `worktree/runs/real_probe` directory was removed. The worktree `runs` directory is empty.

## Verify

- Matching input: 12 B rows (`P0001` through `P0012`), including explicit P0004 `00d3d83d6571a7d9c15adbb84e0c371ce46d15a4`.
- Model settings: outline `qwen3.5-plus`, thinking budget `8192`, output limit `18000`; local judge `qwen3.7-flash`, thinking budget `4096`, output limit `6000`.
- Preflight estimate: `0.8361168 CNY` for the observed three-provider-call shape; hard local ceiling `1.0 CNY`.
- Shared ledger before: `2.2761514 CNY`, 39 settled, 0 reserved, 0 uncertain.
- Shared ledger after: `2.3133652 CNY`, 42 settled, 0 reserved, 0 uncertain.
- Probe ledger delta: `+0.0372138 CNY` from `0.0054632 + 0.0067842` local adaptive calls and `0.0249664` level1 outline call. The ledger cap remains `30.0 CNY` and was not reset.
- F3 call accounting is checked against both the new settled ledger call IDs and the collector's `local_triage.model_calls`: 2 local adaptive calls plus 1 level1 outline call.
- F1 `portion` is recorded as `null` with `not_applicable_no_portion_in_fresh_or_resumed_tasks`; the actual arrangement contains no portion field to compare.

## Blockers

The formal local judge stopped with usable but partial source-bound material. It retained the PDAC microbiota-depletion/anti-PD-1 mouse-model route and the patient-microbiota-transfer-to-mice condition, while preserving that PDAC patient-level prospective or completed randomized evidence was unavailable in the local material. The outline also preserves this boundary and records unresolved clinical evidence limits. No external retrieval, forced answer, hand-edited science, or full BODY run was performed.
