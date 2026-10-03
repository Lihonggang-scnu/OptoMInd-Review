# BODY WO03 local real acceptance handoff

This is a compact sanitized archive for the local WO03 acceptance and the next cloud repair. It preserves the actual current owner payload as it was written to the run cache, a clearly labelled reconstruction of the production request messages, a sanitized projection of the provider raw response and parsed output, the prior owner comparison and coordinator feedback, and all three retrieval phases. The original provider request envelope was not recorded, so the reconstructed messages and sanitized response are not byte-for-byte originals; their source hashes and stage receipts remain available. It is evidence for a bounded repair, not a claim that the full BODY chain or scientific quality has passed.

## Reading order

1. `ASTRA_ACCEPTANCE.md` — historical acceptance conclusion, exact tested SHA, actual calls, limits, and the distinction between owner content gain and retrieval selection failure.
2. `owner/OWNER_INPUT_PAYLOAD_SANITIZED.json` — sanitized projection of the actual persisted current owner `cache_inputs` payload. It retains 11 current source-material A/B records, the separate P0004 late-material record with its deep derived input, and the three feedback rows. The packet evidence reports 12 source entries; the owner payload itself has 11 source records plus P0004 in the separate late-material field, so these counts must not be conflated.
3. `owner/OWNER_MESSAGES_RECONSTRUCTED.json` — request messages reconstructed from that persisted payload by the tested production `_messages_for` constructor. It is explicitly reconstructed/sanitized and is not the original provider envelope. `owner/OWNER_RAW_RESPONSE_SANITIZED.json` preserves a sanitized projection of the actual provider raw response with hidden reasoning, raw-paper fields, and local paths removed; `owner/OWNER_OUTPUT_PARSED.json` preserves the parsed stage output.
4. `owner/PRIOR_COMPARISON.json` — actual prior owner plan plus the prior coordinator response used for comparison; `reports/OWNER_ROOT_CONTENT_REVIEW.md` records the current content judgment.
4. `retrieval/PHASE_SUMMARY.json`, `retrieval/ACTUAL_QUERY_OUTPUTS.json`, and `retrieval/ACTUAL_TRIAGE_DECISIONS.json` — actual query outputs, local triage decisions, selected identities, statuses, call counts, and costs for all three phases. Local-triage model requests and rights-unknown paper prose are omitted with explicit markers.
5. `retrieval/LOCAL_SELECTION_FIXTURE.json` and `retrieval/reproduce_selection.py` — offline reproduction of the observed P0004 rank-7/top-6 miss. P0004 has 320 indexed segments; the global useful-passage cap was 5. `known_paper_handles` is a match flag after a hit, not a pin.
6. `code_audit/` and `reports/` — sanitized audit and historical/current reports. Their original local-only paths are replaced with aliases; the source acceptance output is untouched.
7. `REPAIR_INSTRUCTIONS.md` and `CLOUD_START_PROMPT.md` — root-owned continuation files already present in this target directory; they are preserved and included in the manifest.

## Original-path to archive map

The following large or local-only originals remain under the local acceptance source and were not copied. The archive replacement is the evidence a cloud reader should use:

| Original local artifact | Archive replacement |
| --- | --- |
| `<WO03_SOURCE_ROOT>/reports/REAL_RUN_REPORT.json` | `retrieval/PHASE_SUMMARY.json`, `retrieval/ACTUAL_QUERY_OUTPUTS.json`, `retrieval/ACTUAL_TRIAGE_DECISIONS.json`, and `evidence/PARAMETERS.json` |
| `<WO03_SOURCE_ROOT>/reports/OWNER_LIVE.json` | `owner/OWNER_CALL_RECEIPT.json`, `owner/OWNER_INPUT_PAYLOAD_SANITIZED.json`, `owner/OWNER_MESSAGES_RECONSTRUCTED.json`, `owner/OWNER_RAW_RESPONSE_SANITIZED.json`, and `owner/OWNER_OUTPUT_PARSED.json` |
| `<WO03_SOURCE_ROOT>/reports/RETRIEVAL_LIVE.json` | `retrieval/PHASE_SUMMARY.json`, `retrieval/ACTUAL_QUERY_OUTPUTS.json`, and `retrieval/ACTUAL_TRIAGE_DECISIONS.json` |
| `<WO03_SOURCE_ROOT>/runs/live/owner_reuse_current/stages/affected_chapter_revision/successful/CH02.json` | `owner/OWNER_INPUT_PAYLOAD_SANITIZED.json` and `owner/OWNER_OUTPUT_PARSED.json` |
| `<WO03_SOURCE_ROOT>/runs/live/retrieval/*` | The three retrieval evidence files under `retrieval/`; raw local-triage requests, provider payloads, fulltext, and cache trees are omitted. |
| `<WO03_SOURCE_ROOT>/run_real_acceptance.py` | Not uploaded; `evidence/COMMANDS.md` records the historical invocation and `retrieval/reproduce_selection.py` is the only runnable archive reproducer. |

The exact original SQLite/FTS index, shared budget SQLite ledger, source-paper fulltext, PDFs, and unselected run/cache files are local-only. The paid runner is not part of this cloud docs pack. Commands mentioning the local runner document provenance; they are not instructions to execute the absent runner from the cloud archive.

## Current evidence and boundaries

The actual tested production SHA is `8810c7bf38227f2987d7510937bdf9001ffd47ad`; the local test portability correction is separate and test-only. The initial failed test diagnosis was a Windows JSON/path escaping false negative, not production handoff loss. The actual owner call added concrete P0586 inosine-as-T-cell-fuel and P0004 PDAC boundary material while preserving the existing CH02 structure. The retrieval loop genuinely initialized queries, but all three bounded phases stopped without claiming fulfillment; local triage selected P0597/P0087/P0479 while P0004 was present at rank 7 outside the default six-paper window.

The local lookup report says P0004 is indexed and readable, with 320 segments, including the PDAC orthotopic anti-PD-1 sensitivity route. The archive keeps the identity, section, segment counts, and derived opinion. It does not carry SQLite, FTS, PDFs, source fulltext, cache trees, credentials, authorization headers, signed URLs, or provider payloads containing rights-unknown paper prose.

## Exact bounded validation

The acceptance used the cloud worktree at the tested SHA and the existing shared 30 CNY ledger. The offline socket-denied selection was 212 passed, 2 deselected, including 64 WO03 tests; compile checks and diff checks passed. The actual new budget was `0.1085646 CNY` across 13 calls, bringing the shared ledger to `0.5861682 CNY`; reserved and uncertain were both `0`, leaving `29.4138318 CNY`. The report records the real parameters: `local_top_papers=6`, `local_passages_per_paper=2`, `max_rounds=3`, `max_empty_rounds=2`, `max_queries_per_round=3`, `max_provider_retries=2`, `shared_deep_read_budget=5`, and `external_live_search=false` for this acceptance.

Original local-only source aliases used by this archive are listed in `MANIFEST.json`. The source task root remains the authoritative original output; only the approved pack builder and source-side `PACK_REPORT.json` were added there.
