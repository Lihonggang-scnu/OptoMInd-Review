"""Shared role capacities for local outline-revision drivers.

This only loads configuration and constructs the existing production client.
It does not plan, review, write, or initiate a model request.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[3] / "config" / "outline_revision" / "quality.json"


def load_quality_profile(role: str, profile_path: str | Path | None = None) -> dict[str, Any]:
    """Return the selected explicit settings without a second set of defaults."""
    path = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    profiles = document.get("profiles", {})
    if role not in profiles:
        raise ValueError(f"quality_profile_unknown_role:{role}")
    settings = profiles[role]
    required = {"model", "thinking", "thinking_budget", "max_output_tokens", "json_mode", "timeout_seconds"}
    if not isinstance(settings, dict) or set(settings) != required:
        raise ValueError(f"quality_profile_invalid_settings:{role}")
    return copy.deepcopy(settings)


def make_quality_client(
    role: str,
    *,
    key_file: str | Path,
    budget_ledger: Any,
    raw_response_dir: str | Path,
    profile_path: str | Path | None = None,
    prompt_token_counter: Any = None,
) -> Any:
    """Use the role profile verbatim, retaining the normal shared cost ledger.

    Local drivers should pass the client to existing production consumers,
    rather than rebuilding the former small-capacity constants. Budget changes
    belong in the profile, so the constructor cannot receive conflicting copies.
    """
    from .module4.runtime import QwenDirectClient

    settings = load_quality_profile(role, profile_path)
    return QwenDirectClient(
        **settings,
        key_file=key_file,
        budget_ledger=budget_ledger,
        raw_response_dir=raw_response_dir,
        prompt_token_counter=prompt_token_counter,
        max_retries=0,
    )
