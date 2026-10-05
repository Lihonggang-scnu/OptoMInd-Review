# Live commands and recovery record

All commands below ran from `F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\worktree` with `C:\Anaconda\python.exe`. The key argument was only the path `F:\OptoMind-Review-2\api_keys\qwen-api-key.txt`; the file was not read into this record or copied. Environment used for live/native runs: `PYTHONUTF8=1`, `PYTHONHASHSEED=0`, `PYTHONPATH=.`, `OPTOMIND_ECONOMY_TEXT_CEILING=0`.

The fixed case was `F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\ROOT_FIXED_CASE.json`; its recorded SHA256 is `7211e022ac4bc2684c1a7d5b5be905bb9fd059f1e3a0f1529a88bd867c6b6378`. The shared ledger was `F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite`, limit `10` CNY, with retry count zero and one configured key.

## Real scheme runs

The A first attempt used the formal `scripts\upgrade3\post_body_revision.py run` entry point with variant `A`, `config\post_body_revision\A.json`, `--output-root ...\live`, `--run --allow-paid --budget-cny 10`, the shared ledger, and the key-file path above. The exact command line was not persisted as a standalone command log; this is an execution template reconstructed from the saved A metadata and output paths, so any unrecorded shell quoting is unknown. It made one Qwen call (`qwen3.7-flash`), settled `0.0759348` CNY, and failed the fixed-issue identity guard because the response returned R4/R5/R6 instead of R1/R2/R3. Output: `live\A\live`.

The single A retry used the same formal entry point and case, variant `A`, original A config, shared ledger and key-file path, with `--output-root ...\live_attempt2`. It made three calls, settled `0.1059688` CNY, and completed with R1 applied; R2/R3 remained unresolved. Output: `live_attempt2\A\live`.

The exact B command was:

```text
C:\Anaconda\python.exe scripts\upgrade3\post_body_revision.py run --case F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\ROOT_FIXED_CASE.json --variant B --config F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\worktree\config\post_body_revision\B.json --output-root F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live --run --allow-paid --budget-cny 10 --budget-ledger F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite --key-file F:\OptoMind-Review-2\api_keys\qwen-api-key.txt
```

B made five calls. Four settled for known cost `0.0419254` CNY; the first R3 verifier timed out with an uncertain `0.2137224` CNY reservation. Original output remains `live\B\live`.

The B recovery driver command was:

```text
C:\Anaconda\python.exe F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\native\b_retry_driver.py --mode live --output F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live_B_retry\B\recovery
```

The driver replayed the four completed B responses after matching saved model and messages, then dispatched exactly one new `issue_003_verifier` call. The recovery settled `0.006509` CNY; the earlier uncertain reservation stayed held. Full B known settled cost is therefore `0.0484344` CNY, while the recovery report's `0.006509` is incremental recovery cost only. Output: `live_B_retry\B\recovery`.

The exact C command was:

```text
C:\Anaconda\python.exe scripts\upgrade3\post_body_revision.py run --case F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\ROOT_FIXED_CASE.json --variant C --config F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\worktree\config\post_body_revision\C.json --output-root F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live --run --allow-paid --budget-cny 10 --budget-ledger F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite --key-file F:\OptoMind-Review-2\api_keys\qwen-api-key.txt
```

C made five `qwen3.7-flash` calls and settled `0.0482194` CNY. R1 and R3 were applied/verified; R2 remained pending. Output: `live\C\live`.

## Offline evaluator commands

The three-run blinded packet preparation used seed `20261005`, swap fraction `0.34`, the fixed case, and runs `live_attempt2\A\live`, `live_B_retry\B\recovery`, and `live\C\live`. It produced `evaluation\manifest.json` and packets under `evaluation\packets` with no provider call.

The preparation command was:

```text
C:\Anaconda\python.exe scripts\upgrade3\evaluate_post_body_revision.py prepare --case F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\ROOT_FIXED_CASE.json --runs F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live_attempt2\A\live F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live_B_retry\B\recovery F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\C\live --output-dir F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\evaluation --seed 20261005 --swap-fraction 0.34
```

The offline report command consumed the existing `evaluation\judgments_primary.json`:

```text
C:\Anaconda\python.exe scripts\upgrade3\evaluate_post_body_revision.py report --evaluation-dir F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\evaluation --judgments F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\evaluation\judgments_primary.json --output F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\evaluation\report_offline\primary_report.json
```

The tool accepted an assessed A judgment and an assessed B judgment as agent observations. C had no accepted complete judgment (one invalid and one missing), so no three-way quality conclusion or Pareto-qualified method was accepted. The report explicitly labels assessments as not ground truth. B's generation cost in this report is the recovery increment `0.006509` CNY; it does not replace full B accounting (`0.0484344` known plus the held uncertain reservation).

The focused evaluator module was rerun once:

```text
C:\Anaconda\python.exe -m pytest tests/upgrade3/test_post_body_revision_evaluation.py -q
```

Result: `12 passed in 12.11s`. No production or test files were changed during this closeout; the two accepted test fixture byte-write changes were already present before the run.

## Final ledger observation

The latest shared-ledger totals are `0.2785574` CNY settled, `0.2137224` CNY uncertain/held, and `0` reserved across 15 real attempts: 14 settled and 1 uncertain. No further paid call, network operation, Git mutation, or key-file read was performed during closeout.

## Final offline verification update

The four independent modules were collected and executed together:

```text
C:\Anaconda\python.exe -m pytest tests/upgrade3/test_post_body_revision_contracts.py tests/upgrade3/test_post_body_revision_cli.py tests/upgrade3/test_post_body_revision_engine.py tests/upgrade3/test_post_body_revision_evaluation.py -q
```

Collection reported **92 tests** and execution reported **92 passed in 28.76s**. This was a native offline test run with no model or network call.

The original `evaluation\judgments_primary.json` was preserved byte-for-byte. A separate `evaluation\judgments_primary_consistent.json` changes only `pair-002-0.knowledge_preservation` from `preserved` to `loss`; its existing loss record identifies the right-side original passage, while the winner remains `left`. The consistent offline report is `evaluation\report_offline\consistent_report.json`; the primary report was not overwritten.

The consistent report accepted the assessed forward packets `pair-001-0` (A), `pair-002-0` (C), and `pair-003-0` (B). The reverse packets `pair-001-1` and `pair-002-1` remain `missing`; no reverse judgment was fabricated. The C method-level loss flag in this report is the copied top-level comparison flag for the original-side loss record, not a claim that the C candidate lost knowledge. The report still has no Pareto-qualified method and is an agent assessment, not a scientific conclusion.
