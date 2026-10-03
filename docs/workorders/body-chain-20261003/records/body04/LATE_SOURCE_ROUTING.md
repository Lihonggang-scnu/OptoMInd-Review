# BODY04 late-source routing handoff

## Evidence and scope

Read R2 `ADVISOR_HANDOFF.md` and `ASTRA_ACCEPTANCE.md`, WO-04 and F11 in `docs/workorders/body-chain-20261003/records/00.md` before implementation. Historical F11 (589 route records / 603 identities) is a route-opportunity gap, not proof that all 14 sources disappeared. The historical original route/identity/packet files remain LOCAL_ONLY; this work did not claim access to them.

The bounded code change preserves the original source routes. After the existing coordination/owner revision and before case enrichment, `_route_late_sources` identifies newly admitted pool or resolved packet-only sources. Known chapter/use context produces a route to those chapters only. Candidate navigation is not ownership. Unknown context uses the existing source router for the increment only, with separate `late_source_routing` cache files and call IDs. Prompt text and stage ordering are unchanged.

Substantive review/tool material and a resolved original-study identity are sufficient. No own A/B, full-text download, weaker wording or importance discount is required. Identity-only sources remain visible as `late_material_unavailable`; conflicting current-pool / packet identities remain visible as `late_identity_conflict`. Neither receives assigned chapters or fabricated case content. Cases remain a model selection decision, not a paper quota.

## Actual baseline failure

Before editing production routing, ran the new deterministic wiring regression against the current R2 routing implementation. The late level2 source `P0002` carried a valid identity and this supplied summary:

> A review reports a controlled comparison with outcome Q under condition Z.

It had CH01 ownership. `test_late_usable_source_reaches_actual_case_input_and_persisted_routes` failed at access to actual captured CH01 case-group input with `KeyError: 'source_materials'`. The initial route ledger contained only P0001; late material was absent from case selection despite being present in the updated pool.

`LATE_SOURCE_ROUTING_EVIDENCE.json` contains captured actual `case_groups` payloads and persisted routing summaries for a reproducible baseline-route-gap control and the repaired wiring. The baseline control disables only the new incremental helper on the combined working tree; it is explicitly a reproduction of the pre-change route mechanism, not a fresh run of the untouched R2 commit. The original pre-edit failing test above separately established the actual baseline.

## Repaired evidence

- CH01 actual case input includes P0002's supplied review-derived summary in `source_materials`, and the new route includes that substantive content
- CH02 keeps original P0001 routing and does not receive P0002
- Persisted `stages/source_routing_summary.json` retains original route contents and adds the new route
- Known context adds no source-router call: recorded model-boundary route inputs contain P0001 only
- Resume yields zero model-boundary calls and an identical persisted route summary
- Normal no-late-source path adds no route or model call; identical input resumes without calls
- Unknown-context isolated test passes only P0002 to the existing router, preserves original batch cache files, resumes without calls, and invalidates the incremental cache when the actual summary changes
- Packet-only resolved review content routes without a pool card; candidate navigation in another chapter creates no ownership
- Same-handle current-pool / packet identity conflict stays visibly unresolved with empty assigned chapters and empty use content
- Tool narrative and original/reporting identity provenance survive the case-material allowlist intact

## Test boundary and results

The four stage-boundary wiring tests use the real `ProgressiveReviewPlanner.run` control flow and material/case-input construction with a deterministic planner callback, substituted `_tool_cycle` and substituted `_post_case_review`. They are offline integration wiring fixtures. They are **not** full BODY model runs and do **not** mock only the network/model boundary.

Five additional isolated helper tests exercise real routing/material construction and the existing router callback/cache boundary: packet-only admission/idempotence/conflict, unknown incremental material-bound cache, pool-packet identity collision, tool narrative case-input preservation, and identity-only packet fallback to the same study’s actual pool material.

Command:

`PYTHONPATH=/tmp/optomind-wo01-deps:. python -m pytest tests/upgrade3/test_body04_late_source_routes.py tests/upgrade3/test_progressive_review_plan* -q --disable-warnings`

Result: **139 passed** (9 new late-source regressions + 130 existing progressive-planner tests). No real model, paid call, scholarly retrieval, download, secret access, or full BODY generation was performed. Test counts establish engineering invariants only; scientific quality, real model relevance judgments, and real writer acceptance remain for local budgeted validation.

## Remaining boundary

The case-material function now falls back from an identity-only packet row to substantive current pool material only when stable identities match and no conflict is flagged. Existing substantive same-identity packet material retains its precedence. This prevents the confirmed thin-packet shadowing failure without introducing a global material refresh policy. Original route records are deliberately not re-routed merely because their material changed.
