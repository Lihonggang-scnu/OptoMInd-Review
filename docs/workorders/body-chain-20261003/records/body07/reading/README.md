# WO07 chain 3 evidence

See [RECORD.md](RECORD.md) for exact commands, test counts, the five-line production diff explanation, and limits.

- `before.json`: failure-first results and the actual fulfilled→false-unresolved handoff
- `question.json`: same-paper new question, useful partial, owner update, fresh-instance cached resume
- `required_outputs.json`: same question with new required outputs, separate task/directory, retained partial and owner update
- `normal.json`: unchanged plan, no reading/new material/recovery/oversized batch
- `normal_fulfilled.json`: fulfilled reading clears stale local unresolved text before owner; owner returns no change
- `pytest-after.txt`: 4 focused chain checks passed
- `pytest-regression.txt`: 70 prior adapter/store/owner checks passed

The aggregates preserve normalized persisted texts and messages with original/redacted SHA-256 hashes. Temporary fixture roots are removed, so exported text is not byte-identical to the original local files. SQLite contents are read-only row snapshots, not copied database files. Everything is synthetic `LOCAL_ONLY`; no scientific or live-provider acceptance is claimed.

The owner resume test exercises a cache hit, not an actual owner outage. Historical false-unresolved caches are not rewritten; corrected material is produced when the adapter assembles it again.
