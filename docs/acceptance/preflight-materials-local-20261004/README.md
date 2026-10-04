# Local material acceptance: second-stop package

This public package records the bounded local second-stop acceptance for the material path on 2026-10-04. It contains saved evidence only; packaging made no provider call, production edit, model change, or test change.

Read in this order:

1. `ASTRA_ACCEPTANCE.md` — root conclusion, scope, costs, and the four engineering seams.
2. `ROOT_REAL_COMPATIBILITY.json`, `ROOT_CITATION_CONSUMPTION.json`, and `ROOT_STATIC_REVIEW.json` — machine-readable root evidence.
3. `offline/CONTROLS.stdout.log` and `offline/CONTROLS.stderr.log` — the 290-pass offline control run.
4. `real_checks/results/SECOND_STOP_RESULT.json` — provider-free compatibility and first-adoption result.
5. `real_checks/results/ATTACHED_PACKET.json`, `ARRANGEMENT_FROM_REAL_PACKET.json`, `ARRANGEMENT_INPUT_FROM_REAL_PACKET.json`, `UNIT_PAYLOAD.json`, `UNIT_MESSAGES.json`, and `ID_MAP.json` — the processed A/B/deep-to-writer handoff. These are derived materials, not paper snapshots or raw fulltext.
6. `live_writer_qwen37_single_v2/CH02_U3/UNIT_BODY.md`, `UNIT_RESULT.json`, `LIVE_CALL_RESULT.json`, `RAW_RESPONSE.json`, `UNIT_INPUT.json`, and `UNIT_MESSAGES.json` — the one new live writer result and its ordinary files. `real_checks/results/LIVE_WRITER_ONCE_META.json` records the call and ledger delta.
7. `historical_prior_reading/` — one retained initial derived A/B/deep reading result and card for provenance context. The interrupted moved/edited fixture copies are omitted because the old edit marker was placed after the reader-consumed body and was not a valid compatibility proof.
8. `historical_body_baseline/` — prior BODY comparison material. It is historical context, not a new result; its source code SHA was not recorded and is labeled `unknown` in `PROVENANCE.json`.
9. `drivers/` and `PROVENANCE.json` — archive-only reproduction derivatives, exclusions, redaction rules, and source mapping. The live-capable driver refuses to run unless both an explicit flag and an explicit opt-in environment variable are supplied.
10. `MANIFEST.md` — SHA-256 manifest for every package file except the manifest itself.

The second stop included one new `qwen3.7-flash` writer call: 0.0251484 CNY, bringing the shared settled total to 2.3385136 CNY. The package does not repeat that call. The remaining work is bounded engineering judgment: correct canonical citation consumption (seven distinct canonical handles in the body versus one reported production handle), make Windows unit identity safe for CLI paths and ordinary raw files, decide whether `reused_from` is excluded from the case-cache contract, and keep the temporal A/B/deep ordering result as a hypothesis rather than a real-loss finding. A full BODY run and any additional paid calls remain separately authorized work.

The actual compatibility claim is supported by `ROOT_REAL_COMPATIBILITY.json` and `SECOND_STOP_RESULT.json`: they record the isolated source-state checks for unchanged, moved, body-changed, and reference-changed material. The source-state fixtures and their full snapshots are deliberately omitted from this public package.

No source-state trees, replay snapshots, PDFs, reading-view fulltext, SQLite wallets or budget databases, credentials, signed links, or raw provider headers are included.

Public input/message copies have local paths redacted and are not byte-exact replay recordings. The successful live probe used the production runtime and CLI budget client through a driver with a safe output directory; the unmodified CLI path was not proven to work for colon-containing unit IDs. These assets are for diagnosis, not production PLAN/cache.
