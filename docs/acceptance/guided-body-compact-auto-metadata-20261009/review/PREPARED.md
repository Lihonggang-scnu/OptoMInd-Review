# Compact automatic-metadata Max-guide → Plus trial: prepared checkpoint

Status: **READY_FOR_ROOT_PAID_APPROVAL**.  No paid provider call has been made from this run root.

## Frozen identity

- Run root: `F:\OptoMind-Review-2\outputs\guided_body_compact_auto_metadata_20261009_12cny`
- Detached worktree HEAD: `737ef95aff9f6ffb8061427489ca7e52fade5a5b`
- The production guide, prompts, runtime, and planning files are unchanged.  The wrapper is an external run artifact.
- Guide input SHA-256: `1a8624373328c7011a920c72affd165104d7304eb7c5e9fb1b3d4fb2efc60591`
- Run config SHA-256: `56ed432b80eca36b9d78d55b07888c6b9db192bef3ebf42d3f63c5d83ad166c9`
- Tokenizer SHA-256: `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`
- Wrapper SHA-256: `e0a1686a623be2cd6b9261d4270f14670b4173f51a37eded85aa3a309eb1103f`

The wrapper accepts only `--plus-only`, rejects `--allow-max`, and requires the original manifest, tokenizer, guide, config, and source commit.  It narrows runtime reservations to the frozen campaign cap while leaving the ledger's stored 60 CNY limit untouched.  It validates writer and metadata profiles separately: writer is qwen3.5-plus with thinking 16384 and max output 49152; automatic metadata is qwen3.5-plus, thinking false, budget 0, max output at most 2048.  No `--metadata-declarations` are supplied.

## Budget gate

- Stored ledger limit: 60 CNY; fixed S0: 18.3609232 CNY.
- Launch snapshot S1 settled: 32.4145152 CNY; S1 open physical spend: 0 CNY.
- New allowance: 12 CNY.
- Effective campaign cap: `min(58.3609232, 32.4145152 + 12) = 44.4145152` CNY.
- Preparation read found 39 settled rows and no reserved/uncertain rows.  The campaign snapshot records the baseline identity and refuses changed baseline rows, open physical reservations, cap overrun, or a reset of S1 on resume.

The campaign snapshot is `CAMPAIGN_BUDGET_START.json`.  Its ledger and key paths are intentionally not reproduced here.

## Offline checks

- Wrapper `--self-test`: passed, 0 network calls.  It covers atomic over-cap blocking, stored-limit/S0 preservation on restart, non-Plus rejection before provider creation, metadata low-cost Plus acceptance, persistent campaign recovery, and open-reservation blocking.
- Wrapper `--help`: passed.
- Explicit negative CLI checks: `--allow-max` rejected with `campaign_disallows_allow_max`; missing `--plus-only` rejected with `campaign_requires_plus_only`.
- Python compilation of the wrapper: passed.
- The campaign snapshot was revalidated after the final wrapper hash was recorded.

## Actual free preview

The latest live-source preview is run `20261009T142705Z-a8be167b45` under `PREVIEW/runs/`.  It completed without provider dispatch:

- `model_calls=0`, `client_invocations=0`, `paid_dispatch_count=0`, known cost 0 CNY.
- All seven chapter IDs were accepted in the frozen order; preview intentionally plans the first author stage and stops before an unknown model response.
- Ch1 measured input: 379,810 tokenizer tokens; reserved estimate 433,580; context limit 934,464; estimated maximum request cost 3.307184 CNY; fits.
- Ch1 final wire preview: qwen3.5-plus, `max_completion_tokens=65536`, `enable_thinking=true`, `thinking_budget=16384`, stream with usage included.
- The preview saved the final messages plan and request hash in its run directory and left the shared ledger unchanged.

## Material identity audit

The free compile audit distinguishes the 221 canonical source-identity navigation pool from the 194-source semantic supply union.  It found 95 structured tasks, 20 tool materials, 3 aliases, and these chapter task-bound semantic source counts: Ch1 55, Ch2 32, Ch3 23, Ch4 23, Ch5 36, Ch6 28, Ch7 46; union 194.  Ch5's material list has one extra guide-supplied item beyond its task-bound count.  The 221 pool must not be reported as 221 sources supplied to the author.

## Root decision

Prepared artifacts and checks are complete.  Wait for root's explicit paid-start approval before invoking `--run`; after approval, every physical request must retain its messages, response, usage, and effective request, and any unresolved automatic metadata recovery or budget state must stop the run without a manual declaration or paid retry.
