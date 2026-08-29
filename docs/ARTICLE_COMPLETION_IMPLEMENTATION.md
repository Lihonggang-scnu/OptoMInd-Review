# Review Article Completion Pipeline

Status: implemented mainline stage  
Language rule: all model-facing intermediate messages and artifacts are English  
Primary entry: `run_review_harness.py`

## Purpose

The body-section pipeline already researches, writes, cites, audits, and
revises scientific sections. This stage turns those sections into a complete
review article without treating the introduction, outlook, conclusion, and
abstract as independent body chapters.

## Runtime sequence

```text
Review Lead
  -> REVIEW_BLUEPRINT.json
     -> article_rhetorical_contract
     -> body sections only

Section Review Authors
  -> audited section drafts
  -> SECTION_HANDOFF_CARD.json

Article Completion Editor (one bounded A-model task)
  -> ARTICLE_SYNTHESIS_MAP.json
  -> ARTICLE_COMPLETION_PACKAGE.json
  -> title / abstract / introduction / challenges-outlook / conclusion

Deterministic stages
  -> GLOBAL_FIGURE_PLAN.json
  -> COMPLETE_REVIEW_EN.md
  -> ARTICLE_STRUCTURE_AUDIT.json
  -> ARTICLE_FIGURE_PLACEMENTS.md

Existing Visual Editor and Visual Evidence Factory
  -> selected source figures
  -> composites
  -> disclosed generated explanatory figures
  -> FINAL_VISUAL_PACKAGE.json
```

## Important boundaries

- `blueprint.sections` contains scientific body sections only.
- `article_rhetorical_contract` plans article-level rhetoric.
- Section handoff cards are writing memory, never evidence.
- Paper, text-chunk, and visual IDs in handoff and synthesis artifacts are
  recomputed or filtered by deterministic code.
- The abstract is written last and contains no citation markers.
- The conclusion may not introduce a new topic or new evidence.
- Global figures are selected by eligibility. Field maps, timelines,
  benchmark landscapes, and challenge roadmaps are candidates, not mandatory
  decorations.
- Missing figures do not invalidate scientifically useful prose.
- The complete English manuscript is the input to the existing translation
  and LaTeX publication stages.

## New main artifacts

| Artifact | Role |
|---|---|
| `article_rhetorical_contract` in `REVIEW_BLUEPRINT.json` | Plans the complete article before body research |
| `SECTION_HANDOFF_CARD.json` | Compact cross-section writing memory |
| `ARTICLE_SYNTHESIS_INPUT.json` | Bounded, traceable aggregation input |
| `ARTICLE_SYNTHESIS_MAP.json` | Cross-section consensus, disputes, trade-offs, challenges, and opportunities |
| `ARTICLE_COMPLETION_PACKAGE.json` | Title, abstract, introduction, outlook, conclusion |
| `GLOBAL_FIGURE_PLAN.json` | Eligibility-based article-level figure plan |
| `COMPLETE_REVIEW_EN.md` | Complete English content |
| `ARTICLE_STRUCTURE_AUDIT.json` | Whole-article deterministic quality report |
| `ARTICLE_FIGURE_PLACEMENTS.md` | Canonical image paths, captions, provenance, and placement guidance |

## Cost design

- The historical authoring allocation is reduced from 19.5 CNY to 17.5 CNY.
- A 2.0 CNY article-completion envelope is added.
- Default preflight maximum remains unchanged.
- One completion agent writes all front/back matter. Four independent premium
  calls are intentionally avoided.
- Global figure planning, assembly, ID filtering, and structural audit are
  deterministic and cost no model tokens.
- The next-call cost reserve blocks only another model call. It never discards
  an already-paid tool submission.
- If a valid package was written immediately before a budget stop, the runner
  performs deterministic post-write validation and records a completed result
  without another model call.
- An unchanged body fingerprint reuses the validated completion package at
  zero additional model cost.

## Quality and safety gates

- Exact numeric assertions about existing performance require a verified
  `[REF:paper_id]` in the same sentence.
- Unreferenced numbers are accepted only when explicitly presented as a
  proposed target, future milestone, recommended benchmark, or success
  indicator.
- Word-count targets guide the writer. A 90% hard floor prevents trivial or
  incomplete components while avoiding repeated premium rewrites over
  editorially meaningless five-word shortfalls.
- Avoidable contrast phrasing is recorded as an editorial warning rather than
  forcing a full article rewrite.
- Scalar strings in declared string-list fields are losslessly normalized to
  one-item arrays; identifiers and object collections are never guessed.

## Real validation checkpoint

The controlled qBIC/MIR gas-sensing validation reused seven audited body
sections and did not perform retrieval or body rewriting. The completed
manuscript contained 9,080 words, seven body sections, and 32 referenced
papers. The structural audit passed with no blocking or non-blocking flags.
The completion stage consumed 159,347 input tokens, 23,168 output tokens, and
an estimated 2.746212 CNY across the original run plus one targeted numeric
claim revision. A clean run is expected to cost less because the schema,
word-floor, and numeric-claim instructions are now present before the first
submission.

## Standalone command

```powershell
py -3.11 scripts/run_article_completion.py `
  --blueprint <REVIEW_BLUEPRINT.json> `
  --sections-root <authoring/full_review/sections> `
  --output-dir <article_completion> `
  --model-tier premium_model `
  --cost-budget-cny 2.0
```

The normal user path should use `run_review_harness.py`; the standalone
command is for stage-level diagnosis and controlled reruns.
