# Portable writer path repair (offline)

## Finding and bounded change

The archived real input/result identify chapter `CH02`, unit `CH02:U3`. The actual
entrypoints are `scripts/upgrade3/review_unit_writer.py` and
`optomind_research/runtime/upgrade3/review_unit_writer.py`; advisor snapshots are
not runtime sources. Before repair, normal CLI directory `CH02_CH02:U3`, completion
directory `CH02_CH02:U3_completion`, and both raw filename formulas contain `:`.
Windows component validation rejects these; the historical acceptance records
native Windows directory failure and an NTFS ADS raw response. This probe runs
on Linux and does **not** repeat or claim native Windows/NTFS execution.

Only four disk-component joins and one helper import per consumer change.
`portable_component` preserves safe legacy components. Unsafe names use a readable
slug (at most 24 characters) plus a 96-bit SHA-256 suffix (maximum 53 characters).
The encoded namespace is reserved case-insensitively, including a regression for
an uppercased literal encoded name. Lossy replacements, traversal/separators,
ADS colons, Windows devices, controls, trailing dots/spaces and long components
are covered. JSON IDs, provider call IDs and task contents remain original.

This is component safety, not an arbitrary whole-path/MAX_PATH fix. Caller-chosen
roots still determine total path length; deeply nested evidence roots exceeded
260 characters before the capture was moved to a short temporary root. The final
inventory records actual full/relative path lengths. Existing safe legacy IDs
that differ only in case retain their pre-existing Windows alias behavior.
The digest offers collision resistance, not a mathematical injectivity claim.

## Evidence and reproduction

From repository root:

    PYTHONPATH=/tmp/optomind-stage2-deps:. python -m pytest tests/upgrade3/test_review_unit_writer_portable_paths.py tests/upgrade3/test_review_unit_writer_completion.py -q
    PYTHONPATH=/tmp/optomind-stage2-deps:. python docs/workorders/body-chain-20261003/records/preflight_final_seams/paths/run_offline_path_probe.py

`TESTS.stdout.log`: **52 passed** (44 path tests, 8 prior completion controls).
All six normal/completion × preview/fake/run CLI combinations are tested.
`PATH_PROBE_RESULT.json` contains before formulas, actual argv/report, physical
file inventories, SHA-256 digests, path lengths and completed assertions.
`normal.stdout.log` and `completion.stdout.log` are actual CLI output; stderr is
empty. Actual files are retained in the temporary directory named in the
inventory, rather than duplicating the archive's large messages/body in Git.
The reproducible driver regenerates these files.

Both recorded runs invoke actual `main`, real archived arrangement/view loading,
production prompt/message construction, writing/completion and file export.
Only `_real_client` creation is replaced with a canned provider boundary; sockets
are denied. Normal reply is the saved historical raw response. Completion reply
is an explicitly synthetic path-probe fragment. Both actual saved message arrays
are compared against the provider-boundary arguments; ordinary raw JSON files
are read back and compared to the supplied responses. Input/result chapter and
unit IDs, plus completion task IDs, remain `CH02`, `CH02:U3`, `CH02:U3_P01`.

Production reports retain `mode=run`, `model_calls=1` and historical response
usage fields because this exercises the real run export branch. Those are **not
new live calls or charges**: authoritative probe metadata records zero actual
provider calls. This is no scientific-quality or new full-BODY result. Public
archive redactions mean current loading uses retained inline material, not
omitted original paper snapshots. No prompts, BODY execution order, scientific
content or citation logic were edited by the path repair.
