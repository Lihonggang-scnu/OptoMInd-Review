# Independent downstream A/B/C revision safety review

Review date: 2026-10-05 UTC
Repository: OptoMInd-Review-post-body-revision-abc
Scope: new revision contracts, engine, CLI/config/prompts and blinded evaluator. No production files edited by reviewer. No network, provider, full BODY or upstream pipeline execution. All Python execution used the specified offline bootstrap that denies socket connections.

## Outcome

No remaining release-blocking defect observed in the reviewed snapshot after coordinated fixes. Ready for controlled local testing, not a claim of scientific quality, provider success, or publication readiness. Final read-only re-review of the frozen files completed at 2026-10-05 08:42 UTC. Independently reran all 83 new revision tests: 83 passed in 1.78 seconds. The parent separately reports 557 legacy tests passing; that legacy count was not independently rerun in this review.

## Concrete defects found and resolved

1. Partial transport response acceptance: explicit complete=false was not inspected. Engine now rejects it, as well as nonterminal finish reasons. Verified independently.
2. False scientific resolution: malformed/unclassified reviewer issues remained pending while model_assessed_issues_resolved was true. Engine now rejects unknown kinds and any pending issue prevents resolution. Verified independently.
3. Unknown proposal evidence: engine now rejects proposal evidence IDs outside selected actual materials. The low-level patch binding helper also validates proposal evidence IDs, rejects duplicates and nonselected IDs, and restricts the effective evidence to the declared subset.
4. Cost completeness after interrupted call: durable call record originally omitted dispatched intent until after returning. Crash after a cost-known review could incorrectly report complete total excluding an uncertain author call. Engine now persists dispatch intent before external call. Independent simulated-interruption probe now reports cost_cny=null, known_cost_cny=0.1, cost_complete=false, without repeating the interrupted call.
5. Live cost reporting: provider token counts did not become reportable call costs, making all live comparisons ineligible. CLI/engine now carry calculated CNY and provenance separately, with missing usage retaining unknown cost. This is configured-price calculation, not a verified invoice.
6. CLI failed-generation exit code: failed run used to return zero. CLI now reports nonzero for failed generation.
7. Recording envelope fidelity: adding offline zero cost initially double-wrapped transport responses and could hide partial metadata. CLI now preserves mapping metadata while adding explicit offline cost provenance.
8. Explicit source identity preparation: preparation previously hid direct material_id/material_ids links inside original_identity, making supported citations unusable. CLI now validates and preserves usable links and rejects unknown/conflicting mappings.
9. Evaluator attribution: report variant and candidate hash were initially trusted without matching the run manifest. Evaluator now checks manifest self-hash, report fingerprint, manifest variant and case, evidence/case fingerprints, candidate SHA, baseline bytes and independently replays patches.

## Independently exercised boundaries

- Exact CRLF preservation outside patched text; immutable baseline and deterministic byte reconstruction
- Stale input, evidence and scope invalidate resume
- Interrupted external-call slot does not automatically repeat on resume
- Concurrent same-output runs are excluded by lock
- Explicit partial/empty-choice transport envelopes rejected
- Malformed reviewer issue cannot create a resolved report
- Distinct actual stage paths: A combined+verifier (2 calls), B review+author+verifier (3), C initial path then one stronger author and stronger verifier (5 on rejection)
- Scientific numeric correction allowed when selected material exists, rather than editorial numeric guard blocking scientific repair
- Existing contract tests cover duplicate anchors, overlapping patches, stale hashes, unknown citations, exact source IDs, preservation spans, insert/remove behavior and fenced/Unicode/CRLF snapshots
- CLI tests cover preview without constructing provider/ledger, paid double gates, finite shared budget requirement, explicit model settings, no retries, shared-ledger accounting, recordings, resume and preparation
- Evaluator tests cover source/input binding, tampering, swapped-order disagreement, uncertainty, missing judgments, offline exclusion and unknown-cost exclusion

Independent probes retained:
- /tmp/revision_boundary_probe.py
- /tmp/revision_paths_probe.py
- /tmp/revision_crash_cost_probe.py
- /tmp/revision_safety_probe.py

## Important remaining limitations

- Semantic factuality, preservation and whether a reviewer concern is real remain model judgments. Deterministic checks cannot prove entailment or detect every negation/causal/knowledge-loss change.
- A scientific/missing issue with no selected actual evidence stays pending; it is not routed into evidence-free acceptance or dismissal. This is intentionally conservative.
- Blinding is procedural: the fresh local evaluator must not read the separately stored decode key or generation reports.
- No real provider credentials, model execution, external pricing verification, real scientific quality assessment or statistical superiority test was performed.
- Model-assisted results and calculated costs must retain their provenance; fixture success is plumbing validation only.

## Final frozen-snapshot re-review

- All 83 new tests pass with the socket-denying offline bootstrap
- Read-only inspection confirms bounded targets, prioritized issue processing and preflight checks; dispatch intent and known billing persisted before parsing; stable selected source identities sent to author/verifier; low-level proposal evidence validation; typed scientific judgment evidence requirements; severe-regression/knowledge-loss eligibility exclusions; safe evaluation report output
- Preparation preserves the review argument, scope and fine-grained outline rather than discarding needed intent; source variants and complementary identity metadata remain explicit
- No additional release blocker found, and no production files were changed by this reviewer
- Signoff remains limited to readiness for controlled local testing, without live-provider, invoice or scientific-quality validation

## Final root additions after independent review

Final bounded context tests add explicit related-block handoff to author/verifier and prevent applying mutually dependent edits against stale counterpart context. Shared unchanged counterpart text remains permitted. Root reran the full new suite after these changes. No new provider calls or upstream mutation.
