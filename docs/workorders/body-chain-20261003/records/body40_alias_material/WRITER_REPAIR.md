# BODY40 second stop: explicit alias material consumer

Base: `e75c66c1ffd018a93636960caa7c51e74911c2af`.
Runtime edit: `optomind_research/runtime/upgrade3/review_unit_writer.py` only.

## Reproduction and result

- Initial synthetic real `build_chapter_view` → `build_source_catalog` → persisted arrangement → `build_unit_view` tests: 9 failures, 1 pass before runtime edits. Canonical-only producer catalog with explicitly declared alias caused a false missing-material row.
- Final dedicated tests: 18 passed. Focused regression selection: 194 passed, including first-stop historical binding, numeric citations, diagnostic handoff, completion, output consumption, portable paths and short chain.
- Public archived Ch6_U1 and Ch6_U2: baseline and repaired real consumers replayed using the same explicit portability adapter. Both baseline/current consumers use historical max_material_chars_per_source=0 and planning_revision=True. The production unit_messages serializer is also replayed and its final user payload inspected: both baseline alias rows were missing. Repaired output has one P0049 material with P0605 alias; no missing P0605 row; original task IDs and source uses unchanged. System prompt and serialized task payloads are unchanged. Canonical science fields are hash-identical to the corresponding historical writer inputs. See `ACTUAL_CONSUMER_REPLAY.json`.

## Boundary

Only `source_catalog[canonical].aliases` establishes equivalence. No title, DOI or record-ID search creates aliases. Invalid declarations, multiple owners, self aliases and aliases colliding with independent catalog keys fail closed. This deliberately also rejects apparently compatible direct-key collisions instead of choosing between independent rows. Where the original packet is available, contradictory DOI or uncorroborated contradictory record IDs fail closed. Distinct historical IDs with the same normalized DOI are accepted.

The writer emits canonical material once and preserves original requested task handles. Completion uses the same declared identity class and symmetrically retains source-linked tool content. Both canonical and declared alias citations are recognized; citing either does not falsely mark the equivalent requested handle unused. `source_count` now counts unique material rows, while original requested handles remain in tasks and input handle metadata. Message payloads can change to carry the corrected material and citation identities; prompt text and scientific prose are not edited.

A narrow adjacent diagnostic correction recognizes nonempty supplemental/local/tool text, including source-linked chapter tool text, without requiring a review-reported original to have its own A/B card. Identity/title/status metadata alone still produces the no-material warning. This does not invent A/B, perform reading, alter source eligibility, or assert independent validation of a review-reported result.

## Limits and safety

- No owner/planner/arrangement algorithm change; no fuzzy-title merge added; no scientific content, historical BODY or acceptance archive edited.
- Cached arrangements are consumed through `build_unit_view`; stale saved UNIT_INPUT files are not silently rewritten or repaired in place.
- Existing first-stop card identity conflict behavior remains conservative and unchanged, including rejecting a foreign card paper ID even if its DOI matches. The canonical locator is read once; no new alias-card import behavior is introduced.
- The archive contains unavailable Windows locators. On Linux, one is too long when interpreted as a literal filename. The replay adapter marks only exact foreign card/packet paths named in that archived catalog unavailable, equally for baseline and current consumers. It does not rebind locators, supply content or change serialized inputs. This is not native Windows/provider acceptance. A dedicated synthetic test verifies normal real JSON-card I/O separately.
- Content replay source is the already-public acceptance archive. Native snapshot-selection metadata selects body/body_assembly_final/arrangements/Ch6/CHAPTER_ARRANGEMENT.json with explicit view_path=body/arrangement_repaired/Ch6/ARRANGEMENT_INPUT.json. Both baseline/current use those exact public files. Public final/repaired arrangement copies are hash-identical; original-local snapshot equality is not assumed. No native raw manuscript or material content is replayed.
- No network, model or full BODY call in these offline checks. Publication is tracked separately by the final source commit. Replay records contain paths, IDs, counts, booleans and hashes only; synthetic test text is explicitly labeled.

## Reproduce

Environment: `PYTHONHASHSEED=0`; `PYTHONPATH=docs/workorders/body-chain-20261003/records/body40_identity_citations/offline_bootstrap:/tmp/optomind-body40-deps:.`.

Run `python -m pytest -q tests/upgrade3/test_body40_explicit_alias_material.py`.
Run `python docs/workorders/body-chain-20261003/records/body40_alias_material/replay_archived_consumer.py` from the repository root.
