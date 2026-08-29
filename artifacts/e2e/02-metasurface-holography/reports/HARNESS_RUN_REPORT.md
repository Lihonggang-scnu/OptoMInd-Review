# OptoMind Research Harness Run Report

- Run ID: `rhr_metasurface_holography_20260828_v1`
- Status: `awaiting_human_review`
- Current stage: `latex_publication_zh`
- Active wall time: 5662.40 seconds
- Tokens: 8,808,975 input + 1,191,420 output
- Estimated model cost: CNY 6.4258

## Stage metrics

| Stage | Status | Seconds | Input tokens | Output tokens | Cost (CNY) |
|---|---:|---:|---:|---:|---:|
| article_completion | disabled_by_publication_mainline | 0.00 | 0 | 0 | 0.0000 |
| article_structure_audit | needs_attention | 0.00 | 0 | 0 | 0.0000 |
| authoring_revision | completed | 1500.73 | 3,972,851 | 201,282 | 2.6740 |
| chapter_style_governance | completed | 81.28 | 110,973 | 11,828 | 0.0971 |
| chinese_translation | completed_with_warnings | 550.39 | 157,091 | 22,259 | 0.1029 |
| latex_publication | submission_ready | 19.73 | 0 | 0 | 0.0000 |
| latex_publication_zh | submission_ready | 20.81 | 0 | 0 | 0.0000 |
| llm_style_pipeline | completed | 497.08 | 0 | 0 | 0.0439 |
| packaging | completed | 78.05 | 0 | 0 | 0.0000 |
| phase3_argument_orchestration | completed_with_limits | 2182.06 | 894,056 | 253,747 | 0.3818 |
| phase3_argument_orchestration_invalidated_attempt_1 |  | 0.00 | 894,056 | 253,747 | 0.3818 |
| publication_mainline | awaiting_human_review | 0.00 | 0 | 0 | 0.0000 |
| publication_mainline_commander | completed | 206.06 | 1,140,775 | 16,626 | 0.7244 |
| publication_mainline_enhancement | completed | 3618.09 | 922,654 | 73,845 | 0.8115 |
| publication_mainline_handoff | completed | 0.33 | 0 | 0 | 0.0000 |
| publication_mainline_staged_completion | awaiting_human_review | 2069.92 | 468,717 | 135,652 | 0.8093 |
| quality_review_gate |  | 0.00 | 0 | 0 | 0.0000 |
| query_planner | primary_valid | 20.25 | 1,434 | 2,675 | 0.0024 |
| research_plan | disabled | 0.00 | 0 | 0 | 0.0000 |
| research_plan_publication | disabled | 0.00 | 0 | 0 | 0.0000 |
| review_lead | completed | 143.49 | 63,159 | 14,882 | 0.0245 |
| s2_literature_intelligence | partial | 238.16 | 0 | 0 | 0.0000 |
| section_coverage | partial | 559.50 | 94,940 | 104,820 | 0.1028 |
| section_coverage_feedback | partial | 0.34 | 0 | 0 | 0.0000 |
| section_coverage_portfolio | reused_for_phase3_recovery | 94.16 | 54,853 | 60,356 | 0.0593 |
| section_supplementary_closure | not_integrated | 0.00 | 0 | 0 | 0.0000 |
| topic_scoped_kb | partial | 141.13 | 0 | 0 | 0.0000 |
| visual_editor | completed | 37.06 | 27,338 | 1,540 | 0.0067 |
| visual_materialization | degraded | 590.01 | 6,078 | 38,161 | 0.2035 |

## Operational trace

- Indexed event logs: 27
- Completed model calls: 444
- Tool calls: 190 canonical / 202 results
- Tool reconciliation: 0 interrupted, 12 orphan results
- Errors: 1
- Recoveries/model switches: 0
- Detailed machine-readable log index: `<repository-root>\outputs\research_harness_e2e\rhr_metasurface_holography_20260828_v1\HARNESS_LOG_INDEX.json`

## Recorded errors

- `authoring_revision` / `ExceedMaxIters`: max_iters=24 exceeded (react_iter=24; iter_count separately counts completed model calls)
