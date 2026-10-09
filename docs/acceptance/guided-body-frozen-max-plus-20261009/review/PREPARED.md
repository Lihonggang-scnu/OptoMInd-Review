# Frozen automatic-guide Plus-only BODY trial — preparation

Status: **READY FOR ROOT PAID REVIEW; no model call made.**

- Source: `6a5ed067f7e302db569c6f8edcf7379c3ed252b5`; detached worktree clean.
- Automatic guide: `F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009\inputs\GUIDE_RECOVERED_FOR_REVIEW.json`; SHA `1a8624373328c7011a920c72affd165104d7304eb7c5e9fb1b3d4fb2efc60591`. Review archive comparator: `F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009\inputs\BASELINE_GUIDE.json`; parsed JSON equal.
- Manifest: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json` SHA `98e1ee62c4bf239fc54ed133d57c8c6c666b7231fd86c6e2065e79fcddf777a7`. Tokenizer SHA `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`. Config SHA `ec22e193dd9a985a05f7be9bb4906d3f07e362c3f88e1092881d7103ea8832bb`.
- Shared original ledger: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`; stored cap 60, S0 settled `18.3609232`, effective frozen cap `58.3609232`; pre-run settled `24.4610072`, final settled `32.4145152`, open `0`.
- Wrapper: `F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009\run_guided_body_frozen_max_plus.py` SHA `4fa77b609757f975caa4e286ef345d935522953954e3f668908b7811f8fa5192`; campaign snapshot SHA `34ade15e92449e7875d8512b077e2a30a7f1f3feffa44ff9904f707c2d18ab38`. Wrapper requires `--plus-only`, rejects `--allow-max`, and refuses missing fixed campaign snapshots.

## Free verification

- Targeted guided-body suite: 132 passed.
- Wrapper selftest: 5 passed, 0 network calls.
- Python compile and CLI help: passed.
- Preview: `F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009\PREVIEW`; run `20261009T121958Z-ff42936c1e`; model calls 0, paid dispatches 0. Ch1 payload is 1,298,885 bytes; 379,996 measured tokens; reserved-input estimate 433,788; estimated maximum 3.308016 CNY; fits 934,464. Four top fields are exact, material counts are 1309 atoms / 55 identities / 2 aliases / 3 tools / 221 navigation entries, prefix is empty, and recursive exact `outline_action` count is 0.

## Proposed paid command after root approval

See `records\PAID_COMMAND.txt`; it uses the same manifest, automatic guide, Plus config, tokenizer, frozen campaign wrapper, original ledger, existing key path, `--run --budget-ledger ... --budget-limit 60 --plus-only`, and a new `LIVE` output directory. No Max, retry, editor, reviewer, or guide regeneration is included.

Evidence: `records\PREVIEW_AUDIT.json`, `records\PROVENANCE.json`, `records\SELFTEST.log`, `records\CLI_HELP.txt`.

## Final paid run and offline audit

- The official Plus-only chain completed all seven chapters. Final run: `LIVE\runs\20261009T124431Z-4c66595e22`; final BODY: `LIVE\FULL_BODY.md`; BODY SHA256: `ae20879c53cca554cdaa0cfb75aea594c97751acb21ce14d0b6f4cd842812ae5`.
- There were 7 author calls plus 1 bounded Ch6 completion call. Known current-run usage cost was `7.953508` CNY. The shared ledger ended with 39 settled rows, `32.4145152` CNY settled, and zero reserved/uncertain rows; usage and ledger actuals match exactly.
- Ch3–Ch5 were recovered only through root-approved hash-bound metadata declarations. Ch6 was bound with `complete=false` and the single existing guide table gap, then one official completion call inserted the table; no scientific review notes entered any request.
- `records\ACTUAL_PREFIX_AUDIT.json` verifies every author request's accepted prefix byte-for-byte against the preceding final segments, exact four-field payload shape, and recursive `outline_action=0`. `records\ACTUAL_REQUEST_AUDIT.json` records each actual request, usage, effective Plus wire settings, and raw hash.
- `records\REFERENCES.json`, `records\NUMBERED_DELIVERY_BODY.md`, `records\FINAL_SOURCE_IDENTITY_MAP.json`, and `records\DELIVERY_FORMAT_AUDIT.json` are free, handle-preserving delivery artifacts. The delivery copy is byte-identical to the official `LIVE\DELIVERY_BODY.md`; 323 bracketed citation occurrences map to 108 distinct known handles with no unknown bracketed handles. The runtime's broad lexical diagnostic also sees unbracketed `P4119` inside organism name `Marseille-P4119`; this is recorded as a lexical false positive, not a citation.
- `records\FINAL_ARCHIVE_INPUT_INVENTORY.json` lists the frozen source, guide/config/tokenizer hashes, exact request/raw/result paths, recovery decisions, final BODY, and delivery artifacts. Nothing was uploaded.

- Source/guide integrity is recorded in records/SOURCE_GUIDE_INTEGRITY.json: locked HEAD 6a5ed067f7e302db569c6f8edcf7379c3ed252b, clean worktree, production entry normalized-equal to HEAD (Windows CRLF checkout only), guide bytes equal expected SHA, and parsed automatic/review guides equal.

- Citation closeout clarification: records/NUMBERED_DELIVERY_BODY.md remains the original handle-form compatibility copy, not a numbered reader. The separate numbered reader is records/NUMBERED_READER_BODY.md, generated offline by the locked delivery_citations formatter; its stable DOI/paper identity mapping and equivalence audit are NUMBERED_READER_MAPPING.json and NUMBERED_READER_AUDIT.json.
