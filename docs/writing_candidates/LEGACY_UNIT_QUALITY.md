# Opt-in legacy unit quality stage

The original unit writer and its complete task/material payload remain the authoring path. `--quality-control` adds independent actual-body assessment with Plus, at most one bounded correction/completion, and one post-assessment when the body changed. Assessment is a model diagnostic pending human review, not scientific acceptance.

```powershell
python scripts/upgrade3/legacy_unit_writer.py --input FULL_BODY_INPUT.json --output-dir pilot --quality-control --only-unit CH01:CH01_U03
```

Preview is offline. To run, add `--run --ledger <owned-ledger.sqlite> --budget-scope <shared-experiment> --budget-limit <absolute-total-cap>`, plus the existing credential/tokenizer options when needed. The same lifetime ledger covers writer, reviewer, completion and post-check. The author uses Plus with 8192 thinking tokens and 32768 output tokens by default; reviewer uses Plus with 16384 and 24576. Full materials enter capacity estimates without clipping.

`--only-unit CHAPTER:UNIT` is repeatable. The report lists requested and full unit counts. It retains full book context and each selected unit's complete material dependency closure. Selection does not change writer request identity: rerun with no selection to reuse the pilot and generate remaining units. A selected pilot assembly reports its restricted scope.

Each task assessment must name an actual task ID, use a permitted status (`covered`, `partial`, `missing`, `material_limited`), explain the assessment, and use valid material handles. Covered/partial rows must quote the exact actual body. An explicit omission quote such as `first exact fragment ... second exact fragment` is also accepted when every fragment matches verbatim in order; parser diagnostics retain the original quote and exact body spans. There is no fuzzy match or word substitution. These parser diagnostics do not change existing completion requests. Tasks may be naturally merged into prose or tables. Missing/partial rows trigger only the existing one-call task completion; material-limited rows remain pending. Only the corresponding assessment rows enter optional `gap_feedback`; these diagnostics guide the author to the missing development without changing the original tasks or materials. The completion payload retains the selected tasks' full recursive source closure, including review-mediated evidence. Returned covered task IDs are checked and remain diagnostic.

Optional exact `replace` proposals require unique, non-overlapping original-body anchors and quoted supplied material. Quotes match the actual recursively nested strings, including literal newlines. They use the existing article text editor contract. Invalid proposals are rejected without altering the original body. No style rewrite or quality retry loop runs.

Original `RAW_RESPONSE.json`, `UNIT_RESULT.json`, and unit body remain untouched. A hash-bound `quality` directory saves the original body/payload and each stage's exact messages, profile, estimate and raw response. It saves completion fragments even when incomplete. A separate `assembly_derivative` supplies the retained/changed body and pending issues to the actual assembler, with original-result provenance. Failed/partial attempts remain cached; resume never silently buys them again. Resuming a finished quality result uses zero provider calls. Post-assessment edit proposals are saved and explicitly pending; the bounded correction pass does not run again.

Use `--quality-control --reparse-saved` without `--run` to explicitly reparse existing quality RAW for free after a local parser correction. It keeps the same request identity and copies the previous summary, seal, body and parsed assessments into `reparse_history/<prior-result-hash>`. Existing paid RAW is reused. A request with no response, a partial response, or a transport/budget failure is not re-bought by this flag. It does not delete reservations or authorize a new cap. If an unattempted completion or post-assessment is needed, the free result stays `planned`. Run the same command with `--run` and the same owned ledger to continue only those missing stages. A changed body or derived status uses a separate `assembly_derivatives/<state-hash>`; the old sealed derivative is preserved.

The derived issue list resolves only the known original `markdown_table_missing_or_invalid`/`table_markdown_missing_or_invalid` syntax flags when the existing Markdown syntax check succeeds on the final body and all corresponding table task IDs are reported covered. One table may cover multiple tasks; no task-to-table count rule exists. Original artifacts keep the original issue, the derivative records it in `resolved_original_issues`, and unresolved scientific issues remain pending.

For an explicitly labeled old-response repair experiment, use the same callable stage:

```python
from optomind_research.runtime.upgrade3.legacy_unit_route import build_unit_views, build_legacy_payload
from optomind_research.runtime.upgrade3.unit_realization import run_unit_quality

view = build_unit_views(full_book)[selected_index]
result = run_unit_quality(
    view, payload=build_legacy_payload(view, language="zh"),
    existing_body=exact_saved_writer_body, output_dir=dedicated_repair_directory,
    client_factory=existing_shared_ledger_factory, run=True,
    token_counter=local_counter, language="zh",
    experiment_label="explicit_old_response_repair",
    # Explicit free RAW reparse: use run=False first; later run=True with
    # the same ledger permits only missing, never previously attempted stages.
    reparse_saved=True,
)
```

Tool sources with explicit DOI/stable paper identity register into a copied catalog. Matching identities reuse existing handles; a different identity gets a deterministic free handle while keeping conflicting-handle provenance. Existing typed references occupy their handles even when absent from the source catalog, so allocating a new identity cannot silently rebind an unchanged old task. Original task handles are not globally replaced. Multi-source tool content stays in its original tool record. Unknown identities retain content with a diagnostic and cannot gain a guessed citation identity.

Assembly title order is explicit title override, suitable initial author heading, explicit unit title, then focus fallback. Internal headings and citations remain in the body. Chapter argument metadata remains available in arrangements; the visible draft omits the planning assertion line.

Current limits: tasks need explicit paragraph/table IDs for quality assessment; other units' imagined prose cannot prove coverage elsewhere; attempted failed/partial stages are not retried by local RAW reparse. An authorized new provider attempt requires an explicitly separate experiment identity/output. Changing the quality output or label does not authorize a new budget: use the same owned lifetime ledger and cap. These conditions retain the body and report pending work.

Offline controls:

```powershell
python -m pytest tests/test_unit_realization.py tests/test_legacy_unit_route.py tests/test_legacy_unit_route_review.py tests/upgrade3/test_review_unit_writer_completion.py -q
```

The fixtures are labeled synthetic and test wiring, not scientific answers. They cover omitted versus merged tasks, material limits, task-only completion, recursive review evidence, exact byte preservation, cached/partial response retention, identity collisions, title behavior, pilot/full resume and actual derivative assembly.

On 2026-10-10 the focused command passed 69 tests. Offline previews of the full 29-unit book, four-unit battery holdout and three-unit climate holdout used zero provider calls and passed capacity preflight with intact materials using the conservative UTF-8 byte upper-bound mode (the preview script's default local tokenizer asset was absent). Eighteen tool-only source identities in the full book entered both actual unit writer catalogs and the assembler's identity index. These are wiring and capacity results; they make no claim about the generated scientific content. The later allocator guard regression passed offline; a subsequent exact-tokenizer preview remains a separate validation.

The subsequent parser/resume controls passed 81 tests, including a read-only clone of an explicitly supplied real saved RAW stage (`OPTO_QUALITY_REPLAY_FIXTURE`). Free `reparse_saved=True, run=False` parsed the saved omission quotes and returned planned; same-directory `run=True` used two synthetic offline calls for only the missing completion and post-assessment. The real fixture files remained unchanged. The production author prompt, reviewer prompt, writer profile and request identities remained unchanged.

Adjacent fullbody, output-consumption, citation-handoff and delivery tests passed 141 checks under `python -X utf8`; nine could not run their intended assertions because historical output fixtures are absent from the isolated worktree. Some older tests use `Path.read_text()` without explicit UTF-8, so Windows test invocation requires UTF-8 mode for those tests.
