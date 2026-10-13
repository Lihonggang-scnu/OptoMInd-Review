# Incomplete three-worker full-pool verification

This is a CPU-only audit of the bounded GPU probe; it does not retry failed workers.

- completed: `18/55`
- completed outputs equal single-worker b8 projections: `True`
- missing: `37`
- native worker return codes: `[3221226505, 3221226356]`
- peak total GPU recorded: `5756 MiB`

The stable full-pool route remains `pool_b8_t8_55`; its independent resume receipt is 55/55 skipped with zero reconversion.
