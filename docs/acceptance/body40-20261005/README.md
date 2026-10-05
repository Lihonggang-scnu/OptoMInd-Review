# BODY40 staged acceptance archive (2026-10-05)

This directory is a public inspection export of the completed BODY40 real acceptance. The final human-reviewed manuscript is under `body/body_assembly_final/`; start with **`body/body_assembly_final/REVIEW_DRAFT_HANDLES.md`** (primary); `REVIEW_DRAFT.md` is a numeric derivative with recorded unresolved references. Also read `REFERENCES.json`, and `RUN_REPORT.md`. Root quality decisions and negative evidence are under `run/acceptance_root/`.

## Identity and reproduction boundary

The tested code baseline is `fbf6f79328d533af2563678fc2d0e81364595c44`. The exact local repair snapshot is `7e293b63603157e741ec9b68f30565fcb4065f8a`; `code_delta/` contains the diff and changed-file snapshots, including the new regression test. The archive records the actual staged-driver sequence and command history; `COMMANDS.md` is a run-preparation/history document, while stage states, reports, and exported ledger rows are the final execution evidence.

The actual run was a 7-chapter / 29-unit BODY acceptance with 173 paper identities, 183 direct handles, and 21.892783 CNY actual spend plus 0.228144 CNY held. Ch4 was retained from the partial plan. Q01 and unresolved numeric citations remain explicitly documented in the final acceptance report and citation limitations. The archive does not claim exact paper-set equivalence from the nearby 176-reference comparison.

Follow the recorded chronology: provisional and Level-1 planning/routing/proposals, harmonization, Level-2 and chapter tools, chapter details, global coordination and owner revisions, then case enrichment, arrangement, writing, offline Ch2/Ch3 citation repair and final assembly. This tested revision deliberately completes global/owner review **before** appending cases, without a second BODY rewrite afterward. The method named `_post_case_review` does not describe its actual placement here; consult `RECOVERY_AND_STAGE_ORDER.md` and the locked source, not its name. The entry drivers are `run/acceptance_root/staged_driver.py`, `continuous_driver.py`, and `resume_after_handle_fix.py`; use the `CURRENT_PROCESS_*.json`, `EXECUTION_STATE*.json`, stage files, chapter arrangement inputs/outputs, and writer `UNIT_*` artifacts to inspect each boundary. No cloud reader should invoke production recovery from this export.

`planning/input585/` preserves the 585-row A/B pool and index metadata. `planning/old_run585/` is a comparison-only old successful plan/deep-reading export; it is clearly separated and must not be treated as a production input. `quality/advisor/` contains quality and comparison evidence, including the old manuscript boundary.

## Rights and secrets

Actual model prompts, returns, raw responses, writer messages, inputs, results, derived card material, identities, and citation metadata are retained where useful. Paper/fulltext binaries and unclear-rights bulk payloads are omitted; every omitted original path, size, SHA256, and reason is listed in `OMISSIONS.json`. Absolute local F-drive paths may appear in historical commands and actual messages for provenance. `local_original_path/ROOTS.json` maps local roots to archive-relative locations. Embedded raw `source_material`, `reading_packet.ordered_text`, and equivalent fulltext fields in serialized requests are replaced by field-level omission metadata with original character counts and hashes; generated `body_markdown`, A/B/deep findings, and model prose remain. Credentials, key contents, auth headers, passwords, and signed URL secrets are excluded.

`MANIFEST.json` gives relative path, purpose, source, original size/hash, staged size, and staged SHA256 for every archived file. Large unsanitized JSON artifacts are retained as exact gzip files with readable projection companions; sanitized gzip files are explicitly labeled in the manifest and retain original source size/hash metadata. The full readable final plan is `planning/FINAL_PLAN_FULL.md`; use it with `planning/DETAILED_REVIEW_PLAN.json.gz` and its summary projection. The full budget ledger is in `run/BUDGET_LEDGER_ROWS.json` and `.csv`.

## Start here

1. `FINAL_BODY_ACCEPTANCE_REPORT.md`: root quality judgement, old/new comparison, 11 material spot checks and remaining mistakes.
2. `quick_read/REVIEW_DRAFT_HANDLES.md`: the complete real BODY; same text as the final body directory.
3. `RECOVERY_AND_STAGE_ORDER.md`: exact code snapshot, actual stage order, commands, input relocation and limits on recovery.
4. `MANIFEST.json` / `OMISSIONS.json`: all retained files and excluded or sanitized material.

This branch is an archive and advisor input. No protected branch was merged or rewritten; no additional paid model call was made to build it.

`planning/pool_variants/INDEX.json` preserves all 67 intermediate pool locations as 40 exact content-deduplicated gzip snapshots, including late material additions. They are not silently equated to the initial 585-row pool; use the index to identify the actual stage input.
