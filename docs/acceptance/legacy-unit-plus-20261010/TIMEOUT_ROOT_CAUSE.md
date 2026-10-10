# Ch6_U1 timeout root cause checkpoint

This is a transport-level finding from the preserved Ch6_U1 attempt. It does
not identify an internal provider failure mode.

## Proven evidence

- `attempt_001/ERROR.json` records `QwenTransportError:qwen_stream_read_timeout`.
- The request received HTTP 200 and opened an SSE response. The effective
  client bounds were 1800 seconds of socket read inactivity and 3600 seconds
  total stream time.
- The preserved partial stream contains 646 JSON SSE events and 192,846 raw
  bytes. It contains 7,335 reasoning characters (7,339 UTF-8 bytes), zero
  answer characters, no usage event, no `finish_reason`, and no `[DONE]`.
- The last periodic event was event 640 at about 42.4 seconds. Event 646 was
  followed by no further wire line; the client emitted the read-timeout event
  at about 1,842.7 seconds elapsed, which is the 1,800-second inactivity bound
  after response open.
- The process then exited. There is currently no live process for PID 35096.

## Boundary of the conclusion

The evidence proves that the client waited for a new SSE wire line and timed
out after 30 minutes of inactivity. It does not prove whether the provider was
still reasoning, stalled in an internal queue, dropped the connection upstream,
or encountered another server-side condition. No paid probe, timeout increase,
capacity reduction, model switch, or prompt change is justified by this
record.

## Recovery implication

The preserved attempt remains an uncertain failed attempt and is never treated
as a successful cache. After the separately audited user budget waiver, the
unchanged CLI with explicit `--retry-failed` will create one new attempt for
Ch6_U1 and first attempts for the seven not-started units. The 21 sealed
successful attempts retain their existing cache identity and are skipped.

## Recovery outcome

The explicit unchanged-input retry completed successfully as `attempt_002`. The other seven units completed on their first attempts, and all 21 sealed successful bodies retained their original hashes. This confirms the prior stall did not require a capacity, prompt, model or source change; its internal provider cause remains unavailable.
