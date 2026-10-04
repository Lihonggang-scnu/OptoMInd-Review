# WO07 entry: independent reading of both WO06 rounds

Baseline: `841e9143972c408a3b0f42f04e955efa2137c9b4`. Local citation fix: `252bb7c399680692500881486748d2ecb7ad3f7e`; previous cloud code: `ab94095ab56983ab13d4d634be2e6e74fc9fcb4a`.

## What was actually read

`docs/acceptance/body06-real-20261004/`: README, ROUND2_ACCEPTANCE, round1/ASTRA_ACCEPTANCE_ORIGINAL, NORMAL_BODY, COMPLETION_ORIGINAL_BODY, COMPLETION_FRAGMENT, COMPLETED_BODY, saved response content and reports, round2/REPLAY_COMPARISON and failure/pass logs, plus both inputs/*SAFE_MESSAGES.json views. The production citation diff and new regression tests were inspected.

## Verified versus reported

- Round1 had two local paid writer/completion calls. Round2 reused these responses; it was deterministic replay, **not another generation experiment**. Reported new cost was 0.1036264 CNY total, with no cloud spending authorized.
- The local code change only permits pure P-handle inline-code spans during citation counting. Numeric citation repair retains the protected path. No BODY prompt or scientific prose was changed.
- Independent production parsing reproduces five historical citations, five ordinary citations, and eight completion-fragment citations. The completion preserves original bytes as prefix and the new fragment as suffix; ordinary archive adds a terminal newline. These are output/identity checks, not scientific validation.
- The root freshly reran the previous bounded selection: 476 passed, two existing exclusions. The local 69 targeted and 476 control totals overlap by 53; the distinct local total is 492, not 545.
- Public input views omit paper A/B, full card and tool prose. They expose tasks/system messages and channel metadata, **not full replay requests**. Omitted numerical support remains LOCAL_ONLY.

## Independent content observations

1. Ordinary input task CH05_U01_P01 explicitly says the Phase III result is `unjudgeable` because of provider outage and that this constrains the current statement rather than establishes a field-wide absence. NORMAL_BODY's opening instead says confirming Phase III evidence is lacking. The qualifier is visible in the input: this is observable generation/selection loss, not demonstrated code transport loss. Short-chain fixtures should preserve uncertainty into messages; deterministic tests cannot prove a model will obey it.
2. The completion table's first row places ORR 75% and mOS 52.8 months together for LUMINate/MIMic without independently assigning both results. Its preamble strengthens antibiotic associations to “被证实”. Preserve these content limits rather than silently correcting the archived text.
3. The ordinary task and table row explicitly include TACE+MWA+ICI as a comparison, despite the stated microbiome subject. This scope choice predates WO06 and is not caused by citation recognition.
4. A real table was produced in round1. The separately returned `table_markdown` exercise was a controlled rearrangement of actual generated text, not an observed supplier response shape.

## Decision

Retain the local citation fix and proceed with WO07's four bounded offline short chains. No evidence justifies another paid WO06 trial or a topic-specific prompt change. Check actual persistence, source identity, source text, argument/task IDs and pending delivery boundaries. Keep content quality and engineering completion separate. Original task order and review-derived original-source policy remain in force.

The root additionally reproduced a delivery boundary defect: a complete *loading* summary with `problems_resolved=false` (missing table or length-truncated writer output) still entered the text editor. A narrow gate is justified within WO07. Existing downstream identity-resolution-only gaps must continue to use their supplied catalog path; do not broadly block every unresolved citation before its resolver.
