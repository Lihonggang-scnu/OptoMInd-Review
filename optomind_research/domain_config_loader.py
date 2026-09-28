"""领域配置加载器 — 读取 domain_config.yaml，提供统一访问接口。

迁移到新领域只需修改项目根目录的 domain_config.yaml，无需改代码。
优先级：1) 环境变量 DOMAIN_CONFIG  2) 项目根 domain_config.yaml  3) 默认值
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "domain_config.yaml"

_CACHE: dict[str, Any] | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        import warnings
        warnings.warn(
            "PyYAML is not installed — domain_config.yaml will not be loaded and all "
            "config values will use defaults. Install with: pip install pyyaml",
            ImportWarning,
            stacklevel=3,
        )
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_domain_config(path: Path | str | None = None, *, force_reload: bool = False) -> dict[str, Any]:
    """加载领域配置。结果被缓存；force_reload=True 可强制重读。"""
    global _CACHE
    if _CACHE is not None and not force_reload and path is None:
        return _CACHE

    env_path = os.environ.get("DOMAIN_CONFIG", "")
    resolved: Path
    if path is not None:
        resolved = Path(path)
    elif env_path:
        resolved = Path(env_path)
    else:
        resolved = _DEFAULT_CONFIG_PATH

    config = _load_yaml(resolved) if resolved.exists() else {}
    if path is None:
        _CACHE = config
    return config


# ── 便捷访问函数 ─────────────────────────────────────────


def get_domain_name(config: dict[str, Any] | None = None) -> str:
    c = config if config is not None else load_domain_config()
    return str((c.get("domain") or {}).get("name") or "unknown_domain")


def get_topic_context(config: dict[str, Any] | None = None) -> str:
    """M3 检索用的主题上下文字符串。"""
    c = config if config is not None else load_domain_config()
    raw = (c.get("m3_retrieval") or {}).get("topic_context", "")
    # YAML 多行字符串会包含换行，压缩为一行
    return " ".join(str(raw or "").split())


def get_query_boost_terms(config: dict[str, Any] | None = None) -> list[str]:
    """M3 检索优先短语列表。"""
    c = config if config is not None else load_domain_config()
    terms = (c.get("m3_retrieval") or {}).get("query_boost_terms") or []
    return [str(t) for t in terms if t]


def get_saturation_threshold(config: dict[str, Any] | None = None) -> float:
    """低于此值触发 M3 补证的 saturation_score 阈值。"""
    c = config if config is not None else load_domain_config()
    return float((c.get("m3_retrieval") or {}).get("saturation_threshold", 1.5))


def get_m3_defaults(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 M3 gap loop 的所有默认参数。"""
    c = config if config is not None else load_domain_config()
    m3 = c.get("m3_retrieval") or {}
    return {
        "topic_context": get_topic_context(c),
        "query_boost_terms": get_query_boost_terms(c),
        "saturation_threshold": float(m3.get("saturation_threshold", 1.5)),
        "max_claims_per_loop": int(m3.get("max_claims_per_loop", 5)),
        "from_year": int(m3.get("from_year", 2015)),
        "top_k": int(m3.get("top_k", 5)),
        "results_per_backend": int(m3.get("results_per_backend", 5)),
        "max_queries": int(m3.get("max_queries", 3)),
        "references_per_seed": int(m3.get("references_per_seed", 8)),
    }


def get_preferred_visual_types(config: dict[str, Any] | None = None) -> list[str]:
    """M4 优先推荐的图像论证类型列表。"""
    c = config if config is not None else load_domain_config()
    types = (c.get("m4_visual") or {}).get("preferred_visual_types") or []
    return [str(t) for t in types if t]


def get_section_role_keywords(config: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """M4 各章节角色关键词映射。"""
    c = config if config is not None else load_domain_config()
    raw = (c.get("m4_visual") or {}).get("section_role_keywords") or {}
    return {str(k): [str(w) for w in v] for k, v in raw.items() if isinstance(v, list)}


def get_anti_patterns(config: dict[str, Any] | None = None) -> list[str]:
    """M1 导师应提醒避免的平庸写法。"""
    c = config if config is not None else load_domain_config()
    patterns = (c.get("m1_mentor") or {}).get("anti_patterns") or []
    return [str(p) for p in patterns if p]


def get_key_tensions(config: dict[str, Any] | None = None) -> list[str]:
    """领域核心矛盾列表（用于 M1 导师建议）。"""
    c = config if config is not None else load_domain_config()
    tensions = (c.get("m1_mentor") or {}).get("key_tensions") or []
    return [str(t) for t in tensions if t]


def get_review_style_context(config: dict[str, Any] | None = None) -> str:
    """目标发表场景描述（指导写作风格）。"""
    c = config if config is not None else load_domain_config()
    return str((c.get("m1_mentor") or {}).get("review_style_context") or "")
