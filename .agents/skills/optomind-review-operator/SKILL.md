---
name: optomind-review-operator
description: Operate, diagnose, resume, and validate OptoMind-Review on a downloaded repository. Use when a user asks an AI agent to open the replay portal, prepare a real Review run, submit a new research question, monitor or recover a run, or verify its publications and evidence trail.
---

# OptoMind Review Operator

Help the user run the repository as delivered. Do not redesign the research pipeline unless the user separately asks for code changes.

## Establish the task and boundary

1. Treat the Git worktree containing `quickstart.py`, `run_review_harness.py`, `replay/`, and `artifacts/e2e/` as the only project root.
2. Determine whether the user wants static replay, environment diagnosis, a new quick run, a full publication run, recovery of an existing run, or artifact verification.
3. Static replay and `python quickstart.py doctor` are read-only. Starting the front-end connectivity check makes minimal real network/API requests. Starting a research run consumes model quota and may take substantial time, so require an explicit request and a chosen `quick` or `full` profile.
4. Never read or print credential contents. Check only expected filenames, file existence, and whether files are non-empty. Never place secrets in commands, prompts, logs, reports, or commits.
5. Do not modify the three formal records under `replay/` or `artifacts/e2e/`. New runs belong under `local_runs/`.
6. Do not commit, push, change repository settings, delete caches, or rebuild checkpoints unless the user explicitly authorizes that separate action.

## Preferred startup workflow

1. Read the relevant parts of [the full Chinese Agent manual](../../../AGENT_GUIDE.zh-CN.md): sections 5–8 for a new machine or launch, sections 7–8 and 13–14 for diagnosis or recovery, and sections 9–11 for result verification.
2. From the repository root, run `python quickstart.py doctor` on Windows or `python3 quickstart.py doctor` on macOS/Linux. Report asset, key-file, and dependency readiness without exposing values.
3. For a human-facing session, start `python quickstart.py ui` or `python3 quickstart.py ui`. An Agent should prefer this command over double-clicking the launcher because terminal output remains observable. Report the loopback URL printed by the process.
4. Let the user use static replay immediately. Before live questioning, invoke “检查并准备真实运行” and wait for every required check to finish. Do not bypass a failed gate merely to enable the question form.
5. If the default port is occupied, restart with `--port 0` for an automatically selected loopback port. Keep the service bound to `127.0.0.1`.

## Choose a live profile

- `quick`: real question understanding, literature retrieval, evidence organization, argument formation, and drafting; global model budget is capped at CNY 3. It skips image generation, Chinese translation, and PDF compilation.
- `full`: the complete publication mainline used by the three formal E2E records; global model budget is capped at CNY 15 and includes full-manuscript coordination, visual processing, bilingual publication, and PDF generation when local TeX dependencies are available.

Do not promise a fixed wall-clock duration. Network conditions, literature availability, model retries, and TeX installation affect runtime. Run only one live task at a time.

## Observe, recover, and verify

Monitor `local_runs/<run-id>/HARNESS_STATE.json`, `HARNESS_COST.json`, `HARNESS_EVENTS.jsonl`, `HARNESS_METRICS.json`, `HARNESS_RUN_REPORT.md`, and `DELIVERY_GATE.json`. Distinguish actual spend from reserved budget and distinguish a recoverable stage state from final failure.

Resume an interrupted run from its existing run directory after confirming that the question and `TOPIC_IDENTITY.json` still match. Inspect checkpoints and the last valid candidate before considering any rebuild. Never delete a run directory as a retry mechanism.

For final verification, separately report topic identity, English and Chinese deliverables, article scale, citations, model usage, cost, visual assets, warnings, and the exact recovery point. Do not describe mock/preflight execution as a real E2E run, and do not describe a compiled placeholder-author PDF as submission-ready.

For detailed state meanings, citation boundaries, visual rules, historical failure modes, and the root-cause-first diagnostic order, read [AGENT_GUIDE.zh-CN.md](../../../AGENT_GUIDE.zh-CN.md).
