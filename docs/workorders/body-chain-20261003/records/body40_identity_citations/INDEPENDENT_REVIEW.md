# BODY40 first-stop boundary review

Reviewed working tree against e0615b0 on 2026-10-05, after reading START_AND_SCOPE.md and prior BODY40_INDEPENDENT_EVALUATION.md. This was a read-only production review; no production edits, model calls, research download, or historical manuscript changes.

## Outcome

No remaining concrete blocker in reviewed first-stop seams after the identity worker's handle-only correction. This is an engineering boundary finding, not a scientific quality or full pipeline acceptance claim.

## Finding closed during review

Initial binding retained an identity-less historical P0593 whenever current pool values also contained P0593. A bounded two-step counterexample called the real binder and merge_tool_materials_into_packets: historical useful content was attached to an unrelated current paper and inherited its identity. The binder was corrected to require paper identity or source-unit provenance, preserving unsupported labels in unresolved_source_handle.

I reran the same two-step counterexample against the correction. Current unrelated source remained untouched; useful historical content stayed chapter-level with unresolved_identity and its old label retained diagnostically.

## Boundaries checked

- Stable IDs retained before registration and remapped after normal registration; material text preserved
- Generated numeric suffix aliases do not establish model reference identity
- Explicit caller mappings and unique full-title evidence retained; conflicting/unknown duplicate definitions and scientific-symbol differences remain unresolved
- Tool identifiers come from typed input metadata; generic bracket text, code and links are not classified by prefix alone
- Normal writer, completion, CLI persistence and assembly carry citation uncertainty while allowing useful restricted drafts
- Successful repair/provenance rows are not treated as unresolved failures
- Production diff limited to local identity selection/binding, citation consumption/diagnostics and CLI/assembly propagation; no prompt, stage-order, owner recovery or case-selection edits observed
- Canonical/alias material consumption remains deferred to second stop

## Independent tests

Final combined rerun: 280 passed in 1.48s. Command used python -m pytest with PYTHONPATH=/tmp/optomind-body40-bootstrap:/tmp/optomind-body40-deps:. and these paths:

- tests/upgrade3/test_body40*
- test_body03_local_lookup_r2.py
- test_body03_local_lookup_r2_boundaries.py
- test_body03_local_lookup_reading.py
- test_body06_writer_output_consumption.py
- test_body03_local_lookup_contract.py
- test_body03_retrieval_adapter_contract.py
- test_body06_feedback_arrangement_gate.py
- test_body06_formatted_citations.py
- test_body07_arrangement_writer_short_chain.py
- test_chapter_arrangement_identity_fallback.py
- test_feedback_loop_chapter_tool_materials.py
- test_review_unit_writer_completion.py
- test_review_unit_writer_portable_paths.py

All bare filenames above are under tests/upgrade3/. Bootstrap only bypasses the unrelated eager AgentScope package facade, as supplied by task environment. Review is bounded to working-tree changes and adjacent regression controls; it does not establish unseen sources, exact original fulltext prompts, or scientific accuracy.
