# WO06 writer-output consumption

- Status: READY_FOR_REVIEW (bounded engineering only)
- Baseline: `8582bb698700937b0b89db65b26c8cfa958b210b`, carrying production `6efc7a4`
- Scope authorization: root's WO06 selection after reading the real WO05 acceptance; no WO07 work
- Paid/model/network calls: 0; no full BODY, full suite, credentials, commit or push

## Diagnosis and accepted boundary

Read WO05 `README`, `ASTRA_ACCEPTANCE`, `WHOLE_RESPONSE`, both generated bodies and `ROOT_ACTUAL_HANDOFF`, then WO06 and F17/F18 evidence. The handoff reports material-channel equality; full historical supplier requests remain LOCAL_ONLY. WO05 fixed argument/identity handoff but did not establish better scientific writing. Historical CH03/CH05 missing generation remains a writer-quality/completion problem; this change cannot create omitted knowledge.

F17 reproduced: production parsing returned only `body_markdown` despite a finished independent `table_markdown`. F18 reproduced: explicit completion accepted a table inside a code example and lacked ordinary-path citation reporting. Further bounded controls show raw evidence was saved after parse, so a task-metadata-only result could fail without the requested raw artifact.

## Files and functions changed

- `optomind_research/runtime/upgrade3/review_unit_writer.py`: one shared output consumer used by `parse_unit_body`, `run_unit_writing` and `run_unit_completion`; deterministic finished-table extraction/deduplication; conservative outer-wrapper handling; literal code/link preservation during optional numeric-citation repair; common citation/fence diagnostics; persisted ordinary/completion report fields; raw ordinary response saved before parsing
- `tests/upgrade3/test_body06_writer_output_consumption.py`: 53 offline controls, with fixed fake model responses and production parse/run/write/report boundaries
- `writer/capture.py`: reproducible nine-case production-boundary artifact capture, with an explicit baseline option and compact artifact aggregation
- `writer/BEFORE.json`, `writer/AFTER.json`: actual file bytes represented as UTF-8 strings with SHA-256 hashes; 66 retained artifacts per capture, including raw fixtures, emitted Markdown, complete result reports and representative full messages; repeated input/messages are represented by byte hashes
- `writer/FAIL_FIRST.txt`, `writer/FOCUSED_TESTS.txt`: bounded observed test output

No prompt, CLI, model default, budget, material routing/admission or source-science policy was changed. The separate feedback-gate worker owns `progressive_review_plan.py`.

## Before/after findings

All fixtures are SYNTHETIC engineering controls. Each ordinary case actually invokes `run_unit_writing`, `write_unit_input` and `write_unit_output`; each completion invokes `run_unit_completion` and `write_unit_completion`. None is claimed to be historical model generation.

- Independent table: baseline drops the table from ordinary output and completion remains pending; revised ordinary output preserves prose plus all table cells, and explicit completion can append the finished table
- Duplicate table: body plus independent duplicate emits one table; deterministic equality uses header/data cell strings, disregarding spacing and separator alignment/hyphen width
- Task metadata: `table_tasks` is never rendered as a finished table. Existing prose stays available; ordinary report says `pending_table` with `markdown_table_missing_or_invalid`; existing downstream `_unit_pending_problem` returns `writer_issues:1`. No-body/task-only response fails closed after saving the raw provider response
- Outer Markdown wrapper: one complete `markdown`/`md` wrapper is removed. Unlabelled wrappers are removed only when the interior has structural Markdown (heading or finished table). Top-level JSON provider envelopes still decode; Python/JSON code within an explicit body remains literal
- Internal code table: baseline completion accepted the code example; revised completion stays pending, retains the candidate, reports fence lines and preserves the original body. Indented code and blank-separated/malformed table rows also fail the syntactic table check
- Unknown source: `[P9999]` remains unchanged and appears in both ordinary and completion persisted citation reports. Canonical adjacent `[P9999][P0002]` citations do not hide the unknown handle
- Numeric citation without map: `[1]` remains `[1]`, with `numeric_citation_unresolved`; presence of `P0001` is not an authorization to guess
- Numeric citation with caller map: explicit `{'1':'P0002'}` changes eligible prose `[1]` to `[P0002]`. Only current supplied handles are accepted. Model response maps are ignored. Fenced/inline code, link destinations/titles/images/definitions and defined reference links stay literal
- Completion prefix: the original CRLF and trailing-space bytes are preserved in `ORIGINAL_BODY.md` and as the exact prefix of `COMPLETED_BODY.md`; all 18 before/after captured prefix checks pass
- Completion citation scope: `citation_scope=completion_fragment` checks only the new fragment against requested task sources. Original-body `[P0001]` is untouched. A *new* fragment citing P0001 during a P0002-only completion is honestly reported, independent of original-body content
- Normal ordinary prose/table output is unchanged. Both published WO04/WO05 acceptance bodies pass unchanged parsing controls (the parser's pre-existing trailing-whitespace normalization remains)

Unknown citations and code-preservation diagnostics are reports, not blanket rejection of usable prose. Harmless unwrapping/repair events are separate from `issues`, avoiding accidental feedback escalation. Ordinary emitted-body diagnostics are recalculated by `write_unit_output`, so existing callers persist unknown/numeric/fence/table state without a new CLI parameter. Fine-grained normalization/repair provenance is available in `run_unit_writing`'s returned fields; explicit completion also persists it.

## Completion does not imply content acceptance

`complete` retains transport meaning. `output_consumption_status=consumed` means parser consumption, and completion `pending=false` means the append passed existing transport/status and syntactic table requirements. Every new report says `content_review_status=not_reviewed`.

There is no semantic scorer, automatic content gate, task-count/table-count/paper-count criterion or model-self-report proof. Root-authored `CONTENT_REVIEW_FIXTURES.json` separately records a bounded textual review: one substantive table plus prose can cover two tasks; two syntactically finished tables can still omit the required setting, memory comparison and boundary. Those are synthetic examples read by AI reviewers, not human scientific acceptance.

## Actual fixture settings versus historical settings

No model was executed. The ordinary `run_unit_writing(model='offline-synthetic')` call reaches the fake callable with `{}` kwargs; no output-token/thinking settings are passed to that fake boundary. Explicit completion reaches the fake callable with `model='offline-synthetic'`, `max_output_tokens=4000`, `thinking_budget=0` and a captured `call_id`; `simulated=True` labels its saved body/report. Actual kwargs for every case are in both compact summaries.

The real WO05 archive reports writer `qwen3.7-flash` and usage 39845 input / 2404 output tokens (1024 reasoning tokens reported), not the requested output-limit/thinking-budget configuration. Its original driver/request remains LOCAL_ONLY. Historical Plus/12000/4000 is separately reported in the F18 reference; neither historical observation is substituted for this fixture's configuration or used to change defaults.

## Checks and reproduction

Failure-first: current structural tests loaded against the exact archived writer module, selecting the pre-existing API cases, yielded **18 failed, 3 passed, 32 deselected**. They demonstrate actual prior failures rather than expecting a new API to exist on the baseline. See `writer/FAIL_FIRST.txt`.

Final bounded checked-in controls:

```sh
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python -m pytest -q \
  tests/upgrade3/test_body06_writer_output_consumption.py \
  tests/upgrade3/test_review_unit_writer_completion.py \
  tests/upgrade3/test_body04_writer_tool_handoff.py \
  tests/upgrade3/test_body05_unit_identity_continuity.py \
  tests/upgrade3/test_body05_global_argument_handoff.py
```

Result: **117 passed**. `git diff --check` clean. The independent audit's separate edge probes are recorded by that reviewer, not relabelled as this worker's tests.

Reproduce compact artifacts (output paths may be temporary):

```sh
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python docs/workorders/body-chain-20261003/records/body06/writer/capture.py /tmp/body06-before.json --baseline-commit 8582bb698700937b0b89db65b26c8cfa958b210b
PYTHONPATH="$BODY_REPAIR_TEST_DEPS:." python docs/workorders/body-chain-20261003/records/body06/writer/capture.py /tmp/body06-after.json
```

Captures use a disposable local artifact root. Original report paths inside retained file bytes are provenance, not durable links; aggregate keys identify each original relative artifact. Hashes were checked after aggregation before deleting only this worker's earlier generated scratch trees.

## Limits and stopping point

No full Markdown parser was introduced. Table checking remains deliberately syntactic/conservative, numeric ranges are preserved/reported rather than expanded, ambiguous unlabelled code is retained, and complex link regions may be conservatively excluded from numeric repair. Model omissions, scientific accuracy, full-manuscript quality and historical raw-request verification remain outside this engineering acceptance. WO07 stays closed.
