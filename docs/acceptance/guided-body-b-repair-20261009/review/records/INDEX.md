# Guided body B repaired run — local index

- Source commit: `145d68788037b4aa31295253d0b0d06f7de88685` (detached worktree clean).
- Same original shared ledger: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`.
- Fixed campaign S0: `18.3609232` CNY settled, effective cap `58.3609232` CNY; current ledger: 31 settled rows, `24.4610072` CNY, 0 reserved/uncertain.
- Three official run manifests, seven physical paid author calls; cached stages were reused during recovery. No paid retry and no upload.

## Final manuscript artifacts

- `B_LIVE\FULL_BODY.md` — canonical assembled body; SHA-256 bytes `0b976f11976687efb5b60f636ff92331a9437a7fdde81272be6ad212d9dc3abb`.
- `B_LIVE\DELIVERY_BODY.md` — official handle-normalized delivery copy.
- `B_LIVE\NUMBERED_DELIVERY_BODY.md` — offline output from `build_delivery_citation_map` and `render_reader_citations`.
- `B_LIVE\REFERENCES.json` — 95 mapped identities.
- `records\DELIVERY_CITATION_AUDIT.json` — 216 occurrences, 95 unique tokens, 0 unknown/residual; 23 Ch7 fenced citations numbered; non-citation projection preserved.

## Execution and finance

- `records\FINAL_EXECUTION_STATE.json` / `LIVE_FINAL_STATE.json` — three sessions, seven calls, source/input hashes, result paths, recovery provenance.
- `records\FINAL_FINANCE.json` — baseline/new/total ledger reconciliation.
- `records\B_STAGE_AUDIT.json` — exact four-field request audit, cumulative prefixes, actual request/usage/raw hashes, wire profile, cache and physical-call accounting.
- `records\B_STOP_REPORT.json` and `B_RESUME_STOP_REPORT.json` — preserved metadata-stop evidence; no raw files were replaced.
- `records\LIVE_COMMAND.txt`, `LIVE_RESUME_COMMAND.txt`, `LIVE_RESUME2_COMMAND.txt` — exact wrapper commands with credential path only.
- `records\B_LIVE_STDOUT.log`, `B_LIVE_RESUME_STDOUT.log`, `B_LIVE_RESUME2_STDOUT.log` — preserved CLI stdout/stderr.

No production source, guide, canonical body bytes, old ledger rows, or public export was changed by the offline closeout.
