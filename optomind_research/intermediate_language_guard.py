"""English-only boundary guard for structured agent-to-agent payloads."""

from __future__ import annotations

from typing import Any

from optomind_research.scientific_text_english_normalizer import (
    contains_cjk,
    ensure_english_strings,
)


def ensure_english_payload(payload: Any) -> Any:
    """Translate only CJK string values while preserving the JSON structure.

    Raw user input should be kept outside this function. It is intended for
    intermediate artifacts passed from one agent to another.
    """
    paths: list[tuple[Any, ...]] = []
    values: list[str] = []

    def collect(value: Any, path: tuple[Any, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, path + (index,))
        elif isinstance(value, str) and contains_cjk(value):
            paths.append(path)
            values.append(value)

    collect(payload, ())
    if not values:
        return payload

    translated = ensure_english_strings(values)
    if any(not item or contains_cjk(item) for item in translated):
        raise RuntimeError("English boundary guard could not normalize an intermediate payload.")

    import copy

    result = copy.deepcopy(payload)
    for path, text in zip(paths, translated):
        cursor = result
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = text
    return result

