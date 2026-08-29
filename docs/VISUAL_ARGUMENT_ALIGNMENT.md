# Visual Argument Alignment v1

This document records the active implementation for Step 1b / M4.

## Purpose

The goal is not to add a temporary label to the current 1045 visual chunks. The goal is a reusable chain:

```
visual chunk
  = image + caption + nearby text + body callout + existing visual tags
    -> VisualArgumentClassifier
      -> visual_argument fields
        -> ReviewKnowledgeBase JSONL + SQLite
          -> later claim-level and blueprint-level visual evidence selection
```

The classifier answers:

> What kind of scientific-review argument can this visual support?

It should not merely answer what the image looks like.

## Stability policy

The chain must not fake success. If the first VLM call returns invalid JSON, times out, produces an unusable answer, or cannot access the image, the record is not silently downgraded into a generic category.

The active policy is:

1. Run the main multimodal classifier.
2. Parse and validate strict JSON.
3. Apply deterministic boundary rules.
4. Automatically retry failed rows with a stronger vision model tier, default `vision_premium_model`.
5. Optionally run a high-tier audit sample and compare labels.
6. If a row still fails after retry, leave `visual_argument_type` empty, mark `visual_argument_status="failed"`, write `visual_argument_failure_reason`, and save an unresolved chunk-id list.

Downstream systems should use only rows with:

```text
visual_argument_status == "ok"
visual_argument_type != ""
```

This is deliberate: unresolved visual chunks should be isolated rather than polluting figure selection or claim binding.

## Active implementation

- Prompt: `prompts/Visual Argument Classifier.txt`
- Module: `optomind_research/visual_argument_classifier.py`
- Default KB: `outputs/review_knowledge_base/core58-rkb-hqvisual-v1-20260703`
- Main JSONL: `records/visual_chunks.jsonl`
- Main SQLite: `review_knowledge_base.sqlite`

## Active 8-type schema

The active schema uses 8 argument types:

1. `mechanism_anchor`
2. `taxonomy_or_roadmap`
3. `method_or_workflow`
4. `quantitative_comparison`
5. `trend_or_parameter_map`
6. `representative_example`
7. `anomaly_or_limitation`
8. `synthesis_overview`

The older 5-type plan is superseded. The D-model-only plan is also superseded. The active chain uses a Qwen vision-capable model tier, currently `vision_plus_model`, with deterministic boundary rules after the VLM output.

## Fields written to each visual chunk

Each visual chunk receives:

- `visual_argument`
- `visual_argument_type`
- `visual_argument_claim`
- `visual_argument_confidence`
- `visual_argument_needs_human_review`
- `visual_argument_schema_version`
- `visual_argument_model_tier`
- `visual_argument_prompt`
- `visual_argument_classified_at`

SQLite receives queryable columns:

- `visual_argument_type`
- `visual_argument_confidence`
- `visual_argument_claim`
- `visual_argument_needs_human_review`
- `visual_argument_schema_version`
- `visual_argument_status`
- `visual_argument_failure_reason`

The full record is also preserved in `visual_chunks.raw_json`.

## Boundary rules

The VLM performs semantic interpretation. The program then applies conservative consistency rules:

- Experimental apparatus, measurement setup, circuit diagrams, fabrication processes, roll-to-roll lines, and methodology flowcharts are `method_or_workflow`.
- A single structure schematic is not automatically `taxonomy_or_roadmap`; taxonomy is reserved for multi-route, classification, roadmap, landscape, or historical-evolution visuals.
- `synthesis_overview` is not a fallback. It is reserved for high-level overview or review-style synthesis figures.

This keeps the chain stable for future upstream papers instead of relying on one-off manual fixes.

## Commands

Calibration run:

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:QWEN_HTTP_TIMEOUT_SEC='180'
py -3.11 -m optomind_research.visual_argument_classifier --sample-size 50 --workers 4 --run-id calibration-visual-argument-50
```

Full run with write-back:

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:QWEN_HTTP_TIMEOUT_SEC='180'
py -3.11 -m optomind_research.visual_argument_classifier --sample-size 0 --workers 8 --run-id core58-visual-argument-full-v1 --write-back
```

Retry failed chunks:

```powershell
py -3.11 -m optomind_research.visual_argument_classifier --chunk-ids-file path\to\error_chunk_ids.txt --workers 2 --max-tokens 2200 --run-id retry --write-back
```

Run with automatic retry and high-tier audit:

```powershell
py -3.11 -m optomind_research.visual_argument_classifier --sample-size 0 --workers 8 --auto-retry-errors --retry-model-tier vision_premium_model --audit-sample-size 32 --audit-model-tier vision_premium_model --run-id full-with-audit --write-back
```

## Current core58 result

Final KB audit after full run + retry:

- visual chunks: 1045
- missing `visual_argument_type`: 0
- SQLite classified rows: 1045
- `visual_argument_status=ok`: 1045
- CJK in visual argument outputs: 0
- low-confidence count: 0
- needs-human-review count: 4
- max type share: 24.31%

Distribution:

- `quantitative_comparison`: 254
- `mechanism_anchor`: 253
- `method_or_workflow`: 205
- `trend_or_parameter_map`: 157
- `representative_example`: 119
- `anomaly_or_limitation`: 31
- `taxonomy_or_roadmap`: 22
- `synthesis_overview`: 4

Final audit files:

- `outputs/visual_argument_alignment/final_kb_visual_argument_audit.json`
- `outputs/visual_argument_alignment/final_audit/final_visual_argument_audit.md`
- `outputs/visual_argument_alignment/final_audit/final_visual_argument_audit_contact_sheet.png`

## Relationship to M2a

M2a claim decomposition does not depend on this step.

M2a depends on Step 0:

- `optomind_research/claim_schema.py`
- `llm/proposer_critic.py`
- `prompts/Claim Decomposer.txt`

Visual argument alignment can run in parallel with M2a. It becomes important later when claims are bound to visual evidence and when the blueprint planner selects figures for specific claims.
