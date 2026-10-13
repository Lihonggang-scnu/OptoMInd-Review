# Local Docling batch and directed-reading handoff — 2026-10-13

This change provides a local PDF material route. The optical review end-to-end run remains paused. No model API was called in these parser experiments; Mistral and Mathpix were not enabled.

## Adopted route

1. Native XML remains preferred when available. For PDF batches, `scripts/upgrade3/parse_docling_batch.py --mode basic` keeps one Docling converter resident across the batch. It saves native document JSON and a per-PDF receipt.
2. `scripts/upgrade3/build_local_materials.py --material-parser-mode docling_basic --docling-batch-index ...` imports the verified batch result into ordinary snapshots. The PDF body comes from Docling reading order, with tables, captions and source-bound region handles. It is not the former GROBID/fitz body with only supplementary Docling assets.
3. Ordinary reading and A/B creation use these snapshots immediately. Original PDF, native JSON and the page/bounding-box index remain available independently of A/B and deep reading.
4. When directed reading selects a paper, `--local-formula-config` invokes local CodeFormulaV2 before preparing the deep-reader request. The derived formula material is bound to the original PDF and region index and enters the practical reading messages. The original snapshot is not rewritten. Tables already enter through the basic local table recognizer; this hook enhances formulas, not every table or figure.

Configuration, commands and the distinction from the historical regional API route are maintained in [local_materials.md](../../local_materials.md). Do not enable the old regional vision provider for this local-only route.

## Measurements and content review

Hardware: RTX 3080 10 GB; experiments constrained whole-card usage below 8 GiB. These are local inference measurements, not complete review-generation times.

| Run | Observed result |
| --- | --- |
| Basic, one resident process, batch 8 / threads 8 | 55 distinct PDFs, 1,038 pages, all successful in 106.505 seconds; peak 4,186 MiB |
| Basic, three processes | Only 18 of 55 PDFs completed; two Windows native crashes. Rejected as default despite faster small-sample results |
| Local formula enhancement, batch 32 | 34 formulas across five PDFs in 42.746 seconds; peak 5,895 MiB |
| Formula batch 16 versus 32 | All 34 outputs identical after whitespace normalization |

The 55-PDF count includes supplementary/version files; it is not a count of unique papers. Basic parsing timing excludes later CPU snapshot/crop projection. The formal CLI and consumer short-chain checks are recorded separately below.

Root visually checked original formula and comparison-table pages. Fractions, roots, subscripts and the main relationships survived; dense tables retained row/column structure and numerical content. Some micro-unit characters became `m`, and some formula labels were omitted. Original page/region access is retained for these imperfections. Figure positions/captions are available; semantic interpretation of figures is not claimed. Scanned papers need an explicit OCR route, and unstructured bibliography text is not a resolved identity catalog.

The local LightOnOCR-3 trial was not selected for batch production: this Windows Transformers backend took 360.65 seconds without completing one page. A single formula crop completed in 11.68 seconds. No claim is made about accelerated Linux backends not tested here.

## Local evidence

On the test machine, all original parser outputs, source images and run receipts remain under:

`F:\OptoMind-Review-2\outputs\pdf_bulk_route_20261013`

- `docling_speed/`: full-PDF and formula measurements, successful and failed runs.
- `integration_audit/`: formal CLI, snapshot, A/B and directed-reading message checks.
- `ROOT_QUALITY_REVIEW.md`: root's page-by-page content observations.
- `external_trials/TRIAL_REPORT.md`: bounded alternative local-model trial.

Original PDFs, model weights and local databases are not included in this public change. Existing end-to-end results and budgets are unchanged.

## Formal integration check

The new formal basic CLI converted two real PDFs successfully in 9.0 seconds (peak 3,923 MiB). The subsequent formal snapshot import took 1.923 seconds including its process startup. The selected-paper hook then enhanced six formulas in 14.063 seconds. Reusing the hook took 0.063 seconds, returned the same sidecar and performed no new inference. The ordinary snapshot remained byte-identical. The formulas were present in the prepared practical-reader request; no paid reader call was made for this interface check.

The focused integration suite passed 96 checks with four existing optional-fixture skips before the final path/status adjustments. This is bounded parser/consumer verification, not a new full review-generation run. Final source and verification receipt remain in the local handoff directory.
