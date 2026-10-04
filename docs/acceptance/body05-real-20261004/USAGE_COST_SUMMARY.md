# Actual usage and cost

WO05 made three paid model calls. The real arrangement chose a one-to-one brief mapping; this does not describe the other two calls. Values below are from the run telemetry and root acceptance report.

| Call | Model | Input tokens | Output tokens | Actual CNY |
| --- | --- | ---: | ---: | ---: |
| Whole-plan coordination | `qwen3.5-plus` | 303,741 | 3,096 | 1.289268 |
| Chapter arrangement | `qwen3.5-plus` | 8,115 | 2,406 | 0.0180408 |
| Unit writing | `qwen3.7-flash` | 39,845 | 2,404 | 0.0296766 |
| **WO05 new total** |  |  |  | **1.3369854** |

The shared ledger after this run was 2.0490938 CNY cumulative, with 0 held and 27.9509062 CNY remaining. All three calls finished with `stop`, with no model retry. The original run estimated a 1.53806 CNY preflight reserve and remained within the 2 CNY run cap recorded by the acceptance report.

## Verification boundary

- Offline controls: two independent passing runs each returned 392 passed, 2 deselected, and 0 paid calls. Root's run took 25.99 seconds; the worker's UTF-8 rerun took 26.31 seconds.
- Real chain: one whole-plan coordination call, one arrangement call, and one unit-writing call.
- Not run: complete five-chapter BODY, owner revision calls, and a real split/merge case. Split/merge behavior was checked by offline controls only.
- Initial offline invocations encountered import/locale issues; setting worktree `PYTHONPATH` and UTF-8 mode made the same offline controls pass without a production change. Separately, the real driver initially could not find the tokenizer; it stopped before any model call and resumed after being pointed at the existing local tokenizer.
- The numbers are usage evidence for this run, not a claim of scientific quality or full manuscript acceptance.
