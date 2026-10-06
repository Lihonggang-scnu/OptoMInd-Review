# Autonomous outline strengthening

> **Review status (2026-10-06):** The on-demand material-access path is an experimental candidate awaiting cloud review. It is not promoted to the production default or automatically connected to the whole-paper pipeline; the ordinary BODY prompt and defaults remain unchanged.

This is an opt-in outline planning seam. It accepts the current research question, complete original chapter plan, complete supplied source and tool materials, readonly neighboring duties, chapter context, and the actual local body. The model independently decides whether a material-backed organizational or evidence-allocation change is worthwhile and may return `no_change`.

The standalone CLI defaults to the existing `strong_outline` profile. Plus profiles (`autonomous_outline` and `reviewer_plus`) are explicit experiment choices and are not the production default. The ordinary BODY prompt and whole-paper pipeline defaults are unchanged; this entry does not automatically rerun every chapter and does not call writers or edit manuscript text.

Prepare the exact request offline first:

```text
python scripts/upgrade3/outline_strengthening.py --input <payload.json> --output <run-dir> --profile strong_outline --tokenizer <qwen-tokenizer.json>
```

A paid run is an explicit second step with the same input, prepared output, shared budget ledger, and key file:

```text
python scripts/upgrade3/outline_strengthening.py --input <payload.json> --output <run-dir> --profile strong_outline --tokenizer <qwen-tokenizer.json> --run --budget-ledger <budget.sqlite> --budget-limit 30 --key-file <key-file>
```

Use `--mode reviewed` only when an independent issue-only reviewer is intentionally part of the experiment; its reviewer and owner profiles can be named explicitly. Use `--deduplicate-materials` only for the lossless transport experiment. It replaces exact repeated large material subtrees with JSON pointers inside the model request, verifies expansion against the full original payload, and keeps the original payload for response validation and arrangement projection.

The owner contract preserves source handles, identities, conditions, argument relations, readonly neighbors, and usable material. Structural changes require explicit stable IDs and remaps; unavailable or readonly references are rejected. Accepted output is written through the existing chapter arrangement input. A valid `no_change` result is retained as such.

This seam is a reusable production capability, not evidence of a cost reduction. The 2026-10-06 bounded comparison found the Max dedup request structurally usable, while its settled charge was approximately the same as the reference Max run; Plus experiments were not promoted by the quality review.

## On-demand material access

The optional `--mode on_demand` path builds a catalog over `source_materials`, `candidate_materials`, `candidate_navigation`, and `tool_materials`, including nested source identities and source-supplied locator excerpts. Its access request asks only for material locations and paths. A local resolver then attaches the requested original records to the owner request; it does not summarize, filter away, or rewrite the records. The model-visible owner payload contains the selected complete records and the full lightweight catalog, while the local full payload remains available for response validation and arrangement projection.

Prepare this path offline first:

```text
python scripts/upgrade3/outline_strengthening.py --mode on_demand --input <payload.json> --output <run-dir> --profile strong_outline --access-profile autonomous_outline --tokenizer <qwen-tokenizer.json>
```

The prepared request records both the access request and an all-catalog owner upper-bound estimate. An explicit `--run` with the same input, output directory, profiles, tokenizer, budget ledger, and key file crosses the paid boundary. The owner uses the existing outline response validator and arrangement projection. One bounded continuation read is allowed when the owner returns a valid material request; a normal final response uses one owner call. Access responses cannot carry an updated plan, issue list, manuscript body, or revision answer. This mode is opt-in and does not change the ordinary BODY prompt or whole-paper pipeline defaults.

## Autonomous unit selection

The optional `--mode select` first layer identifies necessary, high-benefit local gaps in how the existing outline develops knowledge. It preserves every complete chapter plan. It does not redesign the review, dictate scientific answers, or require a fixed number of edits. Groups are chapter-local; other chapters remain read-only context. Invalid cross-chapter results are recorded and rejected rather than silently split into different work.

The default input is outline-first: full claims, paragraph duties, case uses and conditions, plus compact navigation for sources actually referenced in those plans. It does not send the full A/B and tool-material pool a second time. Complete records remain available to the unchanged on-demand owner. The explicit `--include-selection-material-index` option expands material diagnostics and can be substantially larger; it is not required for the normal route. Source fields are never reduced to a fixed character prefix.

`selection_reason` records why a local gap is worth addressing; `improvement_focus` and `selection_context` pass advisory responsibilities to the existing second layer. The owner verifies the premise against materials and may decline or retain the original task. No default cross-chapter restructuring or whole-chapter bundling is performed.

Prepare offline from a list or chapter-ID mapping of genuine complete chapter packets, or a newly built selector envelope:

```text
python scripts/upgrade3/outline_strengthening.py --mode select --input <complete-chapter-payloads.json> --output <selection-run-dir> --selection-profile outline_selection --tokenizer <qwen-tokenizer.json>
```

The profile remains `qwen3.8-max`, 32768 thinking and 32768 answer tokens. Explicit `--run` still requires the prepared directory, budget ledger and key file. A valid selection is projected through `selection_to_on_demand_payloads`; second-layer algorithms, prompts and profiles are unchanged. Use fresh preparation for the new contract; historical attempts remain available for audit.

See [implementation and offline message evidence](../verification/selector-local-depth-20261006/README.md).
