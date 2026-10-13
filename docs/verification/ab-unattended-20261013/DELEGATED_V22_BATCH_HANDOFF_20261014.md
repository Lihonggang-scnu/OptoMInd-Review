# V22 directed-reading batch handoff (metadata only)

- Observed UTC: `2026-10-13T21:12:38.263097+00:00`; audit update UTC: `2026-10-13T21:22:49.544370Z`
- Active source: `76f46ee4d4a337b8b3fafd19e6723c4266b82c42`; binding SHA: `2b276e93e7de40422679cf868860e2c784a833c680d80a5b651f4ffe3b282d60`
- Batch: **10** complete transport calls; root reviewed **2**, delegated reviewed **8**; project actual settled **0.2097090 CNY**, account actual **0.2097090 CNY**; ledger totals equal.
- `486.002 s` is the sum of ten provider elapsed values. Filesystem-observed parallel transport span is **71.271777 s** (`2026-10-13T20:57:26.317262Z` → `2026-10-13T20:58:37.589039Z`); review tail after last physical response is **227.068191 s** to the latest decision.
- No raw response, full source text, credentials, source/gate/ledger mutation, or paid retry is copied or started.

## Calls

| handle | reviewer | elapsed s | actual CNY | level2 output | native store | chapters |
|---|---|---:|---:|---|---|---|
| P0022 | delegated_luna | 70.829000 | 0.0112932 | partial / material_ready=True / fulfilled=False | pending | CH2,CH3 |
| P0003 | delegated_luna | 41.562000 | 0.0336006 | material_ready / material_ready=True / fulfilled=True | committed | CH1 |
| P0139 | delegated_luna | 48.359000 | 0.0075318 | material_ready / material_ready=True / fulfilled=True | committed | CH2,CH3 |
| P0090 | delegated_luna | 59.344000 | 0.0104032 | material_ready / material_ready=True / fulfilled=True | committed | CH1,CH2,CH3 |
| P0001 | root | 29.344000 | 0.0497646 | material_ready / material_ready=True / fulfilled=True | committed | CH3,CH5 |
| P0187 | delegated_supplement_fallback_audit | 51.219000 | 0.0679818 | material_ready / material_ready=True / fulfilled=True | committed | CH3,CH5 |
| P0069 | root | 41.688000 | 0.0063114 | partial / material_ready=True / fulfilled=False | pending | CH2,CH5 |
| P0081 | delegated_supplement_fallback_audit | 70.235000 | 0.0108682 | material_ready / material_ready=True / fulfilled=True | committed | CH3,CH4 |
| P0245 | delegated_supplement_fallback_audit | 41.344000 | 0.0052774 | material_ready / material_ready=True / fulfilled=True | committed | CH4,CH5 |
| P0148 | delegated_supplement_fallback_audit | 32.078000 | 0.0066768 | material_ready / material_ready=True / fulfilled=True | committed | CH2,CH3 |

## Verified handoff paths

- Transport request fields: `supervision/calls/<gate>/REQUEST.json:messages[1].content` (`paper_title`, `questions`, `required_outputs`, `source_material`, `structured_source_material`, `optional_bibliography`).
- Level2 input fields: `level2/.../<paper_id>/<task_id>/INPUT.json` (`paper_identity`, `task`, `source_hash`).
- Level2 material fields: `level2/.../<paper_id>/<task_id>/DIRECTED_READING.json` (`paper_identity`, `question_material`, `material_ready`).
- Native ownership mapping: `directed_reading.sqlite:directed_papers.metadata_json` (`chapter_ids`, `section`, `request_origin`), matching `ROOT_V22_DIRECTED_REQUEST_ACCEPTANCE.json.expected_native_chapter_owners` for all ten.
- Active source consumer: `binding.source_worktree`; the V19 `binding.source_root` is legacy redundant metadata for this transport. `planning_transport.py`, both active drivers, and loaded-module validation use the worktree field.

## Hash semantics

- Output `INPUT.json.source_hash` / `DIRECTED_READING.json.source_hash` = `sha256_value(material)` (`source_digest`). Registry `directed_tasks.source_hash` and committed `directed_readings.source_hash` = `task_source_hash`, which adds paper identity, runtime config, and prompt digest. All ten task hashes match; all ten output/registry source-hash differences are therefore expected role differences.

## Material delivery and blockers

- P0022 and P0069 each retain `DIRECTED_READING.json` + `COMMIT.json` with `material_ready=True`, `status=partial`, `fulfilled=False`; native task status is `pending` and no `directed_readings` row is expected until a fulfilled commit.
- Code path retains partial material in runner `materials`, `_read_materials`, `_result_paper_materials`, `case_read_materials`, and chapter `all_materials`; `owner_content` is a separate supplement ownership projection.
- Current `RUN_STATE.current_stage=level2_tools` has no `stage_inputs.level2_tools` key. The observed five-entry `DIRECTED_MATERIAL_CACHE.json` contains **0 exact V22 task IDs**, so next-stage consumption of all ten is still unverified.
- P0003 theory/formula explanations are useful; `examples=[]` is accepted source-shaped output, not a format defect or rework trigger.

Full metadata and per-call hashes: `F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\preparation\ab_unattended_20261013\DELEGATED_V22_BATCH_HANDOFF_20261014.json` (SHA-256 `d5f89e3ace82bc07c631bf3d82fa896476f452af8a7f07d9f1a9dc53af9f7080`).
