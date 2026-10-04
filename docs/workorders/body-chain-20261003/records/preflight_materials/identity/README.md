# F4 current material/history compatibility: bounded offline repair

## Result
- Failure-first actual consumers: 8 failures / 3 controls passed before production changes; `FAILURE_FIRST.txt`
- Final F4 dedicated controls: 21 passed; actual directed adapter/store/reader/cache, actual chapter adapter and emitted messages, socket network boundary denied
- Adjacent selected suites plus F4 and case consumer: 157 passed in 4.25s; `FINAL_ADJACENT.txt` (before test-only chapter fixture upgraded to actual QwenProgressivePlanner adapter; final dedicated21 passed again after that upgrade)
- Independent reviewer6 also passed during development; permanent reviewer3 belongs to reviewer report
- `git diff --check` passed; `_planner_instructions` and `_messages_for` AST unchanged

## Changes
1. Assigned chapter details use existing guarded material construction before their first owner request. Explicit contradictory A/B is absent from the actual messages and packet. Legacy cards without identity remain usable
2. Refresh removes previously copied incompatible A/B and permits recovery from a corrected sparse card without reviving stale text. Compatible independent deep/supplement content remains eligible; explicit foreign deep content cannot clear the saved-card quarantine
3. Prior loader rejects explicit identity conflicts using the existing guard. Saved practical PROMPT content and matching INPUT task data can recover content/task proof for old practical outputs in a fresh store
4. Adapter, legacy tool cache, adaptive directed need/journal and actual reader/store all consider current material. Normalized practical body and bibliography fingerprint content; relocation and snapshot-marker renaming do not change it. Only nominated directed sources are read
5. The actual reader's existing task hash binds content and current identity. The commit keeps that same task hash; output records content hash and identity separately. Current input identity is used consistently in messages and output even if older admission metadata remains stored
6. Body or reference corrections create a local new task for the same admitted paper and leave old artifacts intact. Superseded/foreign answers do not reappear as active question material or old R IDs under new source content. Same-source new-question history behavior is preserved
7. Root-review notes alone cannot prove unchanged historical answers. Supplied review-derived original content stays usable without own A/B or fulltext, with an explicit no-reacquire route when a read is requested

## Controls and evidence
This directory contains compact synthetic evidence:
- ASSIGNED_IDENTITY_MESSAGES.json: actual initial chapter messages/packets in both modes
- BODY_CORRECTION.json and REFERENCE_CORRECTION.json: old/new actual reader user messages, immutable old artifacts, source hashes/task identities, and next chapter user messages/current materials
- MOVE_CONTENT_REUSE.json: actual store stays one task/one committed read after content-identical relocation and snapshot rename
- LEGACY_PROOF_REUSE.json: unchanged legacy artifact + saved PROMPT reused without another provider-boundary call
- FAILURE_FIRST.txt, FINAL_FOCUSED.txt, FINAL_ADJACENT.txt

Complete synthetic replay is in `/tmp/f4-final-evidence`; the broader final replay is in `/tmp/f4-final-verified-replay`

## Existing test adjustments
- Three real-store fixtures now create/assert the content/identity-bound task hash instead of hardcoding the old empty source hash
- Material-reuse fixtures now describe the same requested paper and have real synthetic snapshot/content proof, instead of a contradictory CorpusId under a paper-1 lookup key
- Old body04 refresh test now expects copied incompatible A/B quarantined; the input record itself remains unchanged

## Boundaries
- Missing source proof is not scientific invalidity. Such history remains available but cannot confer a fulfilled exemption; if the current snapshot is absent the adapter retains history with empty current answers and a partial/unverified diagnostic
- Legacy hashes from other snapshot algorithms are not assumed equivalent to current practical-material hashes. Without saved comparable practical content proof, only the affected task is reread; old artifacts remain intact
- Legacy inner-cache reuse requires an exact saved practical prompt and matching gap contract. No arbitrary old artifact is silently upgraded
- No production model/provider/network call, keys, source research, full BODY, prompts, orchestration order, commit or push changed by this worker
- Shared progressive_review_plan.py case batching/attachment edits belong to the parallel case worker

## Reproduce

From the repository root with project dependencies available:

```sh
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-stage2-deps:. python -m pytest -q tests/upgrade3/test_preflight_material_identity_compatibility.py
```

The parent final290-test runner in the parent directory covers this final fixture revision and all bounded controls together.

The checked-in pytest failure log has trailing display whitespace removed for Git diff hygiene; assertions and outcomes are unchanged. JSON evidence is synthetic; no real paper text or credentials are included.
