"""Secret pool helpers with multi-key rotation.

Text key files may contain one key per line. Environment variables may contain
one key or several keys separated by newline, comma, or semicolon.
"""

from __future__ import annotations

import os
import random
import re
import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_KEYS_DIR = PROJECT_ROOT / "api_keys"
DESKTOP = Path.home() / "Desktop"
_RNG = random.SystemRandom()


@dataclass(frozen=True)
class SecretCandidate:
    value: str
    source: str


def split_secret_values(raw: str) -> list[str]:
    values: list[str] = []
    for line in str(raw or "").replace(",", "\n").replace(";", "\n").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if "=" in item and not item.startswith(("sk-", "sk_")):
            _key, possible_value = item.split("=", 1)
            item = possible_value.strip()
        if item:
            values.append(item)
    return values


def mask_secret(secret: str) -> str:
    secret = str(secret or "").strip()
    if not secret:
        return ""
    # Source path + ordinal is sufficient for rotation diagnostics.  A fixed
    # marker avoids persisting even partial credential material in logs.
    return "****"


def secret_fingerprint(secret: str) -> str:
    """Return a stable, non-reversible identifier safe for diagnostics."""

    value = str(secret or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def read_secret_file_candidates(path: Path) -> list[SecretCandidate]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [
        SecretCandidate(value=value, source=f"{path}#{index}")
        for index, value in enumerate(split_secret_values(raw), 1)
    ]


def secret_candidates(
    env_names: Iterable[str],
    filenames: Iterable[str],
    *,
    include_legacy_desktop: bool = True,
) -> list[SecretCandidate]:
    candidates: list[SecretCandidate] = []
    seen: set[str] = set()

    def add(candidate: SecretCandidate) -> None:
        value = candidate.value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(SecretCandidate(value=value, source=candidate.source))

    for env_name in env_names:
        raw = os.environ.get(env_name, "")
        for index, value in enumerate(split_secret_values(raw), 1):
            add(SecretCandidate(value=value, source=f"{env_name}#{index}"))

    for filename in filenames:
        for root in ([API_KEYS_DIR, DESKTOP] if include_legacy_desktop else [API_KEYS_DIR]):
            for candidate in read_secret_file_candidates(root / filename):
                add(candidate)

    return candidates


def shuffled_secret_candidates(
    env_names: Iterable[str],
    filenames: Iterable[str],
    *,
    include_legacy_desktop: bool = True,
) -> list[SecretCandidate]:
    items = secret_candidates(env_names, filenames, include_legacy_desktop=include_legacy_desktop)
    _RNG.shuffle(items)
    return items


def choose_secret(
    env_names: Iterable[str],
    filenames: Iterable[str],
    *,
    include_legacy_desktop: bool = True,
) -> SecretCandidate | None:
    items = secret_candidates(env_names, filenames, include_legacy_desktop=include_legacy_desktop)
    if not items:
        return None
    return _RNG.choice(items)


def attach_provider_error_detail(exc: BaseException) -> str:
    """Read and cache a provider error code/message without exposing secrets."""

    cached = str(getattr(exc, "_optomind_provider_error", "") or "")
    if cached:
        return cached
    provider_code = ""
    provider_message = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            provider_code = str(error.get("code") or error.get("type") or "")
            provider_message = str(error.get("message") or "")
    except Exception:
        pass
    detail = " ".join(
        item for item in (provider_code, provider_message[:300]) if item
    )
    try:
        setattr(exc, "_optomind_provider_error", detail)
    except Exception:
        pass
    return detail


def is_model_scoped_allocation_error(exc: BaseException) -> bool:
    """Whether this key works but the requested model allocation is blocked."""

    detail = attach_provider_error_detail(exc)
    text = f"{type(exc).__name__}: {exc} {detail}".lower()
    return bool(
        re.search(
            r"allocationquota\.freetieronly|free[\s_-]*tier[\s_-]*only|"
            r"use free tier only",
            text,
        )
    )


def is_key_level_http_error(exc: BaseException) -> bool:
    # FreeTierOnly can be model-specific: preserve this key and descend the
    # configured model ladder before considering another account.
    if is_model_scoped_allocation_error(exc):
        return False
    code = getattr(exc, "code", None)
    if code in {401, 403, 429}:
        return True
    provider_detail = attach_provider_error_detail(exc)
    text = f"{type(exc).__name__}: {exc} {provider_detail}".lower()
    return bool(
        re.search(
            r"invalid api key|unauthorized|forbidden|quota|rate limit|"
            r"too many requests|arrearage|overdue|free.?tier|allocationquota|"
            r"account.+standing|insufficient balance|billing|payment",
            text,
        )
    )
