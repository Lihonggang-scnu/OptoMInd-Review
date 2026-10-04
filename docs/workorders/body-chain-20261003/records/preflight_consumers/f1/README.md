# F1: arrangement cache and standalone writer consumer

Synthetic, offline production-path evidence on locked baseline
`6aee581023ebd23921db45e1270f1f35213030a5`. No network/provider calls, project
credentials, full BODY run, prompt edits, sequence changes, or scientific-quality
claim. The arrangement CLI's `--run` is exercised with only its provider and
ledger replaced by controlled local boundaries; its reported `model_calls: 1`
means one local stub invocation. Writer subprocesses use preview or fake mode;
invalid `--run` subprocesses are refused before their deliberately absent key
and ledger can be used.

## Result and changes

- Initial failure-first run, before implementation: 32 failed / 3 passed
- Expanded final 36-case file on an unchanged baseline snapshot: 33 failed / 3 passed
- Same final file on repaired tree: **36 passed**
- `scripts/upgrade3/chapter_arrangement.py`: preserve `source_briefs`, `portion`,
  and `issues` in the existing cache projection; preserve `issues` in legacy unwrap
- `optomind_research/runtime/upgrade3/review_unit_writer.py`: a narrow common
  `load_arrangement` guard consumes the existing validation verdict before
  material loading, client construction, or output replacement. No separate
  writer CLI patch is needed because it already uses this loader

Normal, merged and split tasks each traverse the real fresh arrangement CLI,
cache restore, current validator and standalone writer subprocess in both
legacy-text and owner-brief modes. Restored task data and issues match the fresh
export. Actual writer messages are byte-identical across fresh/cache, retain
owner brief relationships and split portions, and include the review-derived
P0002 deep material without requiring its own A/B card. Actual fake writer body,
messages and result files are also checked. Issues remain caller-facing fields
in arrangement/cache; this repair does not add them to writer prompts.

Invalid `contract_failed` and `needs_arrangement` artifacts are tested through
common/direct loading and standalone preview/fake/run/completion entry points.
The guard returns exit 2, constructs no client, calls no writer, and preserves
all existing output bytes. The final fixture seeds the actual production
`UNIT_BODY.md` / `UNIT_BODY.simulated.md` paths and records their before/after
SHA-256 values. On the unchanged baseline, invalid run/fake cases overwrite
those exact body files; repaired cases preserve them. Completion's original
body was already protected, but the invalid arrangement previously still
started completion and wrote new artifacts.

## Legacy boundary

A genuinely status-less legacy export remains readable. Existing positive
verdicts (`ok: true` or `status: arranged`) remain compatible if there is no
contrary signal. A supplied malformed, incomplete, failed or contradictory
verdict is refused, matching the feedback gate's existing behavior.

Already-lossy old merged/split caches cannot recover missing relationships or
portions from generated IDs or prose. The actual cache CLI now preserves that
failure honestly: `reused_contract_failed`, zero new provider calls, and a
blocked writer. No automatic paid retry, fabricated brief relationship, blanket
cache deletion or altered task identity is introduced. An original raw response
with the missing fields can be explicitly re-exported by the existing interface;
a lossy artifact alone is not enough.

## Reproduce

From the repository root in an environment with the project's test dependencies:

```sh
EVIDENCE_ROOT=$(mktemp -d /tmp/optomind-f1-check.XXXXXX)
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-wo01-deps:. \
BODY_PREFLIGHT_F1_EVIDENCE_ROOT="$EVIDENCE_ROOT" \
python -m pytest -q tests/upgrade3/test_body_preflight_f1_arrangement_cache_consumer.py
```

Use an empty evidence directory for each run, since these are real persisted
cache paths. Omit `BODY_PREFLIGHT_F1_EVIDENCE_ROOT` to use pytest's fresh temporary
directories. For baseline verification, copy only the final dedicated test file
into a separate checkout of the locked baseline and run the same command with
a different empty evidence directory; do not apply the production changes.

Portable related selection (13 files, 203 tests):

```sh
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-wo01-deps:. python -m pytest -q \
 tests/upgrade3/test_body04_writer_tool_handoff.py \
 tests/upgrade3/test_body05_argument_cli_handoff.py \
 tests/upgrade3/test_body05_global_argument_handoff.py \
 tests/upgrade3/test_body05_unit_identity_continuity.py \
 tests/upgrade3/test_body05_feedback_text_reuse.py \
 tests/upgrade3/test_body06_feedback_arrangement_gate.py \
 tests/upgrade3/test_body06_formatted_citations.py \
 tests/upgrade3/test_body06_writer_output_consumption.py \
 tests/upgrade3/test_body07_arrangement_writer_short_chain.py \
 tests/upgrade3/test_body07_delivery_pending_gate.py \
 tests/upgrade3/test_citation_prefix_consumption.py \
 tests/upgrade3/test_feedback_loop_chapter_tool_materials.py \
 tests/upgrade3/test_review_unit_writer_completion.py
```

The broader selected check appended `test_review_delivery_entry.py`,
`test_review_delivery_editing.py`, `test_review_delivery_figures_citations.py`,
and `test_review_delivery_integration.py`. It returned 252 passed / 12 failed
both on the repaired F1 tree and the untouched baseline, with identical failing
node IDs. Those failures require missing local draft/manifest fixtures or a
hard-coded Windows subprocess directory. A later portable check during the
concurrent F2 edit returned 202 passed / 1 failed on the legacy injected-runner
resume-count assertion; the parent and F2 owner were notified. The main repair
record owns the final combined-tree rerun. No full-suite pass is claimed here.

## Included evidence

- `FAILURE_FIRST.txt`: exact initial failing node IDs/counts before production edits
- `BASELINE_FINAL_TESTS.txt`, `AFTER_TESTS.txt`: final matching-test baseline/current results
- `CACHE_MESSAGE_COMPARISONS.json`: selected actual cache tasks/issues and writer message
  tasks/sources; exact fresh/cached message hashes; real fake-body export results
- `INVALID_WRITER_DISK_COMPARISONS.json`: baseline/current exit codes, instrumented
  client/writer counts and actual body/file hashes for each invalid mode
- `OLD_LOSSY_CACHE_RECOVERY.json`: actual old merged/split cache and standalone writer results
- `REGRESSION_RESULTS.txt`: precise selected regression outcomes and baseline failure comparison
- `SUMMARY.json`: concise provenance and verification limits

All inputs are synthetic. Machine-specific roots are replaced by
`<PROJECT_ROOT>`, `<BASELINE_ROOT>` and `<EVIDENCE_ROOT>` in this compact package.
Full messages and cache/output files are recreated by the dedicated test. The
hashes describe original on-disk bytes, not normalized excerpts in this package.
