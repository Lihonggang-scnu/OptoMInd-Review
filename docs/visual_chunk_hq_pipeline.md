# Visual Chunk HQ Pipeline

This document describes the reusable high-quality visual chunk pipeline used by
the review-planning system. The goal is not to patch a few images for one test
case, but to maintain a stable workflow that converts paper figures into
review-ready visual evidence.

## Scope

Only two visual chunk types are first-class review assets:

- `single_figure`
- `subfigure`

`parent_figure` records may be kept as context, but they are not treated as
review assets. A parent image often contains multiple panels and can confuse
both visual tagging and downstream figure planning.

## Input

Primary source used in the current checkpoint:

```text
outputs/visual_chunks/core58_visual_chunks_tagged_full_1pct_sanitized_20260703/visual_chunks.tagged.1pct_sanitized.jsonl
```

Each record should include at least:

- local image path
- paper ID / DOI / title
- figure label and subfigure label
- caption
- nearby text
- body callout text
- prior crop metadata

## Core scripts

```text
experiments/retag_visual_chunks_hq.py
experiments/merge_visual_hq_batches.py
experiments/audit_visual_hq_final.py
```

The retagging script calls a Qwen-VL model to produce
`visual_chunk_hq_profile.v1`. The prompt is intentionally task-local: it does
not assume the model knows anything about OptoMind.

Prompt file:

```text
prompts/Visual Chunk HQ Tagger.txt
```

## Output

Current accepted final output:

```text
outputs/visual_chunks/core58_visual_chunks_hq_single_subfig_final_20260703/visual_chunks.hq_tagged.final.jsonl
```

Final audit file:

```text
outputs/visual_chunks/core58_visual_chunks_hq_single_subfig_final_20260703/final_quality_audit.json
```

Human audit contact sheets:

```text
outputs/visual_chunks/core58_visual_chunks_hq_single_subfig_final_20260703/human_audit/
```

## Final audit counts for the current core58 run

- total visual chunks: 1045
- subfigures: 906
- single figures: 139
- parent figures: 0
- rows requiring human review: 3
- fallback profiles: 1
- CJK rows: 0
- unclear rows: 0

Major role distribution:

- graph: 335
- schematic: 304
- photograph: 103
- micrograph: 66
- spectrum: 51
- mixed: 60
- bar chart: 24
- heatmap: 13
- device structure: 31

## Quality policy

Quality checks must inspect content, not just counts. For each batch, check:

- whether the crop contains the intended single panel or single figure;
- whether border fragments are ignored unless supported by caption/text;
- whether the visual role is specific enough for review planning;
- whether the caption and nearby text align with the current image;
- whether `direct_use_candidate`, `redraw_recommendation`, and
  `needs_human_review` are credible;
- whether the visual can support a concrete argumentative function in a review.

If the output quality is poor, fix the pipeline or prompt first. Do not patch
individual images as the main solution.

## Example commands

Small debugging run:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
py -3.11 experiments\retag_visual_chunks_hq.py --real-vl --sample-size 60 --workers 6 --log-every 10
```

Batch run:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
py -3.11 experiments\retag_visual_chunks_hq.py --real-vl --selection-mode all --batch-size 100 --batch-index 0 --workers 6 --log-every 10
```

Merge accepted batches:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
py -3.11 experiments\merge_visual_hq_batches.py
```

Audit final merged output:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
py -3.11 experiments\audit_visual_hq_final.py
```
