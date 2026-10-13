"""Explicit request-bound saved responses. Never falls back to network."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def model_request_hash(messages, kwargs):
    semantic = {key: kwargs[key] for key in ("model", "max_output_tokens", "thinking", "thinking_budget", "json_mode", "temperature", "top_p", "seed", "tools", "tool_choice") if key in kwargs}
    return canonical_hash({"messages": messages, "parameters": semantic})


class RecordedClient:
    def __init__(self, path):
        self.path = Path(path)
        self.payload = json.loads(self.path.read_text())
        if self.payload.get("schema_version") != "review.model_replay.v1":
            raise ValueError("model_replay_version_invalid")
        self.calls = 0

    def complete(self, messages, **kwargs):
        binding = model_request_hash(messages, kwargs)
        row = self.payload.get("responses", {}).get(binding)
        if not isinstance(row, dict) or row.get("request_sha256") != binding:
            raise ValueError("model_replay_request_missing:" + binding)
        self.calls += 1
        return row["response"]

    chat = complete
