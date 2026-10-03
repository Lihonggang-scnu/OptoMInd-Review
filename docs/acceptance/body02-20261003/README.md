# BODY 00–02 local real acceptance handoff

This is a sanitized, reviewable handoff of the local real acceptance run dated 2026-10-03. It is an evidence archive, not a claim that the whole BODY chain is proven. The tested cloud baseline is commit `5a8549200e5ee9e7be1992ca0cea19e1b7125487`; the package also carries the uncommitted local failure-status patch and its three regression tests.

## Reading order

1. `ASTRA_ACCEPTANCE.md` — conclusion, call ledger, actual content judgments, limits, and the distinction between the first superseded recovery driver and the final successful control.
2. `material_selection/INDEPENDENT_CONTENT_REVIEW.md` — independent reading of the four real reader outputs.
3. `material_selection/OWNER_DELTA_CONTENT_REVIEW.md` — independent inspection of the final owner delta, including the P0004 integration gap.
4. `reader/` — four sanitized actual reader inputs, prompts, parsed outputs, and commit records. The source paper body/fulltext and bibliography payloads are omitted; omission markers identify each transformed file.
5. `owner/` — sanitized full owner evidence. `final_owner_delta_CH02.json` retains actual A/B material inputs, existing/new deep material, complete parsed `updated_plan`, and complete parsed response; the two `whole_plan_improvement` files retain the initial parser outcome/telemetry and final parsed global-coordination response/telemetry without raw provider payloads. Original source-paper fulltext fields are omitted where present; generated owner material is retained.
6. `evidence/` — runtime configuration, offline preflight, final owner/recovery records, and a compact call/ledger summary. The 9.5 MB `REAL_RUN_REPORT.json` is intentionally indexed by outcomes rather than duplicated.
7. `code_audit/` — the local failure-status fix and exact patch.
8. `records/local_acceptance_alignment.md` — blank template for the cloud agent to fill after reading the actual contents.
9. `CLOUD_START_PROMPT.md` — controlled continuation prompt for original WO-03 only.

## What this proves and does not prove

The run used eight settled real calls, actual spend `0.4776036 CNY`, reserved `0`, uncertain `0`; repeated identical reader work added no call. It supports a local pass recommendation for BODY work orders 00–02. It does not prove the whole BODY chain, WO-03–07, full planning, full writing, or final scientific quality. Minor LLM naming/context imperfections were accepted where the evidence remained usable.

`ASTRA_ACCEPTANCE.md` is preserved as the contemporaneous acceptance record. Its statements that the local repair was “uncommitted/unpushed” describe the state when that test finished; this upload branch is the later handoff state in which root will commit the two local code changes. Use the uploaded code directly. `code_audit/LOCAL_FIX.patch` is retained for provenance and comparison; do not manually apply it again when the same change is already present in the uploaded code.

The owner files preserve the actual final plan and response so a later reader can see that P0004 deep material entered the owner input but was only partly surfaced in visible plan prose. This is a finding to inspect, not a claim that every new note was fully used.

## Provenance and transformation

Every copied or transformed artifact in this directory is labeled in `MANIFEST.json`. A `sanitized_copy` is not byte-for-byte original. Original source files under `outputs/cloud_body02_real_acceptance_20261003` were left untouched. No API keys, Authorization values, passwords, signed links, raw provider payloads, SQLite databases, PDF files, or original paper fulltext were included.

The package is intended for the new `body02-local-acceptance-20261003` archive branch. Root owns staging, commit, and push.
