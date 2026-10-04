# Body preflight acceptance package — 2026-10-04

This is a bounded public review package for the CH02:U3 feedback-loop check and two later offline consumer fixes. It is evidence for code and handoff review, not a full BODY acceptance or a scientific quality certificate.

The tested materials were produced on body07-local-acceptance-20261004 at 1f171c4, as recorded in the local reports. This package is prepared on the publication branch body-preflight-local-fixes-20261004; the parent agent owns commit and push.

## What the evidence shows

- The authorized live chain made exactly three provider calls: owner revision (qwen3.5-plus), arrangement (qwen3.5-plus), and one unit writer (qwen3.7-flash). It used one existing CH02 packet and existing coordination feedback; it did not run the full planner, full BODY, or paid retrieval.
- The owner expanded CH02:U3 from three to four paragraph tasks. The arrangement carried all four tasks and seven sources. The real generated body is preserved without scientific edits.
- P0602 is review-derived content with citation identity and remains equal in eligibility to other usable material. The owner and arrangement preserve the direction microbiome depletion -> MDSC reduction/TAM repolarization -> renewed anti-PD-1 sensitivity. The writer reverses that direction in prose. This is writer-generated scientific drift; root must decide the scientific disposition.
- The citation consumer fix is offline only: 74 focused tests pass, all seven writer/assembly source handles are recognized, numeric rendering uses standard markers, unknown handles remain reported, and the body hash is unchanged.
- The feedback export fix is offline only: 15 focused tests pass. U01 keeps a multi-source answer from P0001 plus P0002, U02 keeps only its P0002 answer, and cross-unit isolation holds. The existing identity and material policy is preserved and not tightened.

## Package layout

root/ contains the combined review, next repair note, quality checkpoints, and task-scope summary. stage_audit/ contains the two stage-audit documents. cloud/ contains the two current cloud preflight reports and four earlier local acceptance reports. cloud_crosscheck/ contains the crosscheck report, probe, and bounded result files. live_chain/ contains the real generated body plus concise owner, arrangement, writer, observation, and fee records. small_fixes/ contains both offline repair reports, before/after evidence, and the current small-fix patch.

## Explicit limits and missing evidence

The complete BODY pipeline was not run. The package does not include the full source pool, full A/B/deep/supplement packets, unit input/message payloads, complete raw provider transports, local databases, PDFs, credentials, or complete user material messages. Those remain local for root review. The cloud reports and crosscheck are code-path evidence; they do not prove that the whole current source pool is contaminated or that full scientific quality has recovered.

Every included file is intended to stay below 1 MB. See MANIFEST.json for per-file hashes, sizes, provenance, and the omission list. security/SECURITY_SCAN.json records the package and five pending test-file scan locations using hit types and locations only.

## Priority reading

1. root/ASTRA_COMBINED_REVIEW.md and root/CLOUD_NEXT_REPAIR_NOTE.md.
2. cloud/BODY_PREFLIGHT_FINDINGS.md and cloud/BODY_FULL_RUN_STAGE_MAP.md, then cloud_crosscheck/CROSSCHECK.md.
3. live_chain/OWNER_TASK_INPUT_EXTRACT.json, OWNER_MODEL_RESPONSE.json, PLAN_DIFF_OWNER.json, PLAN_DIFF_ARRANGEMENT.json, PRIOR_BODY05.md and WRITTEN_BODY.md.
4. SELECTED_MATERIAL_CHECKS.json contains selected existing AI-reading findings (including review-derived P0602), not publisher full text. SYSTEM_PROMPTS.json contains actual system instructions; complete user-material requests remain local.
5. Current code already contains the two local small fixes. Do not blindly reapply small_fixes/SMALL_FIXES.patch; use it for comparison.
