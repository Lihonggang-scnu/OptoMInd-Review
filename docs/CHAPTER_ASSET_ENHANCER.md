# Chapter Asset Enhancer (v2)

## R67 production additions (2026-08-20)

The enhancer now has an optional representative-application side path. It is
designed to make a chapter read like a literature review rather than a purely
abstract taxonomy: a suitable method block may receive one concrete
implementation, device, dataset, or historical demonstration with its reported
condition/result and a source boundary.

The path is deliberately additive and fail-open:

- the explanatory citation planner supplies application targets first;
- local central metadata/abstract search is attempted before Semantic Scholar;
- each target requests at most four local candidates and at most four S2
  metadata candidates, with no full-text download;
- one batched low-cost application writer call serves up to three targets per
  chapter;
- missing examples, malformed writer output, or invented numeric detail skip
  only that example and never discard the core enhanced chapter;
- application records are merged into the top-level explanatory ledger
  `records` with `benefit_type=representative_application`, while the nested
  `representative_applications` object remains an audit view.

The mainline adapter passes these settings explicitly and includes them in the
enhancement reuse fingerprint. The user-facing defaults can be disabled with
`--no-publication-mainline-representative-applications` or
`--no-publication-mainline-s2-metadata-fallback`.

Standalone enhancement layer for an accepted section chapter. It consumes the
accepted `input_packet.json` plus the existing `SECTION_DRAFT_EN.md` and writes
an enhanced English chapter without rerunning retrieval, claim review,
evidence binding, or the existing `SectionWriter`.

## Sequence

1. **Argument planner** - establishes `chapter_thesis`, `reader_takeaway`,
   `argument_sequence`, `terminology_rows`, and `explanation_block_rows` from a
   compact claim/evidence-handle inventory, with section responsibility as the
   first priority (user question/purpose, must-cover, boundaries, handoffs).
2. **Explanation block writer** - writes one coherent paragraph per block,
   grounded in the block's assigned claims and exact evidence spans.
3. **Legacy gap auditor + patch writer** - compares the new blocks with the old
   draft, finds only scientifically useful content genuinely lost, and patches
   the affected blocks using existing evidence handles.
4. **Block scientific reviewer + bounded reviser** - one `c2_model` review of
   the final core prose (after legacy patches) flags material contradictions,
   unsupported new mechanisms, and material overclaims; only blocking rows
   trigger at most one revision per affected block. Reviewer/reviser failures
   fail open and keep the generated blocks.
5. **Explanatory citation side path** - targets the final revised prose:
   extracts blocks/sentences that benefit from definition,
   mechanism/background, or representative-application references, searches
   local metadata/abstracts first and optionally Semantic Scholar metadata
   second, and writes a separate `EXPLANATORY_CITATION_LEDGER.json`. No
   OA/full-text download or chunk processing is performed.

The old draft is a semantic map and omission checklist, never an evidence
source. All scientific prose comes from accepted claims and packet evidence.

## Semantic evidence handles

Local code creates handles such as `E01_PINN_LOSS_MECHANISM` and
`C01_...` for claims. Model output refers only to handles. The local renderer:

- resolves handles to real `[REF:paper_id]` markers;
- deduplicates markers per block;
- never accepts unknown handles or raw citation markers;
- records claim-to-paragraph and block-to-claim maps.

## Explanatory citations

Explanatory citations are a separate trust plane from core evidence:

- every ledger record is tagged `role=explanatory_context` and
  `permission=background_explanation_only`;
- only metadata is stored (title, authors, year, DOI, paper id, abstract,
  venue/url);
- local metadata/abstract search runs first; Semantic Scholar metadata search
  is an optional fallback enabled with `--s2-search`;
- records are deduplicated by stable paper identity across blocks and against
  core references;
- identity normalization prefers normalized DOI, then Semantic Scholar ID,
  then internal paper/source ID, then normalized title;
- candidates are scored locally against the query and provider relevance; each
  need keeps one strong source and at most one close, distinct second source;
- scoring weights technical tokens, acronyms, numeric labels, and exact
  multiword phrases while downweighting generic scholarly words; a local
  candidate below the ~0.30 sufficiency threshold does not suppress S2, and
  local + S2 candidates are merged and reranked together;
- an adaptive chapter-level selection audit explains the selected score floor,
  raw/eligible/selected unique counts, and stop reason against the soft 10-20
  range, route counts (local-only / local+S2 / S2-only), without padding weak
  pools or mechanically capping high-quality needs;
- one c2_model semantic reranker call per chapter scores the shortlisted
  candidates on explanatory helpfulness (0-100), removes only clear topic
  mismatches (<=29), and ranks the rest by semantic helpfulness with
  deterministic score as secondary; reranker failure or an incomplete score
  response fails open to deterministic selection and is recorded in the
  selection audit;
- chapter-level 10-20 references and per-block 2-5 references are soft
  expectations only, never quotas;
- explanatory markers are placed immediately after the exact target sentence;
  if the sentence cannot be located, a block-level marker is retained with a
  diagnostic;
- sentences are locally split into stable handles such as `B01-S01`; Qwen
  returns only the handle, benefit type, query, and note;
- legacy patching and block review run before explanatory planning, so
  sentence handles are generated from the final core prose and no post-citation
  reconciliation is needed;
- search/model failure produces an empty explanatory ledger with diagnostics
  and never blocks the core enhanced output.

The CLI local metadata callback reuses one existing library connection for the
run and expands a Qwen query into a small bounded set of distinctive terms and
2-4 word phrases; it never creates a database.

Semantic Scholar results are normalized from backend field names
(`abstract_or_snippet`, `journal_or_venue`, `source_url`, `url_or_doi`) before
scoring and deduplication.

## CLI

```powershell
py -3.11 scripts/run_chapter_asset_enhancer.py `
  --packet-path outputs/.../input_packet.json `
  --old-draft outputs/.../SECTION_DRAFT_EN.md `
  --output-dir outputs/.../enhanced `
  --live
```

`--live` is required for real Qwen calls. Without it the CLI is a dry run.
Existing output directories are refused unless `--allow-overwrite` is passed.
Default tiers: planner/block/patch `c_model`, legacy auditor and contract
repair `c2_model`, explanatory planner and block scientific reviewer `c2_model`,
block review reviser `c_model`.

Optional explanatory controls:

```powershell
--local-metadata-store path/to/literature_resources.sqlite
--s2-search
--explanatory-max-results 10
--disable-explanatory-citations
```

The CLI never creates a local metadata database; an explicitly supplied store
must already exist.

## Artifacts

- `OLD_CHAPTER.md`
- `ENHANCED_CHAPTER.md`
- `CHAPTER_ARGUMENT_PLAN.json`
- `TERMINOLOGY_REGISTRY.json`
- `EXPLANATION_BLOCKS.json`
- `CLAIM_TO_PARAGRAPH_MAP.json`
- `LEGACY_GAP_AUDIT.json`
- `EXPLANATORY_CITATION_LEDGER.json`
- `BLOCK_SCIENTIFIC_REVIEW.json`
- `ENHANCEMENT_REPORT.json`

Persisted JSON uses relative artifact labels rather than machine-specific
absolute paths.

## Failure policy

Hard integrity failures - empty output, unknown evidence handles, raw citation
markers from the model, unknown paper mappings, or unavailable core model
responses - cause fail-open: the downstream text is the original chapter with
an explicit `fail_open_original` status. Gap-audit or patch-model failures keep
the generated chapter and are recorded as diagnostics. Soft issues such as
length, style, and evidence-limited terminology are warnings only.

Planner and block-writer contract defects get one bounded contract-repair call
(`c2_model`). If the repair still violates the local contract, the pipeline
fails open to the original chapter.

Argument-planner initial output and planner contract-repair allow up to 8,000
output tokens; block-writer and other contract repairs use 5,000.
This is an output-completeness allowance, not a requested length: long,
evidence-rich chapters can otherwise truncate a valid planning table before
the closing JSON delimiter. Block writers and scientific review keep their
smaller task-specific budgets.

Legacy gap patches require at least one valid gap-specific evidence handle and
preserve the block's prior evidence handles by unioning, never replacing.

## Block scientific review

The block scientific reviewer runs once per chapter after block generation and
before explanatory citations. It returns rows with `flag_type`:

- `material_contradiction`
- `unsupported_new_mechanism`
- `material_overclaim`
- `advisory`

Only a clear `material_contradiction` / direction reversal can trigger one
targeted revision per affected block. `unsupported_new_mechanism`,
`material_overclaim`, and `advisory` rows are forced nonblocking locally and
preserved as downstream review comments with their suggested hedges. The
reviser must preserve the chapter viewpoint, coherent paragraph, and valid
evidence handles. Reviewer or reviser failures fail open: generated blocks are
kept, diagnostics are recorded, and status remains `enhanced`. All comments and
per-block revision outcomes are persisted in `BLOCK_SCIENTIFIC_REVIEW.json`.

This module is an enhancer, not the final scientific reviewer. Disputed
breadth, comparisons, inference, or mechanism interpretation pass downstream
for full-manuscript review.

Reviewer and reviser evidence scope is block/claim-bounded: all ledger evidence
whose claim belongs to the block's planned claims, plus the plan row's assigned
evidence handles and the block's currently used handles. This allows a reviser
to use a previously unused evidence packet for a claim already planned in the
block, while still rejecting unknown or cross-claim evidence.
