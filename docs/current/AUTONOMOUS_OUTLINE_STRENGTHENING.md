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

The optional `--mode select` entry is a first layer before on-demand strengthening. It reads the complete detailed plans for the supplied chapters, the research question, whole-review duties, conditions, argument relations, paragraph briefs, supporting-study uses, transitions, and chapter-level duties. The model-visible view preserves every chapter plan field exactly, removes absolute card locations and repeated exclusion inventories, and represents repeated neighbor duties as stable IDs. It does not copy full source cards or the whole identity directory into this first request by default; the downstream owner still receives the original complete chapter payload and can request material through the existing access path. Add `--include-selection-material-index` only when the caller explicitly wants the compact shared identity directory.

The selector chooses any number of worthwhile units, can return `none` or `no_change`, and may place units from different chapters in one logical group. It supplies a reason and improvement focus only as machine-generated advisory provenance. The projection creates one owner payload per affected chapter, keeps the shared group identity and reason, makes other group units readonly context, and automatically retains every unselected unit in that chapter as readonly context. It never generates an updated plan or scientific answer.

Prepare the selector offline first:

```text
python scripts/upgrade3/outline_strengthening.py --mode select --input <selection-payload.json> --output <selection-run-dir> --selection-profile outline_selection --tokenizer <qwen-tokenizer.json>
```

The prepared request uses the explicit `outline_selection` profile (`qwen3.8-max`, 32768 thinking and 32768 answer tokens). A paid selector run requires the same prepared directory, budget ledger, and key file with `--run`; it is an opt-in first layer and is not automatically connected to every chapter. Its validated output can be passed to `selection_to_on_demand_payloads`, after which the existing on-demand material access and owner contracts apply. Ordinary BODY and whole-paper defaults remain unchanged.
