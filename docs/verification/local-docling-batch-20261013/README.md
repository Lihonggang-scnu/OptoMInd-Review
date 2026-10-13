# Local Docling batch and directed-reading handoff — 2026-10-13

This change provides a local PDF material route. The optical review A/B path is restored at V7. No model API was called in these parser experiments; Mistral and Mathpix were not enabled.

## One canonical entry and portable profile

Use `scripts/upgrade3/parse_docling_batch.py` for both basic parsing and the
selected-formula mode. `BASIC_PARALLEL3_CONFIG.template.json` in this
verification directory is the complete checked-in three-process profile. Its
basic recommendation is `workers=3`, layout/table/OCR batch 4, accelerator
threads 4, `safe_parser=true`, native `parser_threads=1`, and a fresh child
after at most 20 PDFs, with `gpu_limit_mib=8192` for total-card usage. A config
that omits `workers` deliberately retains the historical one-process default;
do not infer three workers from an old config.

The tested machine command was:

```powershell
$repo = 'F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\worktree'
$pool = 'F:\OptoMind-Review-2\outputs\pdf_bulk_route_20261013\docling_speed'
$python = 'F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\parser_repair\venv_docling\Scripts\python.exe'
& $python -B -X utf8 "$repo\scripts\upgrade3\parse_docling_batch.py" `
  --mode basic `
  --manifest "$pool\LOCAL_PDF_MANIFEST_NORMALIZED.json" `
  --config "$repo\docs\verification\local-docling-batch-20261013\BASIC_PARALLEL3_CONFIG.template.json" `
  --output-root "$pool\official_parallel_full_55" `
  --resume --workers 3
```

For another checkout, change only the repository, Python environment, PDF
manifest and output root. The template's relative
`pipeline_options.artifacts_path` is adapted to this worktree's sibling
`parser_repair\docling_artifacts`; point it to the existing local Docling
model cache before running elsewhere. The profile is offline and contains no
keys or article text. Ordinary `docling_basic` snapshots defer region-image
crops while retaining the original PDF, page/bbox coordinates and region
index. Selected-paper enrichment uses the already cached local CodeFormulaV2
profile (batch 32, single process); it is not a full-PDF formula rerun and
never falls back to a paid provider.

## Adopted route

1. Native XML remains preferred when available. For PDF batches, `scripts/upgrade3/parse_docling_batch.py --mode basic` uses the explicit verified `workers=3`, b4/t4, single-native-parser-thread profile under the 8 GiB whole-card cap. Each child handles at most 20 PDFs, then resumes from verified per-PDF receipts in a fresh child. Historical configs without `workers` retain one resident process. It saves native document JSON and a per-PDF receipt.
2. `scripts/upgrade3/build_local_materials.py --material-parser-mode docling_basic --docling-batch-index ...` imports the verified batch result into ordinary snapshots. The PDF body comes from Docling reading order, with tables, captions and source-bound region handles. It is not the former GROBID/fitz body with only supplementary Docling assets.
3. Ordinary reading and A/B creation use these snapshots immediately. Original PDF, native JSON and the page/bounding-box index remain available independently of A/B and deep reading.
4. When directed reading selects a paper, `--local-formula-config` invokes local CodeFormulaV2 before preparing the deep-reader request. The derived formula material is bound to the original PDF and region index and enters the practical reading messages. The original snapshot is not rewritten. Tables already enter through the basic local table recognizer; this hook enhances formulas, not every table or figure.

Configuration, commands and the distinction from the historical regional API route are maintained in [local_materials.md](../../local_materials.md). Do not enable the old regional vision provider for this local-only route.

## Measurements and content review

### Manifest audit

The real local manifest `LOCAL_PDF_MANIFEST_NORMALIZED.json` contains 55 unique
PDFs and 1,038 source pages. The final `official_parallel_full_55/BATCH_RESULT.json`
contains 55 unique `success` records: all 55 have matching source hashes, full
page coverage, and both `document.json` and `run.json`; no manifest PDF remains
unparsed. This is a PDF-parser count and is independent of any A/B or directed
reading `pending` cards.

Hardware: RTX 3080 10 GB; experiments constrained whole-card usage below 8 GiB. These are local inference measurements, not complete review-generation times.

| Run | Observed result |
| --- | --- |
| Basic, one resident process, batch 8 / threads 8 | 55 distinct PDFs, 1,038 pages, all successful in 106.505 seconds; peak 4,186 MiB |
| Basic, three processes with parser_threads=1 | 55/55 successful; peak 7,075 MiB; zero native crashes; seven content comparisons equal to the b8 baseline |
| Formal production entry, explicit three-child b4/t4 profile | 55/55 newly converted in 68.812 seconds; peak 6,966 MiB; three children, one execution each; zero native crashes |
| Basic, default bounded single-thread children | 55/55 successful in 141.078 seconds; three fresh children (20/20/15); peak 3,692 MiB; seven content comparisons equal to the b8 baseline |
| Basic, three processes with the installed parser default | Only 18 of 55 PDFs completed; two Windows native crashes. Rejected despite faster small-sample results |
| Local formula enhancement, batch 32 | 34 formulas across five PDFs in 42.746 seconds; peak 5,895 MiB |
| Formula batch 16 versus 32 | All 34 outputs identical after whitespace normalization |

The 55-PDF count includes supplementary/version files; it is not a count of unique papers. Basic parsing timing excludes later CPU snapshot/crop projection. The formal CLI and consumer short-chain checks are recorded separately below.

Root visually checked original formula and comparison-table pages. Fractions, roots, subscripts and the main relationships survived; dense tables retained row/column structure and numerical content. Some micro-unit characters became `m`, and some formula labels were omitted. Original page/region access is retained for these imperfections. Figure positions/captions are available; semantic interpretation of figures is not claimed. Scanned papers need an explicit OCR route, and unstructured bibliography text is not a resolved identity catalog.

The local LightOnOCR-3 trial was not selected for batch production: this Windows Transformers backend took 360.65 seconds without completing one page. A single formula crop completed in 11.68 seconds. No claim is made about accelerated Linux backends not tested here.

The formal production comparison is retained at `F:\OptoMind-Review-2\outputs\pdf_bulk_route_20261013\docling_speed\official_parallel_full_55_CONTENT_COMPARISON.json`: all 55 PDFs matched the existing b8 baseline on semantic content, pages, regions, formulas, tables, scientific text and recognized cells (55/55 for every check). Basic snapshot publication now defers eager image crops while retaining the PDF, page/bbox provenance and `REGION_INDEX.json`; `deep_layout` keeps the prior crop behavior, with a separate deferred cache fingerprint.

The verified three-child settings apply to the resident batch entry above. A
single-PDF on-demand Docling CLI configured elsewhere remains its own route and
does not automatically inherit the batch parser-thread override.

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

## Unattended recovery added during the optical run

The actual remaining batch of 38 PDFs exposed two native process failures. Windows WER first identified `docling_parse/pdf_parsers.cp310-win_amd64.pyd` with access violation `0xc0000005`, followed by `ntdll.dll` heap corruption `0xc0000374`. Whole-card GPU peaks were about 4 GiB. Both PDFs being processed at the crashes succeeded in a fresh process; they were not established as corrupt inputs.

The formal supervisor now automatically starts a fresh worker after a native crash, preserving and reusing every valid successful PDF receipt. Repeated crashes at the same PDF isolate that item; basic parsing gives it one local attempt with a single native parser thread. The effective fallback settings and original failures remain recorded. New successes or an isolated culprit reset the consecutive no-progress count; the overall deadline and no-progress ceiling remain bounded. Deadline, GPU-ceiling and operator stops are not treated as retryable native faults. Windows cleanup terminates only the owned worker process tree, including the virtual-environment launcher child.

The 38 PDFs ultimately all completed, reusing 23 successes in the final run and converting the last 15. The three local parsing invocations took 288.138 seconds in total, including the two failed native processes; no API fees were incurred. Targeted recovery checks passed 10 cases in 2.90 seconds. Local evidence and current E2E bindings live in `preparation/local_docling_resume_20261013/` under the optical acceptance root. Snapshot import and A/B continuation are subsequent stages, not part of this parsing timing.
