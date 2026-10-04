# Citation consumption final seam (offline)

## Result

The frozen archive implementation reproduces `x[参考单元论证][P0602] -> []`.
The saved real body contains seven distinct canonical handles, while its
original persisted report records only `P0576`. Replaying the same body through
`write_unit_output` now records, in first-appearance order:

`P0564, P0576, P0602, P0561, P0388, P0190, P0582`.

Unused and unknown sets are empty for this archived unit. The saved Markdown
bytes and the report's body string remain unchanged. The original archive is
not edited. `[参考单元论证]` remains an output-quality issue: it is not silently
removed, normalized, or accepted as scientific support. This does not repair
scientific wording or demonstrate full-BODY quality.

## Scope and downstream inspection

The bounded change is in `review_unit_writer._citation_matches` and its
`citations_in` caller. Canonical paper brackets bypass the fallback adjacency
heuristic only during handle consumption; actual defined Markdown links are
still masked by `_prose_segments`. The explicit numeric-repair path retains
its conservative behavior. Image reference labels are not counted as paper
citations, including unresolved image syntax. Unknown paper handles are still
reported rather than filtered out against unit sources.

`_output_diagnostics` feeds ordinary runtime/written reports and completion
fragment diagnostics. The writer CLI copies the written used/unused fields.
The downstream `full_review_draft.scan_citations` scans the body independently,
not the undercounted report. Its existing numbering code already consumes the
archived seven handles correctly, preserving adjacent canonical brackets and
the explanatory labels. The regression verifies that behavior; assembler
logic and report schemas were not changed. No prompt, BODY ordering, reading,
model invocation, or retrieval changes were made.

## Evidence

- Source archive: `docs/acceptance/preflight-materials-local-20261004/live_writer_qwen37_single_v2/CH02_U3/`
- `before/UNIT_RESULT.json`, `after/UNIT_RESULT.json`: actual persisted
  `write_unit_output` reports for an explicitly labeled `archive-replay`, not
  new model outputs. Inputs/materials are reconstructed from saved input and
  messages; no redacted local paths are dereferenced.
- `before/SUMMARY.json`, `after/SUMMARY.json`: archive artifact SHA-256s,
  minimal reproduction, body byte/string equality, recomputed bookkeeping,
  and downstream assembly/numbering checks
- `BEFORE_TESTS.log`: final targeted suite against frozen baseline source,
  **7 failed, 14 passed** (includes the canonical image-label regression)
- `GUARDED_TESTS.log`: same final suite against repaired source with socket
  connections denied, **21 passed**
- `AFTER_TESTS.log`: targeted plus existing citation-prefix, formatted-citation,
  and writer-output-consumption tests, **95 passed**
- `BEFORE_REPLAY.log`, `AFTER_REPLAY.log`: provider-free replay summaries

Body files generated during replay are checked byte-for-byte, then omitted
from this package to avoid duplicating the archive. Reports retain their
ordinary replay body paths; consult the archived `UNIT_BODY.md` and recorded
hash instead. Inputs/messages are likewise referenced, not duplicated.
Original real output is the provenance source; these reports do not claim a
new paid run. Calls to providers/retrieval: **zero**.

## Reproduce from repository root

```sh
export PYTHONPATH=/tmp/optomind-stage2-deps:.
E=docs/workorders/body-chain-20261003/records/preflight_final_seams/citations
python "$E/replay_archive.py" --baseline
python "$E/replay_archive.py"
python "$E/replay_archive.py" --baseline --test  # expected failing baseline
python "$E/replay_archive.py" --test
python -m pytest -q tests/upgrade3/test_preflight_final_citations.py tests/upgrade3/test_citation_prefix_consumption.py tests/upgrade3/test_body06_formatted_citations.py tests/upgrade3/test_body06_writer_output_consumption.py
```

The baseline source is loaded from archive commit
`4b115e25b900c7832996b4c8213dc657109151d5` without editing the worktree. The
historical BODY source SHA remains unknown as recorded by the archive; no
current source SHA is substituted for it. Public messages are redacted
scientific-input copies, not byte-exact request replay data.
