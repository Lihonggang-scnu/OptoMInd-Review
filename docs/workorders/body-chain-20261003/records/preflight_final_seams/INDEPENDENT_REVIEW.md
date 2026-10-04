# Independent bounded seam review

Reviewed 2026-10-04 against archive HEAD 4b115e25b900c7832996b4c8213dc657109151d5 in OptoMInd-Review-preflight-final-seams. Read README, ASTRA_ACCEPTANCE, PROVENANCE, ROOT evidence, actual saved live writer messages/body/result, and evolving production diff plus new tests. No production edits, provider requests, network calls, or full BODY execution by this reviewer.

## Verdict

No remaining blocker identified in the bounded three-seam patch as inspected at 14:31 UTC. One review finding (case-insensitive collision with the reserved portable filename namespace) was raised and corrected before final inspection. Current direct reproduction confirms its correction. Acceptance is offline engineering acceptance, not native-Windows execution or scientific/full-BODY quality approval.

## Verified

- Saved actual writer input includes P0602's KPC-derived PDAC model, antibiotic depletion, anti-PD-1 sensitization, immune-cell findings and conditions. Saved body incorporates that material and has seven canonical handles; archived result reports only P0576. New replay test produces all seven in original first-use order, no unknown/unused handles, with body bytes unchanged. Unknown canonical handles remain visible to diagnostics rather than being filtered away.
- Defined full/collapsed/shortcut links, ordinary inline links/images, escaped brackets and genuine code examples remain protected by focused tests. Numeric repair remains conservative and requires explicit mapping. Existing intentional pure-reference inline-code behavior is unchanged.
- CLI preview/fake/provider-boundary-stub ordinary and completion paths all run with actual CH02:U3 archive identity. Output components and raw files satisfy explicit Windows lexical rules; raw JSON round-trips, JSON/messages retain CH02:U3, and completion task IDs remain intact. Unsafe names get bounded digest-suffixed components; safe legacy components remain unchanged. Lossy-slug and case-folded reserved-namespace alias tests pass.
- Actual case-batch consumer test loads identical saved reading bytes from two locations, observes different reused_from provenance, resumes without another case call, and retains the moved provenance in the writer packet. Changed findings, conditions, tasks, model and output-token settings still invalidate. Scientific reused_from-named child fields remain significant.
- Actual resume gate uses input_contract; material_signature is not a separate reuse gate. Thus projection change addresses the effective dependency, not merely telemetry.
- AST comparison against HEAD: planning changes only _cache_contract; writer changes only run_unit_completion, _citation_matches, citations_in and run_unit_writing; CLI changes only main's path construction. _messages_for, _planner_instructions and _case_material_rows are AST-identical. directed_reading.py is byte-identical, including _practical_reading_plan. Prompts, planning/body order, candidate eligibility and substantive A/B/deep resolution are unchanged.
- Independent tests: 83 passed (new citation/cache tests, body06 writer output, writer completion); 77 passed (portable paths, prior case-material-cache consumer, stage/chapter cache contracts). Total 160 focused test cases, not the repository-wide suite. Second run log: /tmp/preflight-final-independent-tests.log. git diff --check passed.

## Corrected finding

Initial portable_component used case-sensitive startswith('~id-'). A literal identity equal to an encoded component uppercased could alias it on Windows. Current code uses value.casefold().startswith(prefix); direct comparison and regression test now reject that alias. The 96-bit digest is a practical collision-resistant suffix, not a mathematical guarantee. Safe legacy case-only identities and global/root path length are outside this patch's guarantees.

## Limits and non-blocking existing behavior

- Linux file I/O plus explicit Windows component checks are not an NTFS/Windows integration run. Overall Windows path length and caller-selected roots remain unmodified.
- Nested-bracket link/image labels such as [a [P0602]](https://example.invalid) still count P0602. Reproduced identically by loading archive HEAD's writer, so this is an existing parser limit, not this patch's regression; do not claim complete Markdown parsing or expand this closeout to fix it.
- Public archived source-state snapshots are excluded/redacted. Prior historical compatibility proof can be read but cannot be independently fully replayed from this package alone. New case path test uses offline fixture material, correctly labeled.
- The temporal A/B row hiding a later independent deep result remains a controlled timing hypothesis; the current real packet already has deep. No new actual-loss proof and no reason to expand architecture or change eligibility/order.
- Saved scientific phrasing issues remain as documented. No new model-quality or full-review claims are supported.
