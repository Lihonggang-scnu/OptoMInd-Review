# Legacy unit Plus run status

## Result

- Official CLI live run stopped at `Ch6:Ch6_U1` after 21 complete units and 22 physical attempts. `Ch6_U1` is blocked by `QwenTransportError:qwen_stream_read_timeout`; no retry or release was performed.
- Completed units: Ch1 4/4, Ch2 4/4, Ch3 5/5, Ch4 3/3, Ch5 5/5. Missing: Ch6 U1-U4 and Ch7 U01-U04.
- Official native assembly is `assembled/` with status `partial_check`, 21 loaded units, 8 missing, and 2 rendered tables; the three-task audit reports Ch1/U4 and Ch2/U04 rendered, Ch6/U3 missing.

## Ledger

- Independent 30 CNY ledger: 21 settled reservations, actual `1.936256` CNY; one uncertain Ch6/U1 reservation `1.63964` CNY with unknown actual charge. Read-only guard confirms the next call would be blocked by unsettled reconciliation.

## Verification

- All 22 actual message payloads (including the blocked request) are semantically equal to their free previews; no differences in system, chapter frame, sibling units, owner context, tasks, conditions, source records, or material maps.
- All 22 actual effective requests are identical: qwen3.5-plus, thinking 8192, answer 32768, total/max completion 40960, streaming, 1800-second inactivity and 3600-second overall bounds.
- Completed-output stable citation identities: 132 cited; 43 supplied but uncited; zero unknown tokens. Per-chapter and direct-task/closure counts are in `CITATION_AUDIT.json`; table rows are in `TABLE_TASK_AUDIT.json`.
- Historical Ch1-Ch5 normalized reference counts are in `HISTORICAL_REFERENCE_STATS.json`; they are scope-matched and are not quality judgments.

## Archive candidates

`ARCHIVE_CANDIDATE_MANIFEST.json` is an enumeration only: 186 files / 18,939,473 bytes proposed. It includes actual messages, requests, raw responses, result seals/bodies, the failed `ERROR.json` and partial stream, native assembly, ledgers as JSON exports, audit records, and root review records. It excludes SQLite, credentials, tokenizer data, source worktree, duplicate raw input, standalone material payloads, and duplicate successful SSE captures.
