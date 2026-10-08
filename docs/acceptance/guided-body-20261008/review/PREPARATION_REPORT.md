# Guided BODY preparation report

Recorded 2026-10-08T07:12:46.046517+00:00. This preparation used the detached worktree at `F:\OptoMind-Review-2\outputs\guided_body_trial_20261008\worktree` locked to `e63b12282d2269ca146a53266a47d06f2309a47e`. The worktree was clean before the live A launch. No production source was edited. The following A-live text is a 2026-10-08T07:12:46Z historical preparation snapshot; it predates the live B run. The final supervised state is recorded in the final section below.

## Offline verification

- Dedicated guided tests: `60 passed` in `tests/upgrade3/test_guided_body_contracts.py`, `test_guided_body_writer.py`, and `test_guided_body_cli.py`.
- Compile check: `python -X utf8 -m compileall -q scripts/upgrade3 optomind_research/runtime/upgrade3` exited 0.
- `git diff --check` exited 0.
- Handoff and guide copies are under `F:\OptoMind-Review-2\outputs\guided_body_trial_20261008\inputs`; the sealed manifest is copied unchanged.

## Preview evidence

A and B were previewed with the official `scripts/upgrade3/guided_body_writer.py`, the prior sealed manifest, Qwen tokenizer, and `config/guided_body_writer/plus_first.json`. Both had zero model calls and zero paid dispatches.

- A: guide SHA `18d00e4f5c5785332496fc82a2d79464a9d7ffb286febb9f76b47e3372122abb`; measured 381358 tokens; reserved input 435313; estimated maximum 3.314116 CNY; fits the 934464 input allowance.
- B: guide SHA `2b119747742b147b57b3a9ebfb64ec48ed6e254b590106074dd2b1d427e7cc9b`; measured 382832 tokens; reserved input 436964; estimated maximum 3.320720 CNY; fits the 934464 input allowance.
- Both preview user messages have exactly the four top-level fields `manuscript_guide`, `chapter_assignment`, `materials`, and `accepted_body_markdown`. Recursive task/unit/completion-key count is zero; first accepted prefix is empty; each materials projection has 1309 evidence atoms, 55 source identities, 3 tool materials, and 221 source-navigation records. No root review material appears.
- A preview: `F:\OptoMind-Review-2\outputs\guided_body_trial_20261008\A\CLI_RUN.json`; B preview: `F:\OptoMind-Review-2\outputs\guided_body_trial_20261008\B\CLI_RUN.json`.

## Historical A-live snapshot (2026-10-08T07:12:46Z)

At this earlier snapshot, root-supervised official A was running under PID `42288` and parent exec session `61355`; B had not yet been run. The then-current official run was `20261008T071030Z-0dd8d38957`, stage `author_001`. The exact command, source hashes, ledger marker, precheck, and that historical reservation are retained in the execution-state history. The final stop state is recorded in the final supervised execution section below.

The existing round-two ledger is `F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`. It was reused in place; cap is 60 CNY and no new ledger was created. At the state snapshot, settled was `8.3119972`, current reserved/held was `2.5906880`, and the current reservation was `2.590688`. B was gated at this historical snapshot until root read A. The final B result is recorded below.

## Inputs and configuration

- Locked commit: `e63b12282d2269ca146a53266a47d06f2309a47e`.
- Input hash: `94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa`; book SHA: `3662038f6f241a48979bc8f9e30c8382776def734fcaa4cdddb82858d6cb6322`.
- Source manifest SHA: `37242cc51bc9b6b96a7e92543a6092c17bfdbd0d8f8ab068a2e3d9c1e3db58e7`.
- A/B use Plus, 16384 thinking, 49152 answer, stream true, 1800/3600 second timeouts; no Max, retry, task coverage map, or manual scientific review input.

## Final supervised execution state (2026-10-08)

The root-launched A session (`61355`) completed all seven chapters in seven paid calls at 6.750730 CNY. Its selected result remains under `A\runs\20261008T071030Z-0dd8d38957\FULL_BODY_RESULT.json`.

The root-launched B session (`83704`) ended with `response_invalid` after six paid calls at 4.971964 CNY. Ch1–Ch5 are complete; the Ch6 response is preserved with transport complete, provider `stop`, and usage, but the official validator reported `guided_metadata_missing` and `guided_complete_missing_or_invalid`; Ch7 was not called. No retry was made.

The shared ledger is unchanged except for these settled calls: 24 rows, cap 60.0 CNY, settled 18.3609232 CNY, reserved/held/uncertain 0, remaining 41.6390768 CNY. The total increment from the preparation baseline is 11.722694 CNY.

`A_B_MESSAGE_PREFIX_WIRE_CHECK.json` records the field and prefix audit. A: 7/7; B: 6/6. Every audited user payload had exactly `manuscript_guide`, `chapter_assignment`, `materials`, and `accepted_body_markdown`; all official prior-body prefixes matched byte-for-byte under the production double-newline join; recursive old task/unit/completion keys and root-review terms were zero. Effective wire fields were qwen3.5-plus, thinking 16384, answer 49152, max completion 65536, stream true, inactivity 1800 seconds, overall 3600 seconds.

Root audit correction: the zero legacy task/unit/completion count does not cover `outline_action`. `ROOT_ACTUAL_REQUEST_AUDIT.json` records `supplementary_outline_action_leak_found: true`; the actual requests retain that supplemental directive field even though no legacy paragraph task table or task IDs were detected.

`EXECUTION_STATE.json` is the authoritative supervised state record. No further paid calls are authorized in this record.
