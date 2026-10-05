# BODY40 historical paper identity isolation

## Result and scope

The first-stop identity defect is reproduced and corrected without model calls, research retrieval, scientific prompt changes, stage-order changes, BODY regeneration, or edits to the historical archive.

- Before production edits: the initial dedicated suite had **8 failures, 2 passes** (`BEFORE_TESTS.txt`). These expose unoccupied foreign labels in actual judge requests, retained labels in tool handoff, and the archived TACITO passage.
- After edits and added controls: **67 passed** across dedicated identity, local lookup, and consumer-recovery tests (`AFTER_TESTS.txt`). An additional **47 passed** across existing owner-handoff, retrieval short-chain and supplement-contract tests (`HANDOFF_TESTS.txt`). These groups do not overlap.
- This verifies program identity continuity, not scientific correctness or unattended whole-pipeline production readiness.

## Production changes

1. `_reading_passage` takes a P label only from `LocalGap.current_source_handles` for its stable paper ID. Without that current binding it emits the stable paper ID, even when the historical label is still unoccupied.
2. Bounded paper contexts use the identical current-map/stable-ID rule. Useful passages, openings and sections are retained.
3. `_bind_tool_material_source_handles` resolves a stable ID from the current pool, or a compatible source-unit binding. A source-unit ID cannot override a contradictory paper ID. An unregistered stable source retains its stable ID. A handle-only source cannot establish its originating run, even when that label exists in the current pool. It retains the original label in `unresolved_source_handle`, with no asserted current handle; its narrative is preserved. A compatible registered source-unit binding can provide its stable paper ID.
4. A regression follows handle-only unresolved material through the real packet merge and prompt compaction: useful chapter-level content and its diagnostic survive, without attaching to the unrelated current paper.
5. Existing normal source registration can subsequently assign a fresh current P label. The structured source is rebound; arbitrary existing prose is not rewritten.

`planning_supplement.py` requires no change: its checkpoint signature already includes actual payload and stable/handle source identities. Regression tests seed a successful legacy-label checkpoint, verify the corrected request triggers a separate call at the mocked model boundary, verify both attempts survive, and verify the second corrected request reuses the corrected checkpoint.

## Concrete evidence

- `BEFORE_SYNTHETIC_MESSAGE.json` / `AFTER_SYNTHETIC_MESSAGE.json`: actual production judge payloads for an invented education study, plus resulting tool material. Initially the response can echo foreign P0593. After correction the same useful claim carries `education-study`. All network connections are denied and the model boundary is a fixed response.
- `BEFORE_ARCHIVE_MESSAGE_PROJECTION.json` / `AFTER_ARCHIVE_MESSAGE_PROJECTION.json`: the exact archived TACITO passage is reconstructed through `_reading_passage` and `_triage_payload`. Before it carries P0593; after it carries `69b4c5f84be199181be6ab5d3124f306462234f3`. Text SHA-256 values are identical before/after. The projection stores identities, lengths and hashes rather than duplicating the archived paper passage. This is a request-seam replay, not a claimed complete reconstruction of omitted full texts.
- `AFTER_STAGE_REGISTRATION.json`: exact archived `RUN_STATE.stage_inputs` identity maps and pool rows are used, rather than substituting initial/final pools. Level1 has 585 rows and level2 has 587, neither with P0593. The chapters-tools input has 594 rows and P0593 belongs to `CorpusId:286644045` (allopurinol). Normal registration of TACITO on this snapshot yields P0595 while P0593 remains allopurinol.
- `ADJACENT_INITIAL.txt`: the first neighboring-test run exposed eight pre-existing assertions that expected foreign indexed handles without a supplied current map. Those identity expectations now use stable IDs. The exact ordinary-payload test explicitly supplies its current identity map, keeping its original full payload assertion.

## Reproduction

From repository root:

```
PYTHONPATH=/tmp/optomind-body40-bootstrap:/tmp/optomind-body40-deps:. python -m pytest -q tests/upgrade3/test_body40_historical_identity.py tests/upgrade3/test_body03_local_lookup*.py tests/upgrade3/test_progressive_review_plan_tool_consumer_recovery.py
PYTHONPATH=/tmp/optomind-body40-bootstrap:/tmp/optomind-body40-deps:. python -m pytest -q tests/upgrade3/test_body07_retrieval_short_chains.py tests/upgrade3/test_body04_owner_material_handoff.py tests/upgrade3/test_planning_supplement_attempt_contract.py
```

The isolated environment uses temporary pytest/json-repair/pydantic dependencies. Its bootstrap bypasses only the unrelated eager `agentscope` package initializer; actual upgrade3 implementation modules run normally. This is not an installed full-application integration test. BEFORE files were generated before production edits; do not regenerate them from corrected code and describe them as baseline evidence.

## Explicit limitations

Historical MODEL responses, plans, owner output, arrangement, writer messages and BODY prose that already contain `TACITO(P0593)` remain unsafe to reuse as current production answers. Rebinding structured sources does not repair those old free-text claims. The fix prevents fresh request contamination and invalidates the exact affected local-judge cache signatures; it does not migrate arbitrary higher-level saved pipeline states or repair old prose. The acceptance archive remains read-only evidence, not a production cache.

The historical manuscript requires later location-specific review. Never globally replace P0593: legitimate allopurinol uses must stay intact. No scientific interpretation, source quotas, aliases, or source-material completeness rules are added here.

Publication restriction: synthetic before/after request text is omitted with per-field hashes; the auxiliary replay_identity.py generator is retained locally but not newly published because it contains the same restricted fixture sentence. The dedicated historical-identity tests remain executable and verify actual constructed request content, source binding, material preservation and cache reuse. Archived-stage registration evidence is a saved offline observation, not a command to recover production.

Final upload boundary: the synthetic before/after message files themselves are excluded from this commit after the upload tool also rejected their identity-only projections. They remain local. Public executable tests contain the independent synthetic regression and inspect actual requests; the archive seam projection and stage-registration record remain the concrete identity evidence. No restricted message content is republished indirectly.
