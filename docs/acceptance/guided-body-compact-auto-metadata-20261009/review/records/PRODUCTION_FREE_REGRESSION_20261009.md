# Production guided专项 free regression

## Gate result

**PASS: 167 passed in 18.58s.** No provider, key, or budget-ledger access was used by this test command.

## Exact executed command

The worktree is the frozen campaign worktree.  PowerShell expanded the guided test files, then Python was run in UTF-8 mode because Windows' default GBK locale cannot decode the repository's UTF-8 JSON fixtures:

```powershell
$files = @(Get-ChildItem tests/upgrade3 -Filter 'test_guided*.py' | Sort-Object Name | ForEach-Object { $_.FullName })
python -X utf8 -m pytest $files -q
```

Equivalent expanded file set:

```text
tests/upgrade3/test_guided_body_cli.py
tests/upgrade3/test_guided_body_contracts.py
tests/upgrade3/test_guided_body_plus_only.py
tests/upgrade3/test_guided_body_writer.py
tests/upgrade3/test_guided_delivery_format.py
tests/upgrade3/test_guided_metadata_recovery.py
tests/upgrade3/test_guided_source_handoff.py
tests/upgrade3/test_guided_writer_prompt_requirements.py
```

Output:

```text
........................................................................ [ 43%]
........................................ [ 86%]
.......................                                                  [100%]
167 passed in 18.58s
```

The broader documentation command was also attempted under UTF-8 mode: 521 passed and 6 failed in Windows-specific fullbody long-path/archive replay branches.  Those six are outside the requested guided专项 gate; no production source was changed to mask them.  The guided-only gate above is fully green.
