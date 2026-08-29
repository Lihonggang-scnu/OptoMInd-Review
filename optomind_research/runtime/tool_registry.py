"""Safe research tools for ResearchWorker — path-constrained, no arbitrary I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from agentscope.tool import FunctionTool
from agentscope.tool._response import ToolChunk, ToolResultState
from agentscope.message._block import TextBlock


def make_tool_chunk(text: str, *, is_last: bool = True, ok: bool = True) -> ToolChunk:
    """Helper: wrap a string into the ToolChunk format AgentScope expects."""
    return ToolChunk(
        content=[TextBlock(text=text)],
        is_last=is_last,
        state=ToolResultState.SUCCESS if ok else ToolResultState.ERROR,
    )


def _safe_path(base_dir: Path, rel: str) -> Path | None:
    """Resolve rel relative to base_dir, rejecting traversal attempts."""
    try:
        resolved = (base_dir / rel).resolve()
        base_resolved = base_dir.resolve()
        resolved.relative_to(base_resolved)
        return resolved
    except (ValueError, OSError):
        return None


def build_research_toolkit(
    task_work_dir: Path,
    allowed_input_paths: List[Path],
) -> tuple[List[FunctionTool], dict]:
    """Build the four safe research tools bound to this task's work directory.

    Returns:
        (tools, tool_meta_map) where tool_meta_map maps name -> description.
    """

    def list_task_artifacts() -> ToolChunk:
        """List available input artifacts for this task."""
        entries = []
        for p in allowed_input_paths:
            if p.exists():
                size = p.stat().st_size
                entries.append(f"{p.name} ({size} bytes) — {p}")
        if not entries:
            return make_tool_chunk("No input artifacts available.")
        return make_tool_chunk("\n".join(entries))

    def read_task_artifact(artifact_name: str) -> ToolChunk:
        """Read a named input artifact (by filename). Returns a truncated preview.

        Args:
            artifact_name: The filename of the artifact to read (e.g. 'sample_manifest.json').
        """
        # Try allowed input paths first
        for p in allowed_input_paths:
            if p.name == artifact_name or str(p) == artifact_name:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    preview = text[:3000]
                    if len(text) > 3000:
                        preview += f"\n... [truncated, full length {len(text)} chars, path: {p}]"
                    return make_tool_chunk(preview)
                except OSError as exc:
                    return make_tool_chunk(f"Error reading {p}: {exc}", ok=False)

        # Also try resolving within work_dir
        resolved = _safe_path(task_work_dir, artifact_name)
        if resolved is None:
            return make_tool_chunk(
                f"Path traversal rejected for: {artifact_name!r}", ok=False
            )
        if not resolved.exists():
            return make_tool_chunk(
                f"Artifact not found: {artifact_name!r}. "
                f"Use list_task_artifacts() to see available files.",
                ok=False,
            )
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
            preview = text[:3000]
            if len(text) > 3000:
                preview += f"\n... [truncated, full length {len(text)} chars, path: {resolved}]"
            return make_tool_chunk(preview)
        except OSError as exc:
            return make_tool_chunk(f"Error reading {resolved}: {exc}", ok=False)

    def write_task_note(filename: str, content: str) -> ToolChunk:
        """Write a note or partial result to the task working directory.

        Args:
            filename: The target filename (no directory components). Must end in .txt, .md, or .json.
            content: The text content to write.
        """
        allowed_suffixes = {".txt", ".md", ".json"}
        if any(c in filename for c in ("/", "\\", "..")):
            return make_tool_chunk(
                "Filename must not contain path separators or '..'.", ok=False
            )
        suffix = Path(filename).suffix.lower()
        if suffix not in allowed_suffixes:
            return make_tool_chunk(
                f"Only {sorted(allowed_suffixes)} extensions are allowed.", ok=False
            )

        target = task_work_dir / filename
        try:
            task_work_dir.mkdir(parents=True, exist_ok=True)
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(task_work_dir), prefix=".note_tmp_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp, str(target))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return make_tool_chunk(f"Saved to {target} ({len(content)} chars).")
        except OSError as exc:
            return make_tool_chunk(f"Write failed: {exc}", ok=False)

    def validate_task_result(
        expected_outputs: str,
        success_criteria: str,
    ) -> ToolChunk:
        """Deterministic validator: checks whether expected outputs exist and criteria are described.

        This is a lightweight structural check — it does NOT call an LLM.

        Args:
            expected_outputs: JSON array of expected output filenames (e.g. '["PLAN.md", "RESULT.json"]').
            success_criteria: A brief description of how criteria were met (checked for non-empty).
        """
        errors = []

        # Parse expected outputs
        try:
            outputs = json.loads(expected_outputs) if isinstance(expected_outputs, str) else expected_outputs
            if not isinstance(outputs, list):
                outputs = [str(outputs)]
        except (json.JSONDecodeError, TypeError):
            outputs = [str(expected_outputs)] if expected_outputs else []

        # Check each expected output
        missing = []
        for fname in outputs:
            target = task_work_dir / str(fname)
            if not target.exists():
                missing.append(str(fname))

        if missing:
            errors.append(f"Missing expected outputs: {missing}")

        # Check success_criteria description
        if not str(success_criteria or "").strip():
            errors.append("success_criteria description is empty.")

        if errors:
            detail = "; ".join(errors)
            return make_tool_chunk(
                f"VALIDATION_FAILED: {detail}", ok=False
            )
        return make_tool_chunk(
            f"VALIDATION_PASSED: all {len(outputs)} expected outputs present."
        )

    tools = [
        FunctionTool(list_task_artifacts),
        FunctionTool(read_task_artifact),
        FunctionTool(write_task_note),
        FunctionTool(validate_task_result),
    ]

    tool_meta = {t.name: t for t in tools}
    return tools, tool_meta
