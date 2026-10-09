# Production stream timeout semantics

Read-only source inspection of `worktree/optomind_research/runtime/upgrade3/module4/runtime.py` confirms that the official Qwen SSE loop applies two independent guards:

- `timeout_seconds=1800` is passed as the socket `readline()` inactivity bound (`settimeout(min(inactivity_timeout, remaining))`). Any 1800-second interval with no new wire line raises `qwen_stream_read_timeout`.
- `stream_overall_timeout_seconds=3600` is the total elapsed stream bound; when it expires the loop raises `qwen_stream_overall_timeout`.

The active Ch6/U1 partial stream last wrote at 2026-10-10 03:34:32 local. No process, configuration, or transport state was changed by this inspection.
