# Case-cache operational provenance path repair

Base: archive commit `4b115e25b900c7832996b4c8213dc657109151d5`.
All checks are offline. No paid provider calls, retrieval, full BODY experiment, commit, or push.

## Independent reproduction and minimal repair

`BEFORE.txt` and `BEFORE_RESULT.json` record a production planner run and fresh-planner resume. A saved synthetic partial review-derived original study (no own A/B) was byte-copied to a second directory. Both copies passed `load_prior_readings`; the only material difference was the loader-generated `reused_from`. The initial run made one offline case-model call. The moved resume made another case call and generated a different `input_contract`.

The production change adds only `reused_from` to `_cache_contract`'s existing explicit operational-metadata allowlist. It does not strip the request/record, change `_case_selection_material_row`, model prompts, execution order, or material identity/content compatibility. Named fields inside scientific subtrees remain protected, including a literal `reused_from` key inside question material, A/B, conditions, and required outputs.

`AFTER.txt` and `AFTER_RESULT.json` show the same actual consumer sequence now reuses the cache after moving the identical reading: one initial case call, zero on moved resume, same contract. The initial actual model request retains the original path; the final writer packet retains the current moved path. Changes to consumed finding, conditions, research task, effective model, and output limit each produce a changed contract and exactly one case call. Only the provider boundary is substituted for these planner runs; loading, projection, serialization, batching, cache lookup, reentry, and packet construction run production code. Network sockets are blocked.

## Cache gates and upgrade boundary

The case batch gate continues to require complete status, matching chapter ID, batch index, route handles, unit-source signature, input contract, and case prompt contract. `material_signature` and `task_signature` remain diagnostic fields; neither is used as an independent cache gate. In particular the diagnostic raw material hash may include a path while the effective input contract omits it.

An old saved hash computed with a nonempty `reused_from` is not retroactively normalized. The first ordinary resume under this repair can refresh that batch once; new normalized contracts support path-only reuse thereafter. This is not a promise of universal zero-cost reuse across the software upgrade.

## Temporal A/B shadow: risk only, unchanged

The saved real P0582 row in `ATTACHED_PACKET.json` has A, B, and deep. A targeted control verifies this. Removing just deep and providing it only through the independent map reproduces the resolver's existing first-substantive-row behavior; this artificial order still does not merge that later deep. No broad merge change is made.

Static path inspection and a normal production planner run support the narrower conclusion: `_chapter_details` gathers level1, level2, chapter, prior, and in-memory readings before `build_local_material_payload`, which combines compatible A/B and deep. The new normal-path test proves both A/B and deep reach the actual case request. The owner resolver also offers local material with deep before its identity-safe merge. Thus no actual loss is established in the saved packet or tested normal order. The synthetic helper condition is reachable if called with a substantive stale row and only a later independent map; whether other unusual recovery/resume orderings create that state remains a risk, not a proven production loss or a reason for blanket overwriting.

## Reproduce

`PYTHONPATH=/tmp/optomind-stage2-deps:. python -m pytest -q tests/upgrade3/test_preflight_final_case_path_cache.py`

Use `PREFLIGHT_FINAL_CASE_EVIDENCE_ROOT` with a fresh directory to retain the complete synthetic local run artifacts. No historical fulltext snapshots are required or reconstructed. The historical compatibility package was read as evidence, not misrepresented as a byte-exact replay input.

`REGRESSION.txt`: 64 passed across the new test, existing real case-consumer tests, stage/chapter cache contracts, identity compatibility, recovery context, and material review regressions. The focused test alone: 4 passed.

## Actual contents comparison

`ACTUAL_CONTENT_EXCERPTS.json` contains explicitly labeled extracted projections of the actual initial case user message and the moved-resume writer packet, with each source artifact's local path and SHA-256. It shows the exact `reused_from`, answer, conditions, limitations, and status. The case message intentionally uses the existing 1200-character string bound; the writer packet keeps the untruncated answer. Their scientific selection projections are asserted equal. The local packet snapshot was captured before the later science/settings invalidation controls and is not a full request in this evidence package.
