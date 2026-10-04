# WO06 item 8: feedback arrangement completion gate

## Result

The independent feedback loop now consumes the existing arrangement validation
before writing. A nonempty `units` list with `needs_arrangement` or
`contract_failed` no longer dispatches the writer. The result and active pointer
report `status=partial`, `pending_arrangement=true`, the actual arrangement
status, and the existing validation details. Owner-updated plans, adopted source
content/identity, arrangement files and previous usable writer artifacts survive.

This changes only `run_feedback_loop` in the runtime progressive-plan module.
It does not change normal BODY ordering, writer defaults, prompts, case-chain
behavior, or the arrangement validator itself. This is an engineering acceptance
of this feedback seam, not evidence of improved scientific writing.

## Reviewed evidence and actual baseline

- Read `docs/acceptance/body05-real-20261004/README.md` and `ASTRA_ACCEPTANCE.md`:
  the real WO05 handoff was accepted with explicit quality limits; the real
  arrangement used one-to-one tasks, while split/merge was offline-only
- Read WO06 item 8 and F18 in the consolidated plan and WO00 record: the feedback
  gate was previously a static risk, separate from the normal BODY main path
- Production baseline: `6efc7a44615f0414109678c8c2ac7a040ee0ffc7`
- Documentation archive checkout: `8582bb698700937b0b89db65b26c8cfa958b210b`
- Historical private responses remain `LOCAL_ONLY`; none was accessed or replayed

## Fail-first reproduction

`test_body06_feedback_arrangement_gate.py` uses actual on-disk packets,
`run_feedback_loop`, the real owner-response/material-adoption checks, the CLI
`FeedbackLoop.arrangement_runner`, and the production arrangement parser and
validator. Only owner/model responses, the local token-count boundary and the
writer dispatch sentinel are controlled. It never constructs a real live
transport or reads a credential.

Two synthetic raw responses retain `UNIT_A`:

1. A selected supporting-study source P0002 is neither placed nor marked unused.
   The real validator returns `needs_arrangement`, `ok=false`
2. The response has a wrong chapter identity while retaining its otherwise
   usable unit/tasks. The real validator returns `contract_failed`, `ok=false`

Before the patch, both called the writer once and reported `updated`. Both
regression assertions failed at `writer_calls == 0`, rather than failing at the
fixture/validator setup. After the patch, both retain the owner-adopted P0002
content/identity and nonempty arrangement but make zero writer calls. An
identical retry reuses the pending result without repeating owner or arrangement
calls. `FEEDBACK_GATE_EVIDENCE.json` captures actual before/after raw fixture
shape, validator judgment, dispatch counts, output status and retained content.

## Compatibility and bounded cache behavior

- The production validator emits `arranged`, `needs_arrangement`, or
  `contract_failed`. The gate consumes these verdicts, including contradictory
  explicit failure/error/unplaced-source signals even when `ok=true`
- An omitted `validation` key keeps the legacy callback behavior. This is a
  compatibility boundary, not a claim that an unvalidated legacy arrangement is
  substantively complete. Existing `ok=true` or `status=arranged` success signals
  suffice without inventing new mandatory chapter/task fields
- Explicit empty, null or malformed validation is not success
- A previously completed cache containing an invalid arrangement is marked
  pending without launching callbacks or changing owner/arrangement/body bytes
- Unchanged pending work does not launch a repair loop. Changed execution input
  can reopen the existing bounded loop; an explicit `resume=false` remains
  available. Existing incomplete tool-action retry behavior is retained
- Original invalid arrangement verdicts remain pending through empty feedback,
  owner `no_change`, fulfilled action without owner revision, and legacy
  no-change cache fallback
- Cache-pointer correction checks that the active pointer is unchanged before
  writing, preserving the existing stale-active-artifact guard
- Valid and legacy-no-validation paths still write and reuse byte-identical
  outputs. Existing writer partial/length/issues/unit-dispatch failures remain
  partial rather than becoming arrangement successes

## Verification

Fail-first command (before production edit):

```text
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q tests/upgrade3/test_body06_feedback_arrangement_gate.py --basetemp=/tmp/body06-feedback-gate-before
2 failed at the intended writer-dispatch assertions
```

Final focused command:

```text
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q tests/upgrade3/test_body06_feedback_arrangement_gate.py tests/upgrade3/test_body05_feedback_text_reuse.py tests/upgrade3/test_body04_owner_material_handoff.py tests/upgrade3/test_body05_argument_cli_handoff.py --basetemp=/tmp/body06-feedback-gate-after
69 passed
```

This includes 31 new feedback controls and 38 existing WO04/05 controls.
`git diff --check` passed. Logs are `FEEDBACK_GATE_BEFORE.txt` and
`FEEDBACK_GATE_AFTER.txt`. The independent audit reviewed the earlier 29-control
version, found the original no-change bypass, and confirmed its repair; two
additional controls cover legacy fallback pointer state and unfinished action
retry preservation.

The adapter fixture retains its actual configuration: qwen3.5-plus,
max-output-tokens 5000, thinking enabled, thinking budget 1024. The provider is
controlled offline; those names/settings do not imply a live call or represent
the historical real-run configuration. The writer is a deterministic dispatch
sentinel, not a model-output-quality test. No paid calls, full suite, full BODY,
private-key access, commit or push was performed by this worker.
