# Exact bounded commands

The local acceptance used the tested worktree SHA `8810c7bf38227f2987d7510937bdf9001ffd47ad`. The following two lines are historical local invocations recorded for provenance; `run_real_acceptance.py` is intentionally not uploaded and these commands are not directly runnable from the cloud docs pack.

```powershell
python <WO03_SOURCE_ROOT>/run_real_acceptance.py preflight
python <WO03_SOURCE_ROOT>/run_real_acceptance.py run --allow-paid
```

The offline selection was run with socket guards and the exact 15-file selection recorded in `code_audit/WO03_CODE_ACCEPTANCE.md`, excluding the two documented full fake-chain tests. It completed with `212 passed, 2 deselected` and no network socket. Compilation and `git diff --check` completed successfully.

The archive reproducer is offline and independent of production code:

```powershell
python retrieval/reproduce_selection.py
```
