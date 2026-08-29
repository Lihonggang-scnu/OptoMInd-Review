# Three E2E artifact index

This index covers the three historical end-to-end runs selected for the public
replay. The complete original trees remain under
`outputs/research_harness_e2e/`; this file intentionally uses repository-
relative paths only.

| No. | Human name | Original run directory | Portable publication layer | English PDF | Chinese PDF | Raw size / files |
| --- | --- | --- | --- | --- | --- | --- |
| 01 | Optical diffractive neural networks | `outputs/research_harness_e2e/rhr_optical_diffractive_neural_networks_20260828_v2b/` | `artifacts/e2e/01-optical-diffractive-neural-networks/` | `publication/latex/main.pdf` · 37 pages | `publication/latex_zh/main.pdf` · 35 pages | 823.41 MiB / 1,583 |
| 02 | Metasurface holography | `outputs/research_harness_e2e/rhr_metasurface_holography_20260828_v1/` | `artifacts/e2e/02-metasurface-holography/` | `publication/latex/main.pdf` · 36 pages | `publication/latex_zh/main.pdf` · 34 pages | 899.97 MiB / 2,364 |
| 03 | Scalable photonic computing | `outputs/research_harness_e2e/rhr_photonic_computing_20260829_v1/` | `artifacts/e2e/03-scalable-photonic-computing/` | `publication/latex/main.pdf` · 44 pages | `publication/latex_zh/main.pdf` · 42 pages | 698.73 MiB / 1,361 |

The exact byte-level inventory and SHA-256 values are generated at
`artifacts/e2e-full-manifest.json`. It covers every file in the three original
trees without copying those trees into the public source layer.

## Portable layer contents

Each `artifacts/e2e/<run>/` directory contains the generated publication
package (`publication/`), the publication-mainline decisions and reports
(`publication_mainline/`), the final visual audit/package (`visual_editor/`),
and a sanitized run summary. This includes, where the run produced them:

- English and Chinese PDF files;
- `.tex`, normalized manuscript, bibliography, and source-package files;
- LaTeX build and publication-integrity audits;
- final citation/metadata, style-governance, and manuscript-completion reports;
- visual contract, visual package, cost, and final-audit reports;
- a delivery summary with status, page counts, and recorded degraded checks.

The complete raw trees additionally retain all section candidates, evidence
ledgers, full-text acquisition records, cache synchronization receipts,
runtime events, and recovery checkpoints. Nothing in the preparation pass
removes or relocates those originals.

## Delivery notes

All three runs have an English PDF and an English publication audit. The run
directories record a degraded delivery state only because the Chinese
translation audit has warnings; this does not invalidate the English PDF. The
third run completed without a recorded runtime error, while the first two each
record one recoverable/reconciled error in their harness receipts.

The static replay in `replay/index.html` is built from a deliberately reduced,
path-sanitized event stream. It is not a replacement for the raw run records.
Use an HTTP server when opening it; do not open the HTML with `file://`.
