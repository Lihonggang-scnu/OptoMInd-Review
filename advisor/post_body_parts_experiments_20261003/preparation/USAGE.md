# Post-BODY staged runner

Canonical entry point:

`F:\OptoMind-Review-2\outputs\serial_parts_preflight_20261002\worktree\scripts\upgrade3\post_body_runner.py`

The default is a zero-call preview. It derives the repository root from the script location, reads the BODY and context, and writes the exact stage messages under `PREVIEW/`.

```powershell
python -X utf8 scripts/upgrade3/post_body_runner.py `
  --mode preview --stage introduction `
  --draft <BODY.md> --context <CONTEXT.json> --out-dir <preview-root> `
  --prior-parts-mode full --language zh
```

Use `--prior-parts-mode none` to omit generated preceding parts from the stage payload. The runtime still constructs the same BODY, context, and responsibility inputs. `--language en` is passed through to the production message builder. `context.chapter_roles`, when present, is passed as the explicit runtime role list.

Live execution is explicit and uses the existing Qwen direct client and ledger:

```powershell
python -X utf8 scripts/upgrade3/post_body_runner.py `
  --mode live --stop-after-stage conception `
  --draft <BODY.md> --context <CONTEXT.json> --out-dir <run-root> `
  --key-file <key-file> --ledger <budget.sqlite>
```

Repeat the same command with the next stop stage, or omit `--stop-after-stage` for a complete run after review. Cache reuse is enabled by default. A response is reusable only when the exact messages hash and model/output parameters match; changed inputs create a new cache key and only the affected stages call the provider. `--no-reuse-cache` is available for deliberate invalidation.

Each attempt is isolated under `attempts/attempt-NNN`; request bindings are in `requests/`, cached responses in `cache/`, and direct-client raw responses in `raw_responses/`. Responses and bindings are persisted before parser validation so a parser repair can replay the same response without a new call. A stopped attempt reports `awaiting_review`; quality fields remain manual `pending_review` and are never marked as passed automatically.

Zero-call verification:

```powershell
python -X utf8 -m pytest -q tests/upgrade3/test_post_body_runner.py tests/upgrade3/test_serial_manuscript_parts.py -k "runner or prior_parts_mode or exact_replay_binding or conception_boundary_scalar or card_full_strict_replacement or condition_preserving"
```
