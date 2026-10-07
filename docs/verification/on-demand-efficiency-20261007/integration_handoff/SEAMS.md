# On-demand Max integration seam audit

This is a factual handoff audit for the local Ch2 run on 2026-10-07. It records what the source and run demonstrate and leaves formal cloud integration decisions to the cloud reviewer. It does not claim that the optional engine has been promoted to the production default.

## Source identity

- Current source checkout: `lihonggang-dev`, `4604451df04b7ce9896fe28c5fd8410493fb9cb8`.
- Paid ACCESS and OWNER calls were run from `00a91359fd50ddcc26355c94c8bb067c25985f6a`; its parent implementation was `db219744`.
- The final source adds the navigation identity-group fix after the paid run (`outline_on_demand.py`, `resolve_material_requests`, lines 928-955). That fix was not paid-run exercised here.
- The parent folder already carries the comparison, plans, resume check, manifest, and local driver. This folder adds only the actual request/response exports, the trace, this seam audit, and bounded verification metadata.

## Observed call and data seam

The concrete local path was:

`selector group projection → material catalog/navigation → ACCESS messages → parsed material requests → local resolver/read trace → OWNER messages → updated plan/remap/material adoption → RESULT.json`

The source locations are:

1. **Selector group projection.** `scripts/upgrade3/outline_selection.py:803-957` projects the selected unit group and converts it to the on-demand payload. The paid run's stable group is `selection-de60904848d5c217`, with editable units `Ch2_U01` and `Ch2_U03`; related read-only units remain separately identified.
2. **Catalog and navigation.** `optomind_research/runtime/upgrade3/outline_on_demand.py:254-344` builds the whole supplied-material catalog and keeps the private record lookup local. `:485-575` projects light identity/navigation entries, priority semantic locators, and pageable search records. The catalog signature is carried into the request and stage signatures.
3. **ACCESS request.** `outline_on_demand.py:578-627` builds the material-only ACCESS prompt. A sanitized cloud-readable copy of the two actual messages, the original request hash, profile, wire estimate, and effective stream arguments is in `ACCESS_MESSAGES.json`. The exported copy is not a strict paid-request replay because local paths and restricted fields were replaced; use the original local PRECALL file for byte/semantic replay checks.
4. **ACCESS validation and resolver.** `:648-785` validates request shape and navigation matches; `:871-978` resolves requested IDs to complete records and records canonical IDs/aliases; `:981-1074` emits selection diagnostics and the compact trace. The paid response requested 18 complete `source_materials[...]` records. The 18-row identity/read trace is in `TRACE_SUMMARY.json`.
5. **OWNER input.** `:1096-1136` builds read-only neighboring roles; `:1306-1327` builds OWNER messages from the resolved trace and catalog. A sanitized cloud-readable copy of the two actual messages is in `OWNER_MESSAGES.json`, with local paths and any restricted fields masked by hash/length markers. Its original PRECALL hash is retained for local provenance; the exported copy is not a strict paid-request replay.
6. **OWNER result and identity transfer.** `:1077-1093` assembles the owner payload. The local result returned `status=updated`, `chapter_id=Ch2`, explicit remaps for `Ch2_U01` and `Ch2_U03`, zero structural errors, 367 accepted source identities, and zero unresolved adoption handles. The complete aggregated model content, parsed response, effective request, usage, finish, and stage signature are in `OWNER_RESPONSE.json`.
7. **Checkpoint/resume.** `:1353-1389` defines the checkpoint signature/path/load compatibility checks; `:1392-1484` handles complete material batches; `:1487-1730` runs ACCESS, resolves materials, estimates the concrete OWNER request, calls OWNER, and writes reusable stage records. The recorded run had one ACCESS call, one OWNER call, no continuation, no readonly reads, and the separate result resume check reports zero new calls.
8. **CLI driver seam.** `scripts/upgrade3/outline_strengthening.py:227-314` prepares the on-demand request and does not use the historical local-driver combined all-catalog OWNER gate. `:403-476` rechecks the prepared ACCESS hash and quality profiles, requires the local budget ledger/key file, constructs two clients, calls the runtime, and only then prepares an arrangement packet/input when the result is `updated` or `no_change`.

## Four evidence classes

| Class | Evidence in this run | Exact boundary |
|---|---|---|
| Formal CLI capability | `outline_strengthening.py:227-314,403-476`; `--mode on_demand` | The CLI has prepare/run wiring, profile/hash checks, budget-ledger/key-file requirements, checkpoint directory, and conditional arrangement packet construction. This run did not proceed from the local driver into arrangement or writer. |
| Local driver capability | `run_local_ch2.py` and `live_ch2/` | The local driver completed one real ACCESS and one real OWNER call with streaming, local ledger, actual usage, and resume artifacts. Its exact arguments are exported here. |
| Offline-only validation | Parent `FINAL_VERIFICATION.json`, `ACTUAL_COMPARISON.json`, and local prepare/reuse reports | The 125 bounded checks, capacity batching, checkpoint compatibility, stream/measurement checks, and paid-before-budget-pause resume behavior are offline/verification evidence. They are not a formal cloud end-to-end test. |
| Not tested downstream | No arrangement/writer/full-manuscript result in `live_ch2` | No claim is made that the updated plan was merged into the complete chapter, arranged, consumed by a writer, or merged back into the full manuscript in this run. |

## Budget, stream, and profile seam

The local paid call used `qwen3.5-plus` for ACCESS and `qwen3.8-max` for OWNER, both with thinking enabled, thinking budget 32768, answer/output budget 32768, and effective `max_completion_tokens=65536`. Both calls were streamed and completed with `finish_reason=stop`; the exact effective requests and usage are exported. ACCESS took about 49.204 seconds and OWNER about 396.344 seconds, with no retry.

The formal CLI constructs its clients through `make_strengthening_client` and passes `stream` behavior through the shared client/runtime. The local driver records the effective wire requests and observer events. A cloud promotion must compare its own profile mapping, streaming transport, timeout, and single-round budget reservation against these artifacts; this pack does not infer that they are already identical. The paid run used the explicit local budget ledger and key file; those are not included here.

The preparation path estimates an empty-read OWNER preview, while the production path resolves the actual ACCESS trace and estimates the concrete OWNER request immediately before reserving it. This distinction is visible in `outline_strengthening.py:239-281` and is part of the requested seam review. The current path does not use the historical local-driver combined all-catalog OWNER gate; whether the deployed cloud CLI and local driver are equivalent remains a cloud verification item. The parent comparison remains the authority for measured cost figures.

## Checkpoint compatibility and paid selection pause

The stage signatures bind request, payload, catalog, model, thinking budget, and output budget. The local run shows ACCESS and OWNER stage records, then a separate resume check that reuses the paid selection/read result and updated plan with zero new model calls. It does not prove cloud checkpoint storage or cross-machine path portability. Cloud testing should exercise a pause after paid ACCESS selection and before OWNER reservation, resume with the same request/payload/catalog signatures, and reject changed IDs, aliases, conditions, profiles, or arguments.

## Pending formal gaps

- No cloud run has verified the final `4604451` source after the post-paid-run navigation grouping fix.
- No cloud run has verified that the deployed CLI and the local driver use the same stream, profile, timeout, token-meter, and per-call budget behavior.
- No cloud run has exercised the paid-selection-before-budget-pause checkpoint with persisted cloud artifacts.
- No downstream arrangement, writer, or full-manuscript merge was executed from this result; the cloud must perform a bounded offline short-chain check using the existing updated plan and the formal arrangement/writer contracts.
- No formal acceptance was established for automatic all-unit BODY changes. The user proposal is to make the improved on-demand engine an explicit optional strengthening module/default engine, while the cloud decides exact formal integration and any bounded seam fixes.

These are pending verification tasks, not algorithm-change requests and not claims that the proposed promotion is complete.
