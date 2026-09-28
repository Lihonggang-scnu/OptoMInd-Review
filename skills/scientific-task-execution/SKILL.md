---
name: scientific-task-execution
description: A general-purpose methodology skill for executing structured scientific research tasks using tool-based reasoning.
---

# Scientific Task Execution Methodology

This skill describes a general working method for completing structured research tasks. It does not contain domain-specific knowledge or test answers.

## Step 1 — Inspect the task

Read the task contract before acting. Identify:
- The stated goal and scope.
- Constraints that must be respected.
- Success criteria that define a passing result.
- Expected output files.

## Step 2 — Plan proportionally

**For simple tasks** (single artifact input, single output file, clear validation): proceed directly.
Do not create task entries for routine tool calls such as "read artifact" or "run validation".

**For complex tasks** (2 or more expected output files, multiple distinct phases, or iterative research):
use `create_task` to list the sub-steps needed to reach the goal. Keep tasks coarse-grained and
outcome-oriented, not tool-call-level. Update task status (`update_task`) as each sub-task completes.

## Step 3 — Read required artifacts

Use `list_task_artifacts` to discover available inputs.
Use `read_task_artifact` to read each relevant file.
Do not assume content — read it explicitly.

## Step 4 — Use tools instead of guessing

If a piece of information can be obtained via a tool, call the tool.
If a tool call fails, read the error message and try an alternative approach.
Record what failed and why using `write_task_note`.

## Step 5 — Record findings

Use `write_task_note` to persist:
- Key observations from artifacts.
- Identified gaps or missing information.
- Intermediate conclusions.
- The plan for the remaining steps.

This creates a recoverable checkpoint in case the context window is compressed.

## Step 6 — Validate before completion

Before declaring the task done:
1. Verify each expected output file has been written.
2. Confirm each success criterion has been addressed.
3. Call `validate_task_result` with the expected output list and a concise description of how criteria were met.
4. Only declare completion after receiving `VALIDATION_PASSED`.

## Step 7 — Recover from tool failure

If a tool returns an error:
- Read the full error message.
- Try a different filename, argument, or approach.
- If recovery is impossible, write a note documenting the blocker and the reason.

## Step 8 — Report honestly

If a criterion cannot be met due to missing data, tool limitations, or unclear scope:
- State the blocker explicitly.
- Do not fabricate a passing result.
- Call `validate_task_result` with the actual state — a VALIDATION_FAILED is an honest outcome.
