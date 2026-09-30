# Manuscript parts planning implementation plan

Baseline: `review-v2`, `1d107289928fffdd99e640ade557dc106a1cc4f8`.
Feature branch: `review-v2-manuscript-parts`. No paid calls or historical-plan migration.

## Architecture decisions

The existing `planning_revision_enabled=True` path is the new production-development path. The legacy disabled path remains a historical-compatible route, not a second new planner. New output/state versions distinguish the new contract; old run directories cannot be resumed into it.

The card uses the research's exact six semantic fields (shared context and five fields per part). It lives at top level in partial/final plans and RUN_STATE, with v0/v1/v2 checkpoints; BODY packets receive only a read-only boundary projection. No units, cases, paragraph tasks, second routing pass, or domain schema are introduced.

Actual order: provisional_scope → level1 tools → level1_outline → source_routing → chapter_proposals → harmonize_scope → level2 tools → finalize_chapter_scope → optional chapter needs/tools → chapter_details → case_groups → existing post-case whole-plan review / affected owner revisions → final assembly. The legacy path's whole-plan review is before cases; the new path already has `_post_case_review`, so final calibration belongs there without another model call.

## Function-level changes

1. Add `schemas/upgrade3/manuscript_parts_plan.schema.json` and dependency-free `runtime/upgrade3/manuscript_parts.py` validator/projection. Preserve schema semantics; reject unknown fields and malformed values, not publication titles.
2. `_planner_instructions` / `_messages_for` and `run`: explicitly enable new prompt contract for provisional and level1 calls; require v0/v1 alongside BODY outlines. Light responsibilities are not BODY tasks. Deep teaching/analysis survives any heading.
3. BODY-stage payloads and prompts: pass compact boundary projection; remove obsolete introduction-as-mini-review instruction in new mode; protect source assignments, case/paragraph depth and existing tools. Do not let chapter outputs replace the card.
4. `_post_case_review`: receives current full card, real chapter/case/tool scope. Existing whole-plan response requires `no_change` or `updated` with complete replacement, validated locally. Freeze planning v2 after normal owner coordination; no added model stage.
5. `_stage`, chapter cache checks, `run`, `_partial_result`, `_assemble_final`, `_write_final_outputs`, renderers: version-check resume, include input/task/boundary signatures, preserve global argument, scope and card through stop/resume/final/packet handoff. No fabricated default card.
6. Delivery: explicit new-plan context from current final plan or supplied compact snapshot; retain global understanding, source identities and available material references/excerpts. Carry it through delivery manifest/assembly summary into front/back generation. No second full-pool routing.
7. `build_stage_messages` / `run_front_back_stage`: retain actual BODY and serial conclusion→intro→abstract; add per-part contract and shared global/material context. Use final BODY to calibrate early claims. Remove fixed single-paragraph abstract requirement in the new-contract route.
8. `apply_front_back`: new-contract route uses owned generated-part markers and never replaces substantive BODY by title. v1 supports standalone execution only. Embedded/distributed remain valid plans with explicit unsupported execution state; block downstream final publication rather than silently append parts. Historical fixture route remains separate.

## Preserved assets

No rewrite of planner; no BODY source-routing algorithm replacement, case grouping algorithm replacement, extra planner class or paid call. Preserve arrangement/writer owner briefs, conditions, synthesis, source identities, A/B/deep/local material handling. Do not edit advisor copies, outputs baselines or main.

## Offline verification and review

- Schema positive/negative cases, including prohibited units/cases/paragraph_briefs and all placement modes
- Injected planner fixtures exercise v0/v1/v2, source routing, chapter details/cases, final packets, no_change/replacement, partial level1/level2, resume/no rollback, old-state rejection and cache invalidation
- Tutorial BODY with Introduction title retains mathematical teaching, multiple briefs, sources and conditions; Outlook BODY remains substantive while closing can be embedded
- Front/back captures contract/global inputs, full BODY and prior generated parts; owned-marker application preserves substantive sections; unsupported placement cannot publish
- Run available existing tests; classify absent historical assets separately, never report those as passing
- Read actual fixture content and compare source/task preservation; a replay proves software invariants, not improved model prose. Real content quality awaits separately authorized small global-plan and parts-only calls

## Commit sequence

1. Schema/validator plus planner early contract and BODY/final calibration
2. Persistence/resume/invariant tests and any review corrections (may combine with first commit for atomicity)
3. Front/back and delivery consumption plus offline tests
4. Final review/documentation as necessary; push feature branch, verify remote SHA. No merge.

## Self-review

One card, one existing planner, one source pool and existing global call. The only new component is local contract validation, not a planning agent. Semantic anchors are not string-replacement addresses. Legacy fixtures can test downstream but never become new production planning inputs.
