# Body 03 local lookup: completed real acceptance

This is a small public evidence archive for the completed real local-lookup acceptance on 2026-10-03. It records what was tested and what the run actually established; it does not claim scientific quality passed.

The tested code identity was `2df2a2719b17b05a9ccdfc1574b0649c7bacf586`. The acceptance result was `completed_real_runs_quality_not_passed`: the engineering repair was retained, while both content scenarios still lacked the requested source-bound PDAC microbiota-to-ICI material. The ordinary initial read was unchanged, and explicit paper identity and checkpoint reuse were exercised.

Four direct `qwen3.7-flash` calls were settled at `0.01428 CNY`. The shared ledger ended at `0.6004482 CNY`, with reserved and uncertain amounts both zero. No external live-search, owner-model, or qwen3.5-plus call occurred.

The root content finding is specific. In the no-known-paper scenario, the initial request and its one focused expansion did not include P0004. In the explicit-P0004 scenario, P0004 was present in both requests and the final writer source, but the selected material was the metabolite section (ord115–116), while the relevant immunotherapy material was at ord93–94 and the needed preclinical material at ord55–56. The raw FTS evidence placed the relevant P0004 immunotherapy section at rank 1 (ord93); P0004 ranked 9 with the original question and 64 with the focused question (default candidate window 3000). These ranking facts explain the material loss; they do not supply a new scientific conclusion.

The four parsed model outputs are preserved verbatim in [`RESULTS/`](RESULTS/). The original requests are not copied because they contain paper excerpts. [`REQUEST_METADATA.json`](REQUEST_METADATA.json) is explicitly derived metadata: it retains the unchanged question, criteria, parameters, and source section/type/count/textlength, with excerpt text omitted. [`LOCAL_ARTIFACT_INDEX.json`](LOCAL_ARTIFACT_INDEX.json) records source provenance and omissions. The aggregate numbers and scenario findings are in [`REAL_RUN_SUMMARY.json`](REAL_RUN_SUMMARY.json).

The authoritative source files were `ASTRA_ACCEPTANCE.md` and `reports/REAL_RUN_REPORT.json` in the completed local acceptance run. A copy of the former is included as [`ASTRA_ACCEPTANCE.md`](ASTRA_ACCEPTANCE.md); the latter is represented by the sanitized aggregate summary so machine paths and unrelated run detail are not published.

This archive intentionally omits raw REQUEST and raw response files, fulltext or PDF material, SQLite ledgers, local caches, compiled files, scripts, credentials, signed links, and the long diagnostic prose. Those files remain available only through the local artifact index and source provenance references.

## Next cloud work

Read `ASTRA_ACCEPTANCE.md`, `REQUEST_METADATA.json` and the four `RESULTS` first. Then follow `NEXT_REPAIR.md`; `CLOUD_START_PROMPT.md` is the reusable handoff. Two bounded repairs are requested, no BODY changes or live cloud calls. The relevant local paper is P0004, stable ID `00d3d83d6571a7d9c15adbb84e0c371ce46d15a4`. The original question rank was 9 at default FTS candidate window 3000; rank 7 in older controls used window 500. Neither ranking guarantees relevance on its own.
