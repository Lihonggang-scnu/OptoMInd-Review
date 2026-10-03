# WO-02 code acceptance audit

- Audited checkout: `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree`
- HEAD: `5a8549200e5ee9e7be1992ca0cea19e1b7125487`
- Baseline: `ef1af12a783a5fb8bfaf05f6143dbb1a5fea69a6`
- Scope: `directed_reading.py`, `progressive_review_plan.py`, the practical adapter/store/retry path, and the changed WO-02 record.
- Method: read-only source inspection plus a bounded injected-client probe. No credentials, paid calls, production edits, test edits, or broad test rerun.

## Verdict

`CONCRETE_ISSUE`: the practical reader's provider failure can trigger an additional paid reader call in a later scientific retrieval round, bypassing the retrieval engine's configured provider-retry policy.

## Reproduction boundary

The exact offline probe is [probe_provider_failure_rounds.py](F:/OptoMind-Review-2/outputs/cloud_body02_real_acceptance_20261003/code_audit/probe_provider_failure_rounds.py). It uses three explicit round specs (`direction-1`, `direction-2`, `direction-3`) and no key, socket, Qwen client, paper snapshot, or production output. Its `actual_adapter_closure` case runs the current `make_retrieval_loop_runner` with the default `LoopConfig.max_provider_retries == 1`, an injected directed reader that always returns a failed row with no material, and a paper `P1`. Observed:

```text
configured max_provider_retries: 1
boundary calls: 2 (round 1, round 2)
round 1: external status unmet; nested reader result status failed
round 2: external status unmet; nested reader result status failed
provider_retry journal entries: 0
```

The same probe's direct-engine control cases show the policy distinction: returning ordinary `unmet` makes three scientific-round calls with either `max_provider_retries=0` or `1`; returning `failed` makes one same-round call with `0` and two same-round provider-retry calls with `1`, with no scientific-round advance. This isolates the control-flow contract at the Qwen boundary. A transport, key, quota, or other provider failure has the same routing if it reaches the adapter.

## Why this is a live-path blocker

`progressive_review_plan.py:7284-7297` catches the reader exception as a row with `status: "failed"`, but the aggregate result returns only `status: "unmet"` when no useful material exists. The retrieval closure at `progressive_review_plan.py:7069-7087` likewise returns ordinary `unmet` and does not propagate `provider_failed` or `status: "failed"`. `planning_retrieval_loop.py:746-758` only enters its bounded provider-retry path for `provider_failed`, `failed`, or `provider_error`; ordinary `unmet` advances into the next scientific round. The exact adapter probe reaches two paid-reader boundary attempts in different rounds despite the configured provider retry being one; an exhausted provider retry cannot prevent this because it is never entered.

The direct reader also reports failed paper IDs in `consumed_paper_ids` (`progressive_review_plan.py:7293-7294`), and the outer closure updates the shared ledger from that list (`progressive_review_plan.py:7053-7055`). This is an attempted-paper accounting behavior and was recorded here for traceability; it is not treated as a separate blocker because the existing shared deep-read budget is defined over consumed/attempted unique papers rather than retained useful material.

## Executable fix boundary for the owner

Preserve the failure artifact and attempted-paper accounting, but propagate a provider failure from the direct-reader result to the retrieval closure (for example, `provider_failed: true` or `status: "failed"`) so `run_retrieval_loop` stays in its bounded provider-retry path instead of opening a new scientific round. The owner should verify with the probe that a persistent failure produces only the configured same-round provider attempts (one with retry disabled, two with retry set to one) and no later scientific round; a successful explicit retry remains bound to the same task and raw-response directory.

## Other scoped checks

- Actual Qwen output shape is compatible: `QwenDirectClient` supplies the JSON content string, and `decode_practical_json` accepts that shape.
- Useful partial material is retained and classified as partial; bare/unresolved rows do not satisfy the requested question.
- Explicit retry preserves prior raw artifacts and binds the retry through task identity in `INPUT.json`; same-paper distinct task signatures remain separate.
- A conditional robustness gap remains at `progressive_review_plan.py:7235`: admission scope reads the card/planning scope but ignores a candidate-root `material_scope`. Normal generated cards carry the scope in the card/planning fields, so this was not treated as the live decision blocker.
- No source, test, or Git files were changed by this audit.
