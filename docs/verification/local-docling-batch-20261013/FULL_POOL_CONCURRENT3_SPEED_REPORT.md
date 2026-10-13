# Full 55-PDF three-worker route

- status: `partial_or_failed`
- pages: `1038` across `55` PDFs
- profile: `workers=3`, `layout_batch_size=4`, `table_batch_size=4`, `num_threads=4`
- wall seconds: `121.731`
- confirmed complete pages: `343/1038`
- complete/unfinished PDFs: `18/37`
- worker crashes: `2`; returncodes: `[3221226505, 3221226356, 0]`
- full-pool throughput: `not reported` because the probe was incomplete; confirmed pages / probe wall: `2.817688` pages/s (diagnostic only)
- peak total GPU: `5756 MiB` / `8192 MiB`
- available completed outputs equal single-worker b8: `True` (`18/18` compared)
- unavailable outputs: `37` PDFs; they remain in the recovery receipt and were not treated as content mismatches

For each available output, the comparison covers pages, regions/coordinates, formulas, tables, scientific text, and recognized table cells. Missing outputs are retained in the recovery receipt and are excluded from the content comparison.
