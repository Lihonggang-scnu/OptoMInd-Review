# CodeFormulaV2 bulk enrichment probe

Persistent local model over the same five converted PDFs (78 pages).

| batch | exit | wall seconds | formula batches | nonempty formulas | peak total GPU MiB |
|---:|---:|---:|---:|---:|---:|
| 16 | 0 | 45.527 | 6 | 34/34 | 5700 |
| 32 | 0 | 42.746 | 5 | 34/34 | 5895 |

Outputs retain formula text and each formula's page/bbox provenance while using placeholder image mode to avoid embedding page images. The parent sampled nvidia-smi every 0.5 s and enforced the 8192 MiB total-card limit.
