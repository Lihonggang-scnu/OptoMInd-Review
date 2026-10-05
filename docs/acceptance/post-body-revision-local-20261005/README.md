# BODY revision A/B/C publication staging

**Read first: [METHOD_REVIEW_FIRST.md](METHOD_REVIEW_FIRST.md)**

This is a public-review projection of the local, fixed-three-issue experiment. It is not a new algorithm, a new outline/plan validation, a full BODY regeneration, or a scientific gold standard. Source HEAD is `abc97d5713904d8acc000f1f576c4785b4fa2ef7`; the only source diff is the generic fixed-issue protocol paragraph plus the two Windows fixture byte writes, captured in `source/local_source_patch.diff`.

## Reading order

1. `METHOD_REVIEW_FIRST.md`
2. `source/ROOT_SOURCE_SNAPSHOT.json`, `source/local_source_patch.diff`, and `source/config/`
3. `experiment/ROOT_EXPERIMENT_PLAN.md`, `experiment/records/LIVE_COMMANDS_AND_RECOVERY.md`, and `experiment/records/FINAL_EXECUTION_CHECK.json`
4. `materials/selected_issue_materials.json` and `materials/selected_text_omissions.json`
5. `runs/actual_call_accounting.json`, then the five run reports/candidates and their call records
6. `evaluation/` originals, consistent judgments, reports, and packet-constructor source
7. `MANIFEST_SHA256.json`, `omissions.json`, and `SECRET_SCAN.json`

## Experiment identity and cost

The package retains all five baseline/candidate texts, 15 actual provider-attempt records, B replay provenance, A/B/C configurations, ledger/cost records, evaluation originals, and selected structured issue material. A first attempt and one retry are retained; B has one verifier-only recovery; C did not use its stronger-model escalation. Known settled cost is 0.2785574 CNY with 0.2137224 CNY uncertain/held. B recovery reports 0.006509 CNY as incremental recovery cost; full known B cost is 0.0484344 CNY plus the held uncertainty. No model call was made during export.

The 15 staged call JSON files are byte-for-byte copies of the actual provider-attempt records, including original `messages`, parsed responses, raw response envelopes, model/status/usage/cost fields, and hashes. The four B saved-response replays are represented by `runs/B_recovery/replay_provenance.json` and the original B call files, so they are not duplicated as new attempts. The call records are generated experiment evidence; they may contain the selected supplied material embedded in actual requests. No HTTP authorization header or API key value is present.

The selected-material projection keeps issue anchors, source identities, titles/DOIs/years, and structured generated summary fields for P0289/P0585/P0085/P0388. Each retained `materials.*.text` field was verified to be an exact JSON serialization of its `content_fields`, so it is generated summary material rather than paper fulltext; provenance is recorded in `materials/selected_issue_materials.json`. Full fixed-case copies, duplicate run manifests, the 66 MB packet set, paper PDFs/fulltext, and any text that fails that generated-summary check are omitted and listed in `omissions.json`.

`SECRET_SCAN.json` reports categories and relative locations only. Its credential-path hits are local key-file path provenance in commands/records; the exporter never opened or copied credentials.
