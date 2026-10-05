# BODY40 native first-stop acceptance archive: independent read-only crosscheck

Verified 2026-10-05 UTC against Git objects, without checkout, merge, rebase, production edits, test execution, native driver execution, model calls, network calls, or regeneration. No manuscript or payload text is reproduced here.

## Identity and scope

- Archive commit: `f60dc3f3f9cf86eb6eb638b17ab7549877f9f838`
- Direct parent/source: `e75c66c1ffd018a93636960caa7c51e74911c2af`
- Every change is an addition under `docs/acceptance/body40-native-20261005/`: 55 files, 995,238 bytes. No existing production, test, or historical archive file changes.
- Independently recomputed sizes and SHA-256 of all 54 manifest entries: no mismatch.

## Direct artifact comparisons

- Compared source archive `docs/acceptance/body40-20261005/body/body_assembly_final/REVIEW_DRAFT_HANDLES.md` with native `all29_reassembly/output/REVIEW_DRAFT_HANDLES.md`, using only CRLF/CR to LF normalization.
- Character lengths: 84,173 to 84,170. Complete string equality holds after replacing the sole `[P0011]` with `[11]`. Independent character diff finds only deletion of `P00` at old character offsets 44348:44351. No other prose, tables, headings, or marker changes.
- Old file byte SHA-256: `eb53530675171b1abc4da5cd7b55c5ed6a1f32831e1a72db96a0244c13c37433`
- New file byte SHA-256: `dd7cf5f8cda3a55c187b54b367a9790d3a9bc985c1f84e21ee39703f638a9bac`
- Parsed old/new REFERENCES.json objects are exactly equal. There are 173 entries, 173 unique paper_id values, and 173 unique canonical handles.
- Old/new batch selections both contain 29 jobs with identical unit order. Only Ch6_U4 and Ch7_U02 differ, exclusively in reused_result paths. No other field changes.
- Native assembly summary directly contains 29 unit rows, 173 used papers, 222 catalog papers, status=complete, problems_resolved=false, and 5 pending problems.
- Both original-local replay results' body hashes independently match the report's result_body_sha256 and raw_body_sha256 fields. Ch6_U4 has zero [P0011], one [11], 13 citation problems, and unresolved numeric citations [1] through [12]. Ch7_U02 has five [Q01] markers and one citation problem.
- Both original-local reported raw-response, UNIT_INPUT, and original UNIT_MESSAGES hashes independently match corresponding files in the prior published archive at e75c66c. This corroborates saved source bytes, not access to the original Windows filesystem.
- All five production-file hashes in generated boundary evidence independently match Git blobs at e75c66c byte-for-byte.

## Native-run evidence inspected, not independently rerun

- Native control stdout records 537 passed, 2 failed. Isolated baseline stdout records the same two failing nodes: archived-body/downstream-numbering and moved-reading/science-settings cache test. Failure excerpts show LF/CRLF output-byte mismatch and unescaped Windows-path substring assertion against JSON-escaped content respectively.
- Baseline metadata identifies e0615b0f83ed001042b12c539a08a316f3ef6ab6 and a two-node isolated baseline command. This is evidence of the same two baseline failures, not evidence of a complete 537-test baseline run.
- Dedicated stdout records 17 historical-identity controls passed and 28 numeric/handoff controls passed.
- Native platform, execution timing, clean Windows worktree, restored boundary file, and zero live/paid/network calls are attested by archived logs/reports. This check did not observe the original execution.
- Replay script inspection supports bounded saved-response use: recording provider substitutes for the production CLI provider, token counter is disabled, socket connect/create_connection are denied during writer execution, then production assembly is called. No script was executed here.

## Snapshot and reconstruction boundary

- README, selection note, root review, and script consistently distinguish public-archive replay from original-local replay.
- For original-local replay, script selects final assembly CHAPTER_ARRANGEMENT snapshots and arrangement-repaired ARRANGEMENT_INPUT metadata. The earlier brief_reference_unknown failure and correction are documented. The published archive itself makes no production changes; the snapshot correction is driver input selection. The actual failed prior run and Windows snapshots are not independently accessible in this check.
- Both original-local cases explicitly record original_vs_generated_messages_byte_equal=false; original and generated hashes differ. Generated message files are omitted, so their recomputed hashes are not independently available here. This is historical saved-response consumer replay, not byte-original request replay or new scientific generation.
- Provenance caveat: generated boundary evidence says tested_source_sha=7e293b63603157e741ec9b68f30565fcb4065f8a while acceptance says e75c66c. The same older label is present in the source tracked boundary record; all five relevant production hashes match e75c66c. Treat the label as stale metadata, not independently proof of which commit the Windows interpreter executed.
- SAFETY_SCAN.json records zero heuristic credential/signed-URL matches. Manifest integrity was verified; this is not a comprehensive independent secret audit.

## Conclusion

Published bytes corroborate the first-stop manuscript, source-identity, batch-selection, and diagnostic preservation claims. Native test/runtime claims remain archived execution evidence. Nothing here validates second-stop alias-material consumption, full scientific quality, or fresh full-BODY generation. Existing unresolved issues remain explicit.
