# Provenance and omissions

This public handoff is derived from the local first-stop acceptance on commit `b423646db1dbcf92831373b64563318737696fee`. The root verdict is preserved in `ASTRA_ACCEPTANCE.md`; offline controls and commands are preserved in `LOCAL_OFFLINE_ACCEPTANCE.md`; the actual bounded F1/F3 result is summarized in `LOCAL_REAL_PROBE.md` and the `real_f1/` and `real_f3/` files.

The public files are derived from the parent output records and the real probe outputs. They contain hashes, stage metadata, selected source identities, the small matching-handle inventory, an AI-generated local answer projection, and the provider-generated level1 outline response.

The package intentionally omits full B rows and pool, raw requests, raw HTTP responses and headers, `_llm_response_cache`, SQLite ledgers, local indexes, paper cards, source passages, fulltext, PDFs, complete replay trees, personal information, credentials, signed URLs, and transport tokens. The raw outline response is included because it is the requested model output; it has no transport envelope or source fulltext.

All statements about scientific content remain bounded by the root review. The package is an engineering handoff and review aid, not a publication-quality science certification.
