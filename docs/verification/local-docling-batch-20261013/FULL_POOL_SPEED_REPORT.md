# Full local PDF pool speed

- route: `pool_b8_t8_55`
- input: `LOCAL_PDF_MANIFEST.json` → normalized 55 unique PDFs
- pages: `1038`
- status: `complete`
- successful/failed: `55/0`
- wall seconds: `106.505`
- throughput: `9.746021` pages/s
- peak total GPU from nvidia-smi: `4186 MiB` / `8192 MiB`

The resident worker keeps one Docling DocumentConverter and the offline local model cache alive for the complete manifest. Per-PDF document.json and content_summary.json retain source page coordinates and region indexes.
