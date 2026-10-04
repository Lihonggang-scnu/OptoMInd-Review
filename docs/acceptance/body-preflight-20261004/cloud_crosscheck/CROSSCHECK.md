# BODY preflight cloud finding crosscheck

Date: 2026-10-04
Production tree inspected: `<PROJECT_ROOT>\outputs\lihonggang_full_acceptance_20261001_50cny\worktree`, HEAD `1f171c4731f27f2f4d20107d37a05b805582529c`.
Inputs: `BODY_PREFLIGHT_FINDINGS.md` and `BODY_FULL_RUN_STAGE_MAP.md` (cloud report based on d3a2419).
Method: one synthetic, offline probe at `crosscheck_probe.py`; no provider, network, key, paid model, or full regression. Probe outputs are in `probe_runs/*/RESULT.json`.

## Result

The cloud report's F1–F5 code claims match the current tree. The initial local F5 probe manually constructed an export and incorrectly claimed the ordinary exporter also omitted the field; that probe was corrected to call both actual exporters. As the cloud report stated, the ordinary production CLI exporter retains chapter-level tool material, while the feedback arrangement runner loses it. A separate case-only deep-material seam is also reproduced and should be treated as an additional medium handoff issue. None of these probes establishes contamination of the current 585-source pool.

| Finding | Current code and offline evidence | Impact and smallest repair scope | Status |
|---|---|---|---|
| F1 arrangement cache projection | `scripts/upgrade3/chapter_arrangement.py::saved_payload_from` drops validator-consumed `source_briefs`, `portion`, and arrangement `issues`. The probe records fresh `arranged` then cached `contract_failed` with `briefs_unclaimed:U1:B1,B2`; `build_unit_view` still succeeds and has no source-brief detail. | Medium. Preserve all validator-consumed fields/issues in cache and make the writer/common entry reject an invalid restored arrangement. Add one fresh→cache→writer fixture for merged/split briefs and issues. | **Confirmed** |
| F2 outer tool resume cache | `ProgressiveReviewPlanner._tool_cycle` passes the same outer cache contract to `_stage`; a controlled partial result is returned again on resume with runner calls `1`, and `level1_tools` is recorded completed. Changing model/output/thinking also leaves the case task signature unchanged. | Medium. On resume, re-enter the collector for incomplete/failed results, or cache only complete results with all effective dependencies. Add model/output/thinking/prompt-contract inputs to the case cache signature (or explicitly isolate those runs). | **Confirmed** |
| F3 compact tool projection | `_compact_tool_feedback` emits only supplement/directed arrays, phase, and consumed IDs. The controlled full result contains `LOCAL_ANSWER` in `tool_materials_by_chapter`, while compact output contains neither that answer nor the field. | Medium but scoped to early outline/harmonize/finalize consumers. Preserve source-bound local answer/status/limits in the compact projection and add one local-answer→next-message assertion. | **Confirmed** |
| F4A assigned-card identity | With planning revision enabled, `_chapter_details` sends candidate title A together with card B content; the same conflict is rejected by `build_local_material_payload`. The probe does not show a current-pool collision. | Medium. Apply the existing identity check before initial detail projection and prevent an explicit conflict from reaching the model. Keep review-derived forwarded content with usable identity eligible; this is not a reason to require every paper's own A/B. | **Confirmed, bounded counterexample** |
| F4B prior-reading identity/version | `load_prior_readings` accepts same paper ID/title despite a DOI conflict. The actual reader runner returns `reused_prior_deep_read` before snapshot lookup for a changed source-version marker; no reader/snapshot call is made. | Medium. Compare DOI/identity and source-version/snapshot before reuse; when unavailable, mark unverified or re-read. Do not infer that existing prior files are polluted. | **Confirmed, bounded counterexample** |
| Case-only newly adopted deep | `_case_selection_material_rows` sees the candidate's independent `read_materials`, but `_attach_case_groups` fallback calls `build_local_material_payload(candidate)` without that deep material. The probe returns material available at selection, then empty `supporting_studies` and `source_materials` at attachment. | Medium additional seam. Carry the selected deep material into attach (or retain the exact selected material row) and test a candidate absent from packet/card/B but adopted by case. Do not add an A/B requirement. | **Confirmed** |
| F5 feedback tool export | The real `FeedbackLoop.arrangement_runner` with a stub client returns an arrangement whose view has the cross-source tool answer, but its feedback export/writer payload has no `chapter_tool_materials`. The same view through `scripts/upgrade3/chapter_arrangement.py::_export` does include the field and the writer retains `MULTI_SOURCE_TOOL_ANSWER`. | Small, conditional. Add `compact_chapter_tool_materials(view)` to the feedback export or route both paths through a shared exporter; add one cross-source feedback-writer fixture. If the feedback branch is disabled for the run, record that explicitly. | **Confirmed; ordinary-export contrast corrected** |

## Reproduction evidence

The probe is `crosscheck_probe.py`; it uses temporary synthetic packets under `probe_runs/` and a stub client only for the feedback adapter. The relevant result fields are:

```text
F1 cached_status=contract_failed; cached_errors=[briefs_unclaimed:U1:B1,B2]; writer_view_succeeded=true
F2 first_status=partial; second_status=partial; runner_calls=1
F3 full_has_local_answer=true; compact_has_local_answer=false
F4 chapter_details_model_saw_card_B=true; prior_reuse_accepted_without_snapshot_check=true
CASE_ONLY_DEEP case_selection_material_available=true; case_only_deep_dropped_at_attach=true
F5 feedback_export_has_chapter_tool_materials=false; ordinary_export_contrast_confirmed=true
```

The probe exercises the actual functions and persistence boundaries, but it is not a real retrieval/API outage, real SQLite index update, model generation, or full BODY run. It therefore supports code-path and handoff findings only. In particular, it does not prove that the current source pool contains any F4 collision or that all case additions lose deep material.

## Recommendation to the owner

Treat F1–F4 and the case-only deep seam as medium cross-stage fixes/compatibility checks to resolve in the cloud patch/review before authorizing the full paid run. F5 is a narrow local fix if the optional feedback branch remains enabled. The existing review-derived policy remains valid: usable forwarded content plus identity may participate in writing without requiring the original study's own A/B or downgrading it.
