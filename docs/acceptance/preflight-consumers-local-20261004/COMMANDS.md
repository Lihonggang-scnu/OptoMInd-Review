# Reproduction and review commands

All commands below are local. The offline commands do not call a provider. The real probe commands were already authorized and executed; rerunning the final command would incur provider cost.

## Focused consumer checks

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
python -m pytest -q -p no:cacheprovider tests/upgrade3/test_body_preflight_f1_arrangement_cache_consumer.py tests/upgrade3/test_progressive_review_plan_tool_consumer_recovery.py tests/upgrade3/test_preflight_material_recovery_context.py
```

Observed: `50 passed`.

## First-stop offline controls

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
$env:PYTHONPATH=(Get-Location).Path
python docs/workorders/body-chain-20261003/records/preflight_consumers/run_offline_controls.py
```

Observed: `164 passed`; socket connection was denied by the control runner.

## Actual bounded probe

The paid calls were run only after preflight and approval, using the shared ledger and ordinary key-file client. The key contents are intentionally absent from this package.

```powershell
python runs/real_probe/probe.py preflight
python runs/real_probe/probe.py f1
python runs/real_probe/probe.py f3
```

The public evidence reports the actual three-call ledger delta and does not recommend rerunning it as a public documentation step.
