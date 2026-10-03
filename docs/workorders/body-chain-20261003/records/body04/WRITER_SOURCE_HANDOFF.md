# WO04 writer/source handoff audit and bounded repair

Base: `1b4f7b6a7c0dafb6f7dfe59f46980480f44d993f`. Scope: `chapter_arrangement.py`, `review_unit_writer.py`, seam tests and synthetic evidence. No prompt, BODY execution order, paid calls, live retrieval, full-chain generation or scientific answer changes.

## Independent evidence review

Read the R2 acceptance archive's `ADVISOR_HANDOFF.md`, `ASTRA_ACCEPTANCE.md`, all three original `RESULTS/*RESULT.json`, `REQUEST_METADATA.json`, and `REAL_RUN_SUMMARY.json`, plus WO04 and the consolidated F05/F08/F09/F11 references. R2 already improves discovery/read delivery; its documented scientific overstatements and overstrict patient-causality judgment are model-output limitations, not evidence that downstream identity/material delivery is correct. Full original model requests remain LOCAL_ONLY; metadata does not substitute for them.

F08 local-index remapping is already implemented: `planning_supplement` supplies `current_source_handles`, and triage rewrites both passages and paper contexts. This change does not redo that fix.

## Confirmed failures in actual writer messages

The socket-denied probe reads original production files directly from the base Git object, builds real packet and arrangement files, calls `build_chapter_view`, `build_source_catalog`, `build_unit_view`, and `unit_messages`, and persists the actual user message and synthetic citation bookkeeping. It then repeats against the working tree. No model is invoked.

- Mixed-source tool material carried new P0002's narrative but exported only P0001 in the source catalog and writer handles. P0002 was an unknown citation.
- A single tool-only P0002 scoped to CH01_U01 similarly reached the narrative channel without a citable source record.
- An old P0001 tool reference carrying stable paper ID `trial` was not remapped to current P0002; the current paper's identity did not protect this downstream seam.
- An unresolved different paper with colliding P0001 was folded into P0001's supplement instead of retaining its separate unresolved identity.
- Contradictory packet `source_materials` and `source_identity_map` silently coexisted.

Evidence: `WRITER_SOURCE_BEFORE.json` and `WRITER_SOURCE_AFTER.json`. Each aggregate contains the summary and actual persisted packet, arrangement catalog, and `WRITER_MESSAGE.json`, indexed by relative filename. Temporary fixture/repository locations are replaced with declared path tokens; no local machine paths are published. All scientific text and identities in these fixtures are synthetic, not corrected real-paper content. The synthetic citation string deliberately cites P0002 even in unresolved scenarios; their unknown citation is expected and is not a quality score.

## Repair

- Resolve tool source identities against the existing current catalog using stable paper ID or DOI. Preserve assigned, unclaimed tool handles with a stable identity; do not allocate handles. Remap old collisions to uniquely compatible current identities. Conflicting/unresolved references retain content, paper identity, original handle and an explicit identity status, without borrowing a current handle.
- Fail closed with `source_identity_conflict:<handle>` when a source material record contradicts the current packet identity map. Do not splice an old paper's content to a new identity.
- Count distinct stable source identities before considering handles. Preserve the complete source set and provenance in tool supplements.
- Admit substantive tool sources to the arrangement catalog even when previous owner tasks do not yet name them. Identity-only rows do not create study material. Review/tool-derived narrative with a valid study identity does not require independent A/B cards or receive a material penalty.
- Keep multi-source, tool-only, unresolved and unit-targeted tools at chapter level. In particular a single new tool-only source must not be folded away before the writer can receive its unit context. Existing unscoped, clearly single-source material may still attach to its existing source.
- Hand relevant chapter-tool identities to the writer catalog/known handles and explicit task-completion messages. Unit-scoped tools remain excluded from other units. DOI aliases in chapter tool references follow the existing DOI merge result.
- Reject embedded paper-ID/DOI conflicts in locator cards before backfilling any science, including known general-understanding/review-planning identity-bearing containers; cited-reference identities are not treated as the card identity. Keep correct existing catalog material and visibly mark the foreign card as ignored. Metadata-free legacy cards retain their existing behavior; tested on actual writer messages.
- Accept complete tool relationships carried on admitted `source_materials[].tool_materials` as well as packet-level tool channels. Preserve synthesis at chapter level; do not manufacture per-study findings.

The public `resolve_tool_materials(packet)` helper is available for upstream merge-before-folding and admission. The progressive planner is owned by the separate WO04 owner/admission change; its integration is necessary because identity fields already discarded by a previous fold cannot be reconstructed downstream.

## Verification

Command (existing installed dependencies; socket explicitly denied in the run):

`PYTHONPATH=/tmp/optomind-wo01-deps:$PWD python` calling `pytest.main(['-q', 'tests/upgrade3/test_body04_writer_tool_handoff.py', 'tests/upgrade3/test_review_unit_writer_completion.py'])` after replacing `socket.create_connection` and `socket.socket.connect` with a denying function.

Result: 26 passed (18 new handoff tests plus 8 existing completion controls). Includes actual persisted packet-to-message flow, original-study tool narrative without own A/B, identity-only negative control, resolved/unresolved collision, cross-field identity contradiction, scoped routing, row-carried adoption material, and known-citation persistence.

Reproduce evidence from repository root:

`python docs/workorders/body-chain-20261003/records/body04/capture_writer_source_handoff.py --base 1b4f7b6a7c0dafb6f7dfe59f46980480f44d993f --output docs/workorders/body-chain-20261003/records/body04/WRITER_SOURCE_BEFORE.json`

`python docs/workorders/body-chain-20261003/records/body04/capture_writer_source_handoff.py --output docs/workorders/body-chain-20261003/records/body04/WRITER_SOURCE_AFTER.json`

After: mixed/new-single/resolved-old cases export P0001 and P0002 to the actual writer and accept the synthetic P0002 citation; unresolved collision retains content and explicit unresolved identity; contradictory current map fails before writing.

## Limits and stopping point

These controls establish local identity/material handoff behavior, not scientific correctness or model task completion. They do not prove full BODY quality, model selection of every useful study, or the late-paper router's behavior. Multiple tasks may legitimately share one table; no table-count acceptance rule was introduced. Preserve unresolved material for future identity resolution; do not invent citations or demand new paid reading. Hand off the bounded integrated patch for local real-model acceptance; no protected-branch merge is claimed.
