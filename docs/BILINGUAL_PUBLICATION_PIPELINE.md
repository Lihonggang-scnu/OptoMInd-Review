# Bilingual publication pipeline

## Purpose

The final Research Harness can deliver the same validated review in two forms:

1. the original English manuscript and arXiv-compatible PDF;
2. a faithful Chinese translation and independently compiled Chinese PDF.

Translation is a presentation layer. It does not alter the scientific evidence,
citations, figures, review blueprint, or research plan.

## Mainline flow

```text
REVIEW_CONTENT_PACKAGE.json
  -> deterministic English LaTeX build
  -> block-level scientific Chinese translation
  -> deterministic structural validation
  -> low-cost semantic audit
  -> selective stronger-model repair
  -> Chinese LaTeX build with XeLaTeX/ctex
  -> English PDF + Chinese PDF + two source archives
```

The user-facing `run_review_harness.py` enables both PDFs by default. Use
`--no-chinese-publication` to keep only English, or
`--no-latex-publication` to skip both publication outputs.

## Translation policy

- Source Markdown is split into reversible blocks; heading levels and order are
  preserved.
- `[REF:...]` markers, mathematical expressions, URLs, exact numbers, ranges,
  and units are protected before any model call and restored byte-for-byte.
- Scientific terms without a clearer Chinese equivalent may remain in English.
  Examples include BIC, quasi-BIC, Q-factor, FOM, Fano, Mie, CMOS, model names,
  chemical formulae, and units.
- The default model is `c2_model` (`qwen3.5-flash`). Failed blocks retry with
  `c_model`; persistent high-risk semantic defects use `b_minus_model`.
- The semantic auditor is conservative: minor stylistic issues become warnings;
  citation, number, meaning, or logical reversals fail closed.
- A translation cache prevents repeated payment for already validated blocks.

## Artifacts

```text
publication/
  latex/
    main.pdf
    arxiv-source.zip
    LATEX_BUILD_REPORT.json
  translation_zh/
    FINAL_REVIEW_ZH.md
    PUBLICATION_METADATA_ZH.json
    TRANSLATION_REPORT.json
    TRANSLATION_CACHE.json
    TRANSLATION_COST.json
  latex_zh/
    main.pdf
    arxiv-source.zip
    LATEX_BUILD_REPORT.json
```

`REVIEW_CONTENT_PACKAGE.json`, `HARNESS_COST.json`, `HARNESS_STATE.json`, and
the observability timeline record the Chinese stages and their real token,
cost, time, and failure status.

## Quality gates

The Chinese PDF is emitted only when:

- all translation blocks pass deterministic checks;
- no unresolved major semantic-audit issue remains;
- citation markers and numeric tokens are unchanged;
- XeLaTeX compilation succeeds;
- the PDF has extractable text, no unresolved citations, no mojibake, and no
  leaked HTML or internal reference markers.

Author names and bibliographic references remain in their original language.
Draft metadata still produces `compiled_awaiting_metadata`; the renderer does
not claim submission readiness until real author metadata is supplied.

## Operator commands

Full bilingual publication from an existing content package:

```powershell
py -3.11 scripts/run_bilingual_publication.py `
  --content-package "outputs/REVIEW_CONTENT_PACKAGE.json" `
  --output-dir "outputs/publication/bilingual"
```

Translation only:

```powershell
py -3.11 scripts/run_chinese_translation.py `
  --source "outputs/FINAL_REVIEW_EN.md" `
  --output-dir "outputs/publication/translation_zh"
```

The default one-yuan translation budget is deliberately conservative. A
typical review in the current acceptance set used far less than one yuan, but
the hard budget prevents an uncontrolled retry loop. The default global
Harness admission ceiling is 49 CNY, one yuan above the previous English-only
ceiling, so adding the bilingual deliverable does not silently reduce research
or authoring budgets.
