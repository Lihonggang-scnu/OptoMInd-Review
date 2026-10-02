# Phase 1 planning repair: owner material closure

Date: 2026-10-03
Status: offline targeted validation complete; no model/provider calls

## Change

The planning-revision owner path now receives the current pool explicitly. Before owner validation, it scans only explicit `source_handle`/`source_handles` references in the current chapter plan and owner feedback. For a referenced handle absent from the chapter packet, it resolves the current pool row through the existing card and `build_local_material_payload` path, preserving the card's A/B material and any already-read deep material. Candidate navigation and the complete pool are lookup inputs only and are never treated as owner requests.

A referenced handle missing from the authoritative current pool or lacking readable card material remains unresolved and continues through the existing structural validator. Validation rules were not relaxed. The resolved packet is written back into the in-memory chapter record and the owner cache input, so downstream arrangement/writing receives the same material.

## Files

- `worktree/optomind_research/runtime/upgrade3/progressive_review_plan.py`
- `worktree/tests/upgrade3/test_progressive_review_plan_case_chain.py`

## Targeted evidence

- Explicit current-pool handle (P0574-shaped): owner payload contains the card's A/B fields and validation returns `updated` with no structural errors.
- Missing current card: owner payload contains no fabricated source; validation returns `unresolved` with `updated_unit_sources_unavailable:P0574`.
- Extra pool/candidate-navigation row: owner payload contains only the explicitly referenced handle; no whole-pool injection.
- Existing owner-before-case chain regression: still passes.

Command: `python -m pytest -q tests/upgrade3/test_progressive_review_plan_case_chain.py`
Result: `6 passed`.

No full suite, pipeline, network, paid call, commit, or push was performed. Deep-read reuse and historical packet recovery remain deferred to a separate root review step.
