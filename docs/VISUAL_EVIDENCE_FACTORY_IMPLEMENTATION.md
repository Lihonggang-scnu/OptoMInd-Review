# Visual Evidence Factory implementation

## Purpose

The Visual Evidence Factory turns a review-level visual plan into real,
renderable, traceable figures. It is a reusable part of the Research Harness,
not a one-off asset generator for a specific optics topic.

The factory follows a reader-explanation policy:

- use relevant paper figures already extracted from the selected literature;
- compose source panels only when they form a coherent visual story;
- draw exact or approximate data locally when structured values are supplied;
- use AI only for disclosed conceptual explanation;
- keep review text when no suitable figure can be produced;
- never treat a pending request as a completed figure.

Two placement policies are enforced before materialization:

- a structural visual policy: Abstract and Conclusion get zero placements
  and zero generation requests, Introduction gets at most one total visual,
  and body sections keep the planner's 0-2 ceiling (never a quota);
- every excluded item becomes an explicit unfilled opportunity with an
  `excluded_structural_policy_*` reason, so no visual decision is silent
  and no missing visual blocks text.

## Mainline

```text
Review blueprint + final review + visual knowledge bases
  -> Visual Editor
  -> ARTICLE_VISUAL_CONTRACT.json
  -> current-run-first local retrieval (4 -> 8 -> 12)
  -> selected source-figure audit
  -> source / composite / data / conceptual route
  -> model and human-review policy
  -> FINAL_VISUAL_PACKAGE.json
  -> LaTeX figure copy + body callout
  -> PDF
```

The main entry point is `run_review_harness.py`. The standalone diagnostic
entry point is:

```powershell
py -3.11 scripts/run_visual_evidence_factory.py `
  --visual-plan <VISUAL_EDITORIAL_PLAN.json> `
  --blueprint <REVIEW_BLUEPRINT.json> `
  --review-work-dir <full_review_dir> `
  --output-dir <visual_factory_output> `
  --real-visual-audit `
  --real-image-generation `
  --budget-cny 3
```

## Source routes

### Source-derived

The image must exist locally and retain `paper_id`, DOI when available, source
path, figure/chunk identity, caption context, and review state. Current-topic
assets in `pending_multimodal_review` may be shortlisted; only selected assets
are sent to a vision model. The full visual corpus is never batch-read by a
multimodal model.

Selected source figures are audited against the actual target section. The
vision prompt receives the section id, section title, the full section text
(or a documented generous context window of 12000 characters, deterministically
truncated, never summarized), the section's argument role, and the intended
placement anchor, together with the source caption and paper identity. The
auditor answers only compact content-bearing fields (verdict, section_fit,
usefulness, misleading_risk, caption_notes, reason); the local code owns the
envelope.

Reject semantics: an explicit `verdict == "reject"` or
`section_fit == "unrelated"` removes the placement to an unfilled
opportunity with the full audit retained, regardless of the model's
`misleading_risk` (risk is informational, never a gate that silently admits
a reject). Direct, contextual, and genuinely useful weak background are
accepted. Transport/invalid-JSON failures remain fail-open through the
deterministic traceability fallback with warnings, never a pipeline block.

Source figures carry their permission state, `publication_eligible` and
`publication_eligible_reason`, and an internal-study rights notice when the
source is not explicitly publication-eligible, together with source map,
source caption separation, and attribution.

### Explanatory data visual

When structured values are supplied, Pillow renders a deterministic chart at
zero model cost. The figure records whether values are `exact` or
`approximate`. Approximate figures disclose that they are not a
unified-condition ranking.

### Conceptual generated

Qwen-Image is attempted only when the remaining budget permits. Because image
models may misspell scientific labels, the robust fallback asks a Qwen text
model for a constrained diagram specification and uses Graphviz to render it.
Every result is labelled as AI-assisted/AI-generated and is not scientific
evidence.

## Cost and cache controls

- The visual budget is a hard sub-budget of the global Review Harness budget.
- Image generation has a conservative reference cost before it is attempted.
- A generated image gets at most one retry.
- Source-figure audits are keyed by image hash, the actual target section
  context (id/title/full-text window/argument role/intended anchor), the
  intended purpose/caption, and the auditor prompt content hash, so an old
  caption-only approval can never be reused for a different section or a
  changed prompt.
- Structured diagrams are keyed by plan, section context, prompt hash, and
  model tier.
- Direct generated-image caches are keyed by full task content rather than a
  reusable figure number, preventing cross-topic cache contamination.
- The persistent cache defaults to
  `literature_workspace/visual_evidence_cache/`.
- Every run writes a local cache snapshot and an independent cost report.

When the generation budget allows at least two images and both a mechanism
schematic and a workflow/decision schematic request exist among eligible
sections (Introduction/S01..S08), those two kinds are attempted first so the
budget is not consumed purely by file order. The selection is a generic
preference (never a hard quota and never topic- or section-id-specific); if
either kind is absent, the original order is kept. Generated diagrams stay
disclosed and explanatory-only.

Required cost fields include visual calls, diagram-spec calls, generation
calls, tokens, estimated CNY, cache hits, cost per final figure, and unfilled
visual opportunities.

## Human review semantics

The backend writes `VISUAL_REVIEW_QUEUE.json`.

- In test mode, figures are accepted by the system. Figures carrying model
  warnings use `system_approved_test_mode_with_warnings`.
- In production headless mode, a 30-second no-response policy records
  `timeout_accepted_for_draft`; it never claims that a person approved it.
- An explicit human rejection is fail-closed.

The future UI can submit `approve`, `reject`, `edit_caption`, `replace_image`,
or `regenerate`.

## Canonical artifacts

- `ARTICLE_VISUAL_CONTRACT.json`
- `FINAL_VISUAL_PACKAGE.json`
- `FINAL_VISUAL_AUDIT_REPORT.json`
- `VISUAL_REVIEW_QUEUE.json`
- `VISUAL_COST_REPORT.json`
- `VISUAL_EVENTS.jsonl`
- `VISUAL_AUDIT_CACHE.json`
- `figures/`

The final package is the only visual contract consumed by the updated LaTeX
renderer. A figure is renderable only when the file exists, a canonical
caption exists, a placement anchor exists, provenance is valid, and its review
state is accepted.

## Acceptance evidence (2026-07-29)

### Cross-topic, zero-cost engineering regression

The regression covers:

1. passive daytime radiative cooling;
2. quasi-BIC optical sensing;
3. inverse design of multilayer optical coatings.

All three produced an isolated final visual package, a real local image, a
contract, and event logs with zero API cost and no tested topic leakage.

Report:
`outputs/visual_factory_regression/cross_topic_20260729/CROSS_TOPIC_REGRESSION_REPORT.json`

### Real Qwen visual run

A real quasi-BIC visual run produced four renderable figures:

- one traceable paper figure;
- three AI-assisted structured explanatory diagrams;
- three lower-priority opportunities preserved as unfilled.

The visual-factory cost was about CNY 0.02. A repeated run with the same shared
cache produced the same four-figure package with four cache hits and zero new
model cost.

First and cached runs:

- `outputs/visual_factory_real_smoke/qBIC_cache_first_20260729/`
- `outputs/visual_factory_real_smoke/qBIC_cache_second_20260729/`

### Publication integration

The LaTeX renderer copied all four accepted figures, inserted four body
callouts, compiled a 22-page PDF, and reported no unresolved citations, LaTeX
warnings, overfull boxes, mojibake, or broken figure paths.

Artifacts:

- `outputs/visual_factory_real_smoke/qBIC_final_latex_20260729/main.pdf`
- `outputs/visual_factory_real_smoke/qBIC_final_latex_20260729/LATEX_BUILD_REPORT.json`

The publication status remains `compiled_awaiting_metadata` because real author
metadata and one bibliography record are incomplete. This is unrelated to the
visual chain.

## Known limits

- A generated explanatory diagram may simplify a requested curve or apparatus.
  Such limitations remain visible as review warnings.
- Test-mode acceptance is appropriate for reader explanation and system
  testing; publication-strict mode still requires human review.
- The current backend records a 30-second review deadline but does not itself
  implement the interactive front-end dialog.
- LaTeX page-break aesthetics are intentionally secondary here; the current
  stage guarantees valid content, paths, captions, and compilable output.
