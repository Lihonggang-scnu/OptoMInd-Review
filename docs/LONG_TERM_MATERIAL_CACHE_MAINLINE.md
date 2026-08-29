# Central long-term material cache

The Research Harness has one logical durable scientific-material store:

```text
data/long_term_material_cache/
  CURRENT.json
  snapshot-000001/
  snapshot-000002/
  ...
```

The snapshot directories are immutable versions of the same store, not
independent topic databases. `CURRENT.json` is the only production pointer.
Each snapshot contains:

- `MATERIAL_UNITS_FINAL.json`: traceable text/visual MaterialUnits, source
  identity, content hash, permission, content depth, and query annotations;
- `material_vectors.sqlite`: one local semantic vector per material unit;
- `LONG_TERM_CACHE_MERGE_REPORT.json` for incrementally published snapshots.

## Normal question path

For a normal `run_review_harness.py --question ...` invocation, do not pass
`--base-kb`. After Query Planner completes, the Harness:

1. embeds the compact question, scope axes, and keyword groups;
2. ranks the central cache locally by cosine similarity without a hard
   similarity rejection threshold;
3. restores same-paper context around the matched units;
4. writes a disposable run-local ReviewKnowledgeBase projection;
5. applies the existing topic-scope contract to that projection;
6. contacts S2/OA only for the remaining coverage gaps.

The run-local SQLite is a compatibility view for existing downstream modules.
It is not a second long-term library and is never selected manually for a new
question.

## Writeback path

After initial S2 retrieval, section coverage, portfolio coverage, and the
author-to-researcher feedback wave, the Harness exports materialized text rows
back to the existing MaterialUnit contract. Existing unit/content hashes are
reused. Only genuinely new units are embedded. The existing material-cache
merger then:

1. builds a staging snapshot;
2. rejects unit/vector conflicts and missing vectors;
3. runs SQLite `PRAGMA integrity_check`;
4. atomically publishes the next snapshot;
5. atomically advances `CURRENT.json`.

If embedding or merge fails, the run-local acquisition database and increment
remain available for recovery; the current central snapshot is unchanged.

## Overrides and deployment

`--base-kb` is now an explicit test/recovery override. It is not the normal
production source. `--no-long-term-material-cache-writeback` is diagnostic
only.

The `data/` directory is intentionally outside Git because it contains large
licensed/full-text assets. A server deployment must transfer the whole
`data/long_term_material_cache` directory alongside the code, then verify that
`CURRENT.json`, `MATERIAL_UNITS_FINAL.json`, and `material_vectors.sqlite` are
present and that the vector database passes `PRAGMA integrity_check`.
