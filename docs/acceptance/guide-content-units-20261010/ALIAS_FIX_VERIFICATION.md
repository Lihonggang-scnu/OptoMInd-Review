# Alias-fix verification

This note records the free verification already run for the stable source
alias normalization patch. It records observed tool results; no paid call was
made for these checks.

Source lock:

- Initial paid source: `742bef4a9134da9cbccc4e407ee7a2e36d5eab5f`
- Continuation source: `d1ab9c0b73817a97cea459bf582030206cbbf0a0`
- Change scope: stable same-identity reading-address normalization only; the
  saved paid RAW response remains unchanged.

## Tests

Command:

```text
C:\Anaconda\python.exe -X utf8 -m pytest -q tests/upgrade3/test_guide_maker_contracts.py
```

Observed result:

```text
30 passed in 0.89s
```

Command:

```text
C:\Anaconda\python.exe -X utf8 -m pytest -q tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guided_content_units.py tests/upgrade3/test_guided_continuous_units.py tests/upgrade3/test_guided_metadata_recovery.py
```

Observed result:

```text
101 passed in 17.36s
```

The focused regression covers exact duplicate handles, explicit stable alias
pairs, distinct papers, forged/conflicting aliases, unknown source handles,
and unknown atom IDs. The saved request object and RAW response are not
mutated.

## Saved-response replay

The existing offline recovery command was run against the original saved
`RAW_RESPONSE.json`. It made zero model calls and zero paid dispatches. The
replayed `DRAFT_GUIDE.json` is byte-identical to the original draft
(`0676de...ce9fe`). N3's submitted `P0605,P0049` pair is represented as one
canonical `P0049` address; N1, N2, and the remaining N3 handles stay pending
for the approved live continuation. The recovery command therefore returned
the expected non-complete draft status because material reads still require
model work.

Receipt:

`free_tests/saved_response_replay/RECOVERY_RESULT.json`

