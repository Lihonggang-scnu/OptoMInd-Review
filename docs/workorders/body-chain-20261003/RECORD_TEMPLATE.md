# Work-order record template

Copy this structure for each completed package. The record is an implementation artifact, not a claim that the full scientific workflow has been validated.

```markdown
# Record: WO-<ID> — <short title>

- Status: <READY_FOR_REVIEW | BLOCKED | NEEDS_REPAIR>
- Baseline branch / commit: <cloud branch and SHA>
- Package gate: <entry / explicit approval reference>
- Paid model calls: 0
- Full planning/body/test run: not run

## Scope completed

- <bounded action and reason>

## Files and functions changed

- `<repo-relative path>`: `<function/class/entry>` — <what changed>

## Reproduced failure

- Finding: <F-number and short statement>
- Fixture or evidence: `<repo-relative evidence path or fixture ID>`
- Old observed output: <concrete result>

## Post-change output

- Input: <fixture/short-chain input ID>
- Output: <actual persisted result or report filename>
- Expected property: <specific assertion>
- Observed property: <specific result, including partial/unknown state>

## Normal-path comparison

- Unchanged case: <input>
- Result: <evidence that ordinary behavior remains intact>

## Checks

- <exact bounded command or harness entry point>
- <result and count; do not write only “passed”>

## Unresolved items and risks

- <unknown source version, remaining edge case, or deferred finding>

## Gate decision

- <continue / stop marker / blocked reason>
```

Records must use repo-relative paths. If an input exists only on the local review machine, write `LOCAL_ONLY` and cite the finding, filename, and expected shape; do not paste an absolute path or copy private data.
