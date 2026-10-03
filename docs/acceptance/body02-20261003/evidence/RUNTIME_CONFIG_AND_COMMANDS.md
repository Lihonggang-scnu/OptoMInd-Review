# Runtime configuration and commands (sanitized record)

This is a human-readable record of the tested run. Credential values and credential paths are omitted.

- Tested cloud baseline: `5a8549200e5ee9e7be1992ca0cea19e1b7125487`.
- Reader model: `qwen3.7-flash`; owner model: `qwen3.5-plus` with thinking mode.
- Reader max output tokens: 5000; owner max output tokens: 16384.
- Reader timeout: 300 seconds; max retries: 1.
- Shared run ceiling: 30 CNY; initial reader ceiling: 5 CNY. Actual settled spend was 0.4776036 CNY, with reserved and uncertain spend both zero.
- Topic: `body02_real_qwen_20261003`; chapter: CH02.

The source harness entry was the repository-local `outputs/cloud_body02_real_acceptance_20261003/run_real_acceptance.py`. Its paid-run form was `python ... run_real_acceptance.py run --allow-paid`; this handoff does not authorize rerunning it. Offline preflight and recovery records are included separately.

No API key, Authorization header, signed URL, raw provider response, SQLite ledger, or database is included.
