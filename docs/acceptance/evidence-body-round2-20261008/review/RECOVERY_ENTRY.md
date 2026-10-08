# Round-two recovery entry

This experiment is closed for paid execution. Do not add `--run` without a new approval.

- Root: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny`
- Worktree: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\worktree`
- Locked commit: `f1b78750fae6ff47dbc4d4694f88af3b53c8cf9c` (detached, clean)
- Shared ledger: `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`; cap 60 CNY; settled 6.638229200; held/reserved 0.000000000
- Input hash: `94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa`
- Manifest SHA256: `37242cc51bc9b6b96a7e92543a6092c17bfdbd0d8f8ab068a2e3d9c1e3db58e7`
- Config SHA256: `c80bd0af1d090c2b562aab541e7f3d2b2ae85a0f96f582d38d3f43aac4f27e08`
- Tokenizer SHA256: `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`

## Chosen preserved body

Root selected the unedited continuous result for quality review:

`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\packed_continuous\FULL_BODY.md`  
SHA256: `5f334c674ce54672b0f327e719bbbd3a7a4e6d26b243b02d73fbe99e5c174b51`

Its complete sealed result is `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\packed_continuous\FULL_BODY_RESULT.json`. The scoped route output remains available for comparison but is pending reader issues; technical patch acceptance is not a root quality acceptance.

## Offline inspection/recovery commands

```powershell
$exp = 'F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny'
$wt = Join-Path $exp 'worktree'
C:\Anaconda\python.exe -X utf8 (Join-Path $wt 'scripts\upgrade3\evidence_body_writer.py') --manifest (Join-Path $exp 'PREPARED_MANIFEST.json') --route scoped_revision --draft (Join-Path $exp 'packed_continuous\FULL_BODY_RESULT.json') --output (Join-Path $exp 'scoped_revision_offline_recheck') --config (Join-Path $wt 'config\evidence_body_writer\plus_first.json')
Get-Content (Join-Path $exp 'FINAL_EXECUTION_SUMMARY.json')
Get-Content (Join-Path $exp 'ROUND2_LEDGER_FINAL_STATS.json')
git -C $wt status --short --branch
git -C $wt rev-parse HEAD
```

The current route receipts, raw provider responses, request messages, usage records, and cache/signature provenance remain under `packed_continuous`, `dossier_author`, and `scoped_revision_live`. The compatible reuse source for scoped comparison is the continuous result above, with body SHA `5f334c674ce54672b0f327e719bbbd3a7a4e6d26b243b02d73fbe99e5c174b51`; no historical output was charged again as a new writer call.
