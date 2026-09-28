"""Qwen/DashScope model and client configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from config.secret_pool import SecretCandidate, mask_secret, secret_candidates, shuffled_secret_candidates


CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent
MODEL_POLICY_PATH = CONFIG_DIR / "model_policy.yaml"
DEFAULT_LOCAL_KEY_FILE = PROJECT_ROOT / "api_keys" / "qwen-api-key.txt"
LEGACY_LOCAL_KEY_FILE = Path.home() / "Desktop" / "qwen-api-key.txt"
DASHSCOPE_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ECONOMY_TEXT_CEILING_ENV = "OPTOMIND_ECONOMY_TEXT_CEILING"
ECONOMY_TEXT_CEILING_MODEL = "qwen3.7-flash"
ECONOMY_TEXT_SECONDARY_MODEL = "qwen3.5-plus"
_PREMIUM_TEXT_MODEL_MARKERS = (
    "qwen3.8-max",
    "qwen3.8-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.9-max",
    "qwen3.9-plus",
)


def set_economy_text_ceiling_enabled(enabled: bool) -> None:
    """Enable/disable the run-scoped economy text-model ceiling."""

    os.environ[ECONOMY_TEXT_CEILING_ENV] = "1" if enabled else "0"


def _economy_text_ceiling_enabled() -> bool:
    return str(os.environ.get(ECONOMY_TEXT_CEILING_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_non_text_model_id(model_id: str) -> bool:
    """Return True for multimodal/visual/image model IDs."""

    normalized = str(model_id or "").strip().lower()
    return any(
        marker in normalized
        for marker in ("qwen-vl", "qwen-image", "omni", "image", "vision")
    )


def _is_non_text_model_reference(model_tier_or_agent: str | None) -> bool:
    reference = str(model_tier_or_agent or "").strip().lower()
    return any(
        marker in reference
        for marker in ("qwen-vl", "qwen-image", "omni", "image", "vision")
    )


def _is_premium_text_model_id(model_id: str) -> bool:
    """Classify exact/text model IDs without touching Qwen-VL or Qwen-Image."""

    normalized = str(model_id or "").strip().lower()
    if not normalized.startswith("qwen"):
        return False
    if _is_non_text_model_id(normalized):
        return False
    return any(marker in normalized for marker in _PREMIUM_TEXT_MODEL_MARKERS)


def _apply_economy_text_ceiling(model_id: str) -> str:
    if not _economy_text_ceiling_enabled():
        return model_id
    if _is_non_text_model_id(model_id):
        return model_id
    normalized = str(model_id or "").strip()
    if normalized in {ECONOMY_TEXT_CEILING_MODEL, ECONOMY_TEXT_SECONDARY_MODEL}:
        return normalized
    if _is_premium_text_model_id(model_id):
        return ECONOMY_TEXT_CEILING_MODEL
    return ECONOMY_TEXT_SECONDARY_MODEL


def _coerce_scalar(value: str) -> Any:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(part) for part in inner.split(",")]
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none", "~"}:
        return None
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Parse the small subset of YAML used by model_policy.yaml."""

    root: Dict[str, Any] = {}
    stack: list[Tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            raise ValueError(f"Unsupported YAML line: {raw_line!r}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"Invalid YAML indentation near: {raw_line!r}")
        parent = stack[-1][1]
        if value == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value)
    return root


def load_model_policy() -> Dict[str, Any]:
    """Load the model policy YAML without requiring PyYAML."""

    if not MODEL_POLICY_PATH.exists():
        raise FileNotFoundError(MODEL_POLICY_PATH)
    text = MODEL_POLICY_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        loaded = dict(yaml.safe_load(text) or {})
    except Exception:
        loaded = _parse_simple_yaml(text)
    _validate_model_eligibility(loaded)
    return loaded


def _validate_model_eligibility(policy: Mapping[str, Any]) -> None:
    """Reject voucher-ineligible promotional models before any API call."""

    eligibility = dict(policy.get("voucher_eligibility", {}) or {})
    if not bool(eligibility.get("reject_promotional_models", False)):
        return
    excluded = {
        str(item).strip()
        for item in _as_model_references(eligibility.get("excluded_models"))
        if str(item).strip()
    }
    aliases = {
        str(name): str(model).strip()
        for name, model in dict(policy.get("model_aliases", {}) or {}).items()
    }
    configured = set(aliases.values())
    for values in dict(policy.get("model_fallbacks", {}) or {}).values():
        for reference in _as_model_references(values):
            configured.add(str(aliases.get(reference, reference)).strip())
    blocked = sorted(model for model in configured if model in excluded)
    if blocked:
        raise ValueError(
            "Promotional Qwen models are not voucher-eligible: "
            + ", ".join(blocked)
        )


def _qwen_key_filenames(key_file: str | Path | None = None) -> tuple[str, ...]:
    if key_file is not None:
        return (str(Path(key_file)),)
    return ("qwen-api-key.txt",)


def _qwen_key_candidates(key_file: str | Path | None = None, *, shuffle: bool = False) -> list[SecretCandidate]:
    configured_key_file = str(
        os.environ.get("QWEN_API_KEY_FILE")
        or os.environ.get("DASHSCOPE_API_KEY_FILE")
        or ""
    ).strip()
    if key_file is None and configured_key_file:
        # An explicitly configured file is an isolation boundary for
        # reproducible paid tests. Do not merge historical key pools.
        from config.secret_pool import read_secret_file_candidates

        candidates = read_secret_file_candidates(Path(configured_key_file))
        if shuffle:
            import random

            random.SystemRandom().shuffle(candidates)
        return candidates
    if key_file is not None:
        path = Path(key_file)
        env_names = ("DASHSCOPE_API_KEY", "QWEN_API_KEY")
        candidates: list[SecretCandidate] = []
        for env_name in env_names:
            candidates.extend(secret_candidates([env_name], ()))
        from config.secret_pool import read_secret_file_candidates

        candidates.extend(read_secret_file_candidates(path))
        if shuffle:
            import random

            random.SystemRandom().shuffle(candidates)
        seen: set[str] = set()
        unique: list[SecretCandidate] = []
        for item in candidates:
            if item.value and item.value not in seen:
                seen.add(item.value)
                unique.append(item)
        return unique
    loader = shuffled_secret_candidates if shuffle else secret_candidates
    return loader(("DASHSCOPE_API_KEY", "QWEN_API_KEY"), _qwen_key_filenames())


def _load_qwen_api_key_with_source(key_file: str | Path | None = None) -> Tuple[str | None, str]:
    # Use ordered candidates (first = primary, subsequent = fallback rotation on error)
    candidates = _qwen_key_candidates(key_file, shuffle=False)
    if candidates:
        selected = candidates[0]
        os.environ["DASHSCOPE_API_KEY"] = selected.value
        os.environ.setdefault("QWEN_API_KEY", selected.value)
        return selected.value, selected.source
    return None, "mock_llm"


def get_qwen_api_key_candidates(key_file: str | Path | None = None) -> list[Dict[str, str]]:
    """Return Qwen key candidates in random order, with masked values for logs."""
    return [
        {"api_key": item.value, "api_key_source": item.source, "api_key_masked": mask_secret(item.value)}
        for item in _qwen_key_candidates(key_file, shuffle=True)
    ]


def get_qwen_api_key_candidates_ordered(key_file: str | Path | None = None) -> list[Dict[str, str]]:
    """Return Qwen key candidates in stable file order (first key = first line in file).

    Used by the Research Harness so the primary key is always predictable.
    The existing shuffled variant (get_qwen_api_key_candidates) is unchanged for all other callers.
    """
    return [
        {"api_key": item.value, "api_key_source": item.source, "api_key_masked": mask_secret(item.value)}
        for item in _qwen_key_candidates(key_file, shuffle=False)
    ]


def load_qwen_api_key() -> str | None:
    """Load the Qwen API key from env vars or the local key file."""

    key, _ = _load_qwen_api_key_with_source()
    return key


def _tier_for_agent_or_tier(model_tier_or_agent: str | None, policy: Mapping[str, Any]) -> str:
    aliases = dict(policy.get("model_aliases", {}))
    agent_policy = dict(policy.get("agent_model_policy", {}))
    default_tier = str(policy.get("default_model_tier", "standard_model"))
    if not model_tier_or_agent:
        return default_tier
    key = str(model_tier_or_agent)
    if key in agent_policy:
        return str(agent_policy[key])
    if key in aliases:
        return key
    return key


def get_model_name(model_tier_or_agent: str | None = None) -> str:
    """Resolve a model tier or agent name into an actual model name."""

    policy = load_model_policy()
    aliases = dict(policy.get("model_aliases", {}))
    tier = _tier_for_agent_or_tier(model_tier_or_agent, policy)
    if tier == "deterministic":
        return "deterministic"
    resolved = str(aliases.get(tier, tier))
    if _is_non_text_model_reference(tier) or _is_non_text_model_id(resolved):
        return resolved
    return _apply_economy_text_ceiling(resolved)


def _as_model_references(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def get_fallback_model_names(model_tier_or_agent: str | None = None) -> list[str]:
    """Resolve an ordered fallback chain to concrete model IDs.

    Fallback entries may be model aliases (recommended) or direct Model Studio
    model IDs. The primary model and duplicates are removed while preserving
    configured order.
    """

    policy = load_model_policy()
    aliases = dict(policy.get("model_aliases", {}))
    tier = _tier_for_agent_or_tier(model_tier_or_agent, policy)
    primary = get_model_name(tier)
    raw = dict(policy.get("model_fallbacks", {})).get(tier)
    resolved: list[str] = []
    seen = {primary}
    for reference in _as_model_references(raw):
        raw_model = str(aliases.get(reference, reference)).strip()
        if (
            _is_non_text_model_reference(tier)
            or _is_non_text_model_id(raw_model)
        ):
            model_name = raw_model
        else:
            model_name = _apply_economy_text_ceiling(raw_model)
        if model_name and model_name not in seen:
            seen.add(model_name)
            resolved.append(model_name)
    return resolved


def get_qwen_client_config(model_tier_or_agent: str | None = None) -> Dict[str, Any]:
    """Return OpenAI-compatible DashScope client config with mock fallback."""

    policy = load_model_policy()
    key, source = _load_qwen_api_key_with_source()
    # Keep the user's first key as the predictable primary and rotate only
    # after a classified key/account failure.  Random ordering made a broken
    # account intermittently poison the very first scientific stage.
    key_candidates = get_qwen_api_key_candidates_ordered()
    mock_enabled = bool(dict(policy.get("mock_llm", {})).get("enabled_when_no_key", True))
    mock_llm = bool(not key and mock_enabled)
    tier = _tier_for_agent_or_tier(model_tier_or_agent, policy)
    fallback_models = get_fallback_model_names(tier)
    return {
        "api_key": key,
        "api_key_source": source,
        "api_key_masked": mask_secret(key or ""),
        "api_key_candidate_count": len(key_candidates),
        "api_key_candidates": key_candidates,
        "base_url": DASHSCOPE_COMPATIBLE_BASE_URL,
        "model": get_model_name(tier),
        "model_tier": tier,
        # Keep fallback_model for older callers while new clients consume the
        # complete ordered chain.
        "fallback_model": fallback_models[0] if fallback_models else "",
        "fallback_models": fallback_models,
        "mock_llm": mock_llm,
    }


def validate_qwen_config() -> Dict[str, Any]:
    """Validate Qwen config without exposing the API key."""

    policy = load_model_policy()
    key, source = _load_qwen_api_key_with_source()
    key_candidates = get_qwen_api_key_candidates()
    cfg = get_qwen_client_config()
    aliases = dict(policy.get("model_aliases", {}))
    return {
        "ok": True,
        "policy_path": str(MODEL_POLICY_PATH),
        "base_url": DASHSCOPE_COMPATIBLE_BASE_URL,
        "default_model_tier": policy.get("default_model_tier"),
        "model_aliases": {tier: get_model_name(tier) for tier in aliases},
        "model_fallbacks": {
            tier: get_fallback_model_names(tier)
            for tier in dict(policy.get("model_fallbacks", {}))
        },
        "has_api_key": bool(key),
        "api_key_candidate_count": len(key_candidates),
        "api_key_source": source,
        "api_key_masked": mask_secret(key or ""),
        "mock_llm": bool(cfg["mock_llm"]),
    }
