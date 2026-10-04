# Final preflight seams: bounded offline handoff

Status: **READY_FOR_LOCAL_PREFLIGHT_FINAL_SEAMS**

Branch: `body-preflight-final-seams-cloud-20261004`

Base archive: `4b115e25b900c7832996b4c8213dc657109151d5` (production code unchanged from `6d1c8525fe0c2bcad53fcaec7699c88e04e63503`). This independent branch changes no protected branch and is not merged.

## Decision and actual changes

The three proposed engineering repairs are supported. The local scientific improvements are limited to one writer unit and do not establish full-BODY recovery. Read [LOCAL_ALIGNMENT.md](LOCAL_ALIGNMENT.md) for the actual input/output observations and source limitations.

| Seam | Before | After | Code |
|---|---|---|---|
| Canonical references beside explanatory brackets | Actual archived body contains 7 canonical handles but writer report counts only P0576 | All 7 recognized in first-appearance order, none falsely unused; BODY bytes unchanged. Defined Markdown links/images/code stay protected; numeric replacement keeps its conservative rules | `review_unit_writer.py`: `_citation_matches`, `citations_in` |
| Windows task-ID paths | `CH02:U3` enters CLI directories and normal/completion raw filenames, creating invalid directory/ADS semantics | Task-derived components encoded consistently; normal/completion preview, fake and provider-boundary-controlled paths create ordinary files; JSON/messages preserve original chapter/unit/task identities | New small `portable_paths.py`; runtime normal/completion raw paths; `scripts/upgrade3/review_unit_writer.py` directory joins |
| Moved prior-reading provenance | Identical material loaded from another path changes `reused_from` and triggers an additional case call | Existing metadata projection ignores operational `reused_from`; actual resumed batch makes no additional case call. Scientific content, task, model and effective output limit still invalidate | `ProgressiveReviewPlanner._cache_contract`: one metadata key |

No new planner, scientific prompt, case ordering, material-eligibility rule or broad material merge. In particular review-derived original studies remain usable without own A/B/fulltext. The temporal A/B-shadow probe is retained as a risk; the actual packet contains both A/B and deep, and the normal premerge control preserves them.

## Evidence and commands

- [citations/README.md](citations/README.md): failure-first controls plus actual saved writer report re-consumption; before/after persisted reports, unchanged body hash, downstream first-use order and numbering checks. This is historical output replay, not a new generation.
- [paths/README.md](paths/README.md): actual normal/completion CLI file inventories, original identities, message equality, raw response round-trip, and path lengths. Linux physical I/O with explicit Windows lexical rules, not native NTFS execution. Actual provider calls = 0, even where the CLI `mode=run` report counts a substituted boundary call.
- [cases/README.md](cases/README.md): real loader and production cache/packet consumer with controlled responses; before/after call counts, material and parameter invalidation. Synthetic evidence, not the excluded local snapshots.
- [INDEPENDENT_REVIEW.md](INDEPENDENT_REVIEW.md): independent review, corrected encoded-namespace case-fold collision and remaining parser/platform limits.
- [PROMPT_ORDER_CHECK.json](PROMPT_ORDER_CHECK.json): AST equality of planner prompts and `run`, reading prompt, normal/completion writer message constructors and prompt loaders against base.

Run from repository root in the cloud verification environment:

```bash
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-stage2-deps:. python docs/workorders/body-chain-20261003/records/preflight_final_seams/run_offline_controls.py
```

The runner denies socket connections. Final aggregate: **454 passed** (see `FINAL_CONTROLS.txt`). Previous 290 material controls also passed independently on untouched production base: **290 passed**, `BASELINE_CONTROLS.txt`. These counts overlap; they are not additive and do not represent the complete repository suite. Targeted pre-fix failures and post-fix tests are in the per-seam records. `git diff --check` and compileall of changed modules/tests/record drivers passed. No full BODY or full test-suite run was attempted.

## Remaining boundaries and next local check

1. Run the ordinary writer and completion **formal CLI** on native Windows with `CH02:U3` and the intended real output root. Check raw files with ordinary directory listing/readback, not ADS, and confirm report paths and JSON IDs. Old unsafe Linux filenames/NTFS streams are not migrated. Safe legacy names remain unchanged; legacy IDs differing only by case retain the existing Windows alias limitation. The digest is collision-resistant, not a mathematical uniqueness guarantee. Arbitrarily long caller-selected roots/Windows MAX_PATH are outside this patch.
2. Reconsume the archived response under local production functions before any paid test. Expected handles: P0564, P0576, P0602, P0561, P0388, P0190, P0582. Existing placeholder prose `[参考单元论证]` and scientific overclaims remain unchanged. Recognition does not validate science or every possible Markdown construct; pre-existing nested-bracket link/image parsing remains limited.
3. Check local path-only case reuse under the new cache contract. Old hashes containing `reused_from` can require one normal refresh after upgrade; this patch does not reconstruct old hashes. Subsequent identical-content moves reuse; changed substantive inputs must not. This is a case-batch guarantee, not a promise that every upstream planner stage makes zero calls.
4. Full scientific quality, multi-chapter cohesion and native Windows execution remain unverified. Missing actual source-state snapshots are not required for these bounded seams, but independent reproduction of local real-history compatibility still requires those excluded snapshots on the local machine.

No paid provider call, real research retrieval/download, full BODY run, scientific-text editing, credential use, or merge. Git transfers are repository operations, not model calls. Stop here for local GPT 6.1 sol acceptance; complete real experimentation requires separate authorization.

Evidence packaging note: pytest display-only trailing whitespace was removed from log lines for `git diff --check`; assertions, messages and outcomes were not changed.
