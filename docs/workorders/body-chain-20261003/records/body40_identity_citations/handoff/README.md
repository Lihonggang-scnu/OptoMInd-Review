# BODY40 first-stop diagnostic handoff (offline only)

## Result

- New handoff suite: **10 passed**. Corrected fixtures against committed pre-fix modules: **9 failed**, one subprocess entry test deselected because a child interpreter would load working-tree code.
- Existing adjacent assembly/delivery regression tests: **31 passed**, one deselected. The complete run records **31 passed / 1 failed**: the existing harness-help test launches with a hard-coded `F:/OptoMind-Review-2` working directory, unavailable on this Linux executor. No production change was made to hide this environment limitation.
- The initial failing-first run preceded production edits and recorded eight failures. Its raw Ch6 case initially stopped at an archived Windows locator; the corrected baseline replay separately demonstrates the real numeric repair failure after portable fixture relocation.
- No paid/provider/network calls, BODY regeneration, prompt edits, original artifact edits, scientific corrections, or second-stop alias/material changes.

## Changes

The normal writer CLI passes returned citation diagnostics to `write_unit_output` and retains them in `UNIT_WRITING_RUN.json`. The assembly consumer preserves citation diagnostics in unit rows, exposes unresolved citation identities in pending problems and `RUN_REPORT.md`, and leaves a complete but restricted draft with `problems_resolved=false`. Successful repair provenance alone and explicitly informational/resolved diagnostics do not block.

Older results with separate unresolved-number, unknown-source, tool-identifier, or mapping-diagnostic fields are also carried into pending problems when a consolidated citation-problem list is absent. Existing writer issues and partial results are retained together with citation problems.

## Evidence

- `failing-first.txt`: initial pre-change failures
- `baseline-corrected-fixtures.txt`, `replay_baseline.py`: committed-source replay using final portable fixtures, without reverting or editing the shared working tree
- `passing.txt`: final ten-test suite
- `assembly-regression.txt`: complete adjacent regression result including the unrelated Windows-only harness failure
- `assembly-regression-supported.txt`: supported adjacent cases
- `REPLAY_EVIDENCE.json`: emitted diagnostic fields, body hashes, source hashes, and assembly state
- `replay_outputs/`: actual emitted handle drafts and run reports from bounded offline tests; these are test evidence, never production caches or delivery candidates

The archived Ch6 result carries its 11 existing unresolved numbers into assembly unchanged. The raw Ch6 CLI replay now retains `[11]` and records uncertainty instead of manufacturing `[P0011]`. The raw Ch7 replay recognizes known question ID `Q01`, retains all five bracketed occurrences, and reports the non-source identity. Assembly's existing heading cleanup remains in effect; every non-heading Ch7 paragraph is verified unchanged. Original source bytes are checked before/after.

Two controlled domains (astronomy and materials science) exercise exact-title repair provenance end-to-end. A separately launched actual CLI `__main__` with `--fake-client` verifies file-based offline export and zero declared real model calls. Other tests use `--run` with only `_real_client` replaced by a recorded-response provider: CLI bookkeeping therefore says one call, but the test boundary makes zero paid/network calls. This distinction is explicit in the evidence JSON.

## Environment and reproduction

Run from repository root:

    PYTHONPATH=/tmp/optomind-body40-bootstrap:/tmp/optomind-body40-deps:. python -m pytest -q tests/upgrade3/test_body40_citation_diagnostic_handoff.py
    PYTHONPATH=/tmp/optomind-body40-bootstrap:/tmp/optomind-body40-deps:. python docs/workorders/body-chain-20261003/records/body40_identity_citations/handoff/replay_baseline.py

The temporary dependency directory supplies pytest and the repository's lightweight dependencies. Bootstrap bypasses only the unrelated AgentScope runtime package facade; all tested runtime submodules and CLI functions are real. Tests deny socket connections. Archive replay relocates Windows locator metadata into a temporary arrangement and overlays saved `UNIT_INPUT` material; it does not retrieve omitted full text or pretend to reproduce every historical input byte.

This establishes diagnostic and text-preservation behavior, not scientific accuracy, production autonomy, or a full BODY rerun.

Publication boundary: derived manuscript copies are kept locally but excluded from this new commit. REPLAY_BODY_HASHES.json in the parent record directory gives hashes and sizes; existing archived input/raw/result paths and replay tests retain verifiability. Handoff evidence omits body_markdown values with explicit hash/length markers. No omitted text is indirectly re-uploaded.
