"""Validated runtime policy for the S2-first topic-scoped stage.

The repository intentionally does not require PyYAML.  The policy shipped in
``config/s2_policy.yaml`` uses a deliberately small YAML subset, so this module
has a strict, dependency-free parser for that subset and will use PyYAML when
it is available.  Invalid policy is never silently replaced with defaults.
Missing *fields* receive explicit defaults; a missing policy file or an
invalid value raises :class:`S2PolicyError`.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class S2PolicyError(ValueError):
    """Raised when the S2 policy cannot be safely used."""


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_S2_POLICY_PATH = PROJECT_ROOT / "config" / "s2_policy.yaml"


# These defaults are part of the runtime contract.  They are intentionally
# explicit so a partially authored policy cannot accidentally turn a safety
# limit into an unbounded request or promote metadata into evidence.
DEFAULT_POLICY_CONFIG: dict[str, Any] = {
    "version": 2,
    "s2_first": {
        "enabled": True,
        "existing_backends_mode": "fallback",
        "use_relevance_search": True,
        "use_title_match": True,
        "use_batch_enrichment": True,
        "use_tldr": True,
        "use_specter2": True,
        "use_snippet_search": True,
        "use_citation_context": True,
        "use_ref_mentions": True,
        "use_recommendations": True,
        "build_literature_graph": True,
        "build_historical_lineage": True,
        "register_s2_body_snippets_as_text_chunks": True,
        "require_local_fulltext_before_using_s2_chunk": False,
        "download_high_value_oa_without_llm_gate": True,
        "requested_roles": [
            "foundation",
            "mechanism",
            "method",
            "frontier",
            "review",
        ],
    },
    "standard": {
        "results_per_query": 300,
        "snippet_results_per_query": 300,
        "precise_snippet_results_per_paper": 100,
        "max_precise_snippet_papers": 300,
        "max_abstract_claim_papers": 300,
        "accepted_s2_text_papers_per_facet": [80, 300],
        "oa_fulltext_downloads_per_facet": [20, 200],
        "target_reference_candidate_range": [120, 200],
        "graph_depth": 2,
        "max_search_queries": 12,
        "max_snippet_queries": 8,
        "max_batch_papers": 500,
    },
    "graph": {
        "seed_count": 5,
        "reference_limit_per_seed": 5,
        "citation_limit_per_seed": 5,
        "recommendation_limit": 12,
    },
    "quality": {
        "title_identity_required": True,
        "s2_body_snippet_min_chars": 500,
        "forbid_tldr_as_abstract": True,
        "distinguish_recommendation_from_citation": True,
    },
    "evidence": {
        "minimum_factual_papers": 1,
        "minimum_factual_chunks": 1,
    },
}


_ROOT_KEYS = frozenset(DEFAULT_POLICY_CONFIG)
_SECTION_KEYS = {
    "s2_first": frozenset(DEFAULT_POLICY_CONFIG["s2_first"]),
    "standard": frozenset(DEFAULT_POLICY_CONFIG["standard"]),
    "graph": frozenset(DEFAULT_POLICY_CONFIG["graph"]),
    "quality": frozenset(DEFAULT_POLICY_CONFIG["quality"]),
    "evidence": frozenset(DEFAULT_POLICY_CONFIG["evidence"]),
}


def _strip_yaml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if char == "\\" and in_double and not escaped:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single and not escaped:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return value[:index].rstrip()
        escaped = False
    return value.rstrip()


def _split_inline(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if char == "\\" and quote == '"' and not escaped:
            escaped = True
            continue
        if char in {"'", '"'} and not quote:
            quote = char
        elif char == quote and not escaped:
            quote = ""
        elif not quote and char in "[{(":
            depth += 1
        elif not quote and char in "]})":
            depth -= 1
        elif not quote and char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
        escaped = False
    if quote or depth != 0:
        raise S2PolicyError(f"invalid inline YAML value: {value!r}")
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        return [_parse_scalar(item) for item in _split_inline(value[1:-1])]
    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise S2PolicyError(f"invalid inline mapping: {value!r}") from exc
        if not isinstance(parsed, dict):
            raise S2PolicyError(f"inline value is not a mapping: {value!r}")
        return parsed
    if value[0:1] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise S2PolicyError(f"invalid quoted YAML scalar: {value!r}") from exc
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError as exc:
            raise S2PolicyError(f"invalid integer scalar: {value!r}") from exc
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        try:
            return float(value)
        except ValueError as exc:
            raise S2PolicyError(f"invalid numeric scalar: {value!r}") from exc
    return value


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by the checked-in policy fixtures."""

    entries: list[tuple[int, str, int]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise S2PolicyError(f"tabs are not allowed for YAML indentation at line {line_number}")
        stripped = _strip_yaml_comment(raw_line).strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        entries.append((indent, stripped, line_number))
    if not entries:
        raise S2PolicyError("policy YAML is empty")

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(entries) or entries[index][0] < indent:
            return {}, index
        if entries[index][0] != indent:
            raise S2PolicyError(
                f"unexpected YAML indentation at line {entries[index][2]}"
            )
        is_list = entries[index][1].startswith("-")
        result: Any = [] if is_list else {}
        while index < len(entries):
            current_indent, content, line_number = entries[index]
            if current_indent < indent:
                break
            if current_indent != indent:
                raise S2PolicyError(
                    f"unexpected YAML indentation at line {line_number}"
                )
            if is_list:
                if not content.startswith("-"):
                    raise S2PolicyError(
                        f"mapping/list mixing at line {line_number}"
                    )
                item_text = content[1:].strip()
                if not item_text:
                    if index + 1 < len(entries) and entries[index + 1][0] > indent:
                        value, index = parse_block(index + 1, entries[index + 1][0])
                    else:
                        value, index = {}, index + 1
                else:
                    value = _parse_scalar(item_text)
                    index += 1
                result.append(value)
                continue

            if content.startswith("-") or ":" not in content:
                raise S2PolicyError(f"expected YAML mapping at line {line_number}")
            key, value_text = content.split(":", 1)
            key = key.strip()
            if not key or key in result:
                raise S2PolicyError(f"duplicate/empty YAML key at line {line_number}")
            value_text = value_text.strip()
            if value_text:
                result[key] = _parse_scalar(value_text)
                index += 1
            elif index + 1 < len(entries) and entries[index + 1][0] > indent:
                result[key], index = parse_block(index + 1, entries[index + 1][0])
            else:
                result[key] = {}
                index += 1
        return result, index

    parsed, next_index = parse_block(0, entries[0][0])
    if next_index != len(entries) or not isinstance(parsed, dict):
        raise S2PolicyError("policy YAML root must be a mapping")
    return parsed


def _load_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise S2PolicyError(f"cannot read S2 policy: {path}") from exc
    if not raw_bytes.strip():
        raise S2PolicyError(f"S2 policy is empty: {path}")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise S2PolicyError(f"S2 policy is not valid UTF-8: {path}") from exc
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError:
        parsed_json = None
    if parsed_json is not None:
        if not isinstance(parsed_json, dict):
            raise S2PolicyError("S2 policy JSON root must be a mapping")
        return parsed_json, digest
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return _parse_minimal_yaml(text), digest
    try:
        parsed = yaml.safe_load(text)
    except Exception as exc:  # pragma: no cover - depends on optional parser
        raise S2PolicyError(f"invalid S2 policy YAML: {path}") from exc
    if not isinstance(parsed, dict):
        raise S2PolicyError("S2 policy YAML root must be a mapping")
    return parsed, digest


def _deep_merge(default: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(default))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise S2PolicyError(f"{name} must be a mapping")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise S2PolicyError(f"{name} must be boolean")
    return value


def _require_int(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise S2PolicyError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" <= {maximum}" if maximum is not None else ""
        raise S2PolicyError(f"{name} must be in [{minimum}{suffix}]")
    return value


def _require_range(value: Any, name: str, *, minimum: int = 0) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise S2PolicyError(f"{name} must be a two-item range")
    low = _require_int(value[0], f"{name}[0]", minimum=minimum)
    high = _require_int(value[1], f"{name}[1]", minimum=minimum)
    if high < low:
        raise S2PolicyError(f"{name}[1] must be >= {name}[0]")
    return low, high


def _validate_unknown_keys(raw: Mapping[str, Any]) -> None:
    unknown_root = set(raw) - _ROOT_KEYS
    if unknown_root:
        raise S2PolicyError(
            "unknown S2 policy section(s): " + ", ".join(sorted(map(str, unknown_root)))
        )
    for section, allowed in _SECTION_KEYS.items():
        value = raw.get(section)
        if value is None:
            continue
        mapping = _require_mapping(value, section)
        unknown = set(mapping) - allowed
        if unknown:
            raise S2PolicyError(
                f"unknown S2 policy key(s) in {section}: "
                + ", ".join(sorted(map(str, unknown)))
            )


@dataclass(frozen=True, slots=True)
class S2Policy:
    """Immutable, typed S2 runtime policy."""

    version: int
    enabled: bool
    existing_backends_mode: str
    feature_flags: dict[str, bool]
    requested_roles: tuple[str, ...]
    results_per_query: int
    snippet_results_per_query: int
    precise_snippet_results_per_paper: int
    max_precise_snippet_papers: int
    max_abstract_claim_papers: int
    accepted_s2_text_papers_per_facet: tuple[int, int]
    oa_fulltext_downloads_per_facet: tuple[int, int]
    target_reference_candidate_range: tuple[int, int]
    graph_depth: int
    graph_seed_count: int
    graph_reference_limit_per_seed: int
    graph_citation_limit_per_seed: int
    graph_recommendation_limit: int
    max_search_queries: int
    max_snippet_queries: int
    max_batch_papers: int
    title_identity_required: bool
    s2_body_snippet_min_chars: int
    forbid_tldr_as_abstract: bool
    distinguish_recommendation_from_citation: bool
    minimum_factual_papers: int
    minimum_factual_chunks: int
    config_path: str
    config_sha256: str
    _config: dict[str, Any]

    @property
    def minimum_target_papers(self) -> int:
        return self.accepted_s2_text_papers_per_facet[0]

    @property
    def maximum_accepted_papers(self) -> int:
        return self.accepted_s2_text_papers_per_facet[1]

    @property
    def maximum_oa_downloads(self) -> int:
        return self.oa_fulltext_downloads_per_facet[1]

    def feature_enabled(self, name: str, *, default: bool = False) -> bool:
        return bool(self.feature_flags.get(name, default))

    @property
    def standard(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._config["standard"]))

    @property
    def s2_first(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._config["s2_first"]))

    @property
    def quality(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._config["quality"]))

    @property
    def graph(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._config["graph"]))

    @property
    def evidence(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._config["evidence"]))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._config)


def load_s2_policy(path: str | Path | None = None) -> S2Policy:
    """Load and validate ``config/s2_policy.yaml``.

    Defaults are applied only for omitted fields.  Invalid types, ranges,
    unknown keys, and missing files fail closed with :class:`S2PolicyError`.
    """

    policy_path = Path(path) if path is not None else DEFAULT_S2_POLICY_PATH
    if not policy_path.is_file():
        raise S2PolicyError(f"S2 policy file does not exist: {policy_path}")
    raw, digest = _load_document(policy_path)
    _validate_unknown_keys(raw)
    config = _deep_merge(DEFAULT_POLICY_CONFIG, raw)

    version = _require_int(config.get("version"), "version", minimum=1)
    if version != 2:
        raise S2PolicyError(f"unsupported S2 policy version: {version}")

    s2_first = _require_mapping(config["s2_first"], "s2_first")
    enabled = _require_bool(s2_first["enabled"], "s2_first.enabled")
    mode = s2_first["existing_backends_mode"]
    if not isinstance(mode, str) or mode not in {"fallback", "s2_only", "disabled"}:
        raise S2PolicyError(
            "s2_first.existing_backends_mode must be fallback, s2_only, or disabled"
        )
    feature_names = [
        name
        for name in s2_first
        if name.startswith("use_") or name.startswith("build_") or name.startswith("register_") or name.startswith("require_") or name.startswith("download_")
    ]
    feature_flags = {
        name: _require_bool(s2_first[name], f"s2_first.{name}")
        for name in feature_names
    }
    roles_raw = s2_first.get("requested_roles")
    if not isinstance(roles_raw, list) or not roles_raw or any(
        not isinstance(item, str) or not item.strip() for item in roles_raw
    ):
        raise S2PolicyError("s2_first.requested_roles must be a non-empty string list")
    requested_roles = tuple(dict.fromkeys(item.strip() for item in roles_raw))

    standard = _require_mapping(config["standard"], "standard")
    results_per_query = _require_int(
        standard["results_per_query"], "standard.results_per_query", minimum=1, maximum=10000
    )
    snippet_results_per_query = _require_int(
        standard["snippet_results_per_query"],
        "standard.snippet_results_per_query",
        minimum=1,
        maximum=1000,
    )
    precise_snippet_results_per_paper = _require_int(
        standard["precise_snippet_results_per_paper"],
        "standard.precise_snippet_results_per_paper",
        minimum=1,
        maximum=1000,
    )
    max_precise_snippet_papers = _require_int(
        standard["max_precise_snippet_papers"],
        "standard.max_precise_snippet_papers",
        minimum=1,
        maximum=10000,
    )
    max_abstract_claim_papers = _require_int(
        standard["max_abstract_claim_papers"],
        "standard.max_abstract_claim_papers",
        minimum=1,
        maximum=10000,
    )
    accepted_range = _require_range(
        standard["accepted_s2_text_papers_per_facet"],
        "standard.accepted_s2_text_papers_per_facet",
        minimum=1,
    )
    oa_range = _require_range(
        standard["oa_fulltext_downloads_per_facet"],
        "standard.oa_fulltext_downloads_per_facet",
    )
    reference_range = _require_range(
        standard["target_reference_candidate_range"],
        "standard.target_reference_candidate_range",
    )
    graph_depth = _require_int(standard["graph_depth"], "standard.graph_depth", maximum=8)
    max_search_queries = _require_int(
        standard["max_search_queries"], "standard.max_search_queries", minimum=1, maximum=1000
    )
    max_snippet_queries = _require_int(
        standard["max_snippet_queries"], "standard.max_snippet_queries", minimum=1, maximum=1000
    )
    max_batch_papers = _require_int(
        standard["max_batch_papers"], "standard.max_batch_papers", minimum=1, maximum=500
    )

    graph = _require_mapping(config["graph"], "graph")
    graph_seed_count = _require_int(graph["seed_count"], "graph.seed_count", maximum=500)
    graph_reference_limit = _require_int(
        graph["reference_limit_per_seed"], "graph.reference_limit_per_seed", maximum=1000
    )
    graph_citation_limit = _require_int(
        graph["citation_limit_per_seed"], "graph.citation_limit_per_seed", maximum=1000
    )
    graph_recommendation_limit = _require_int(
        graph["recommendation_limit"], "graph.recommendation_limit", maximum=1000
    )

    quality = _require_mapping(config["quality"], "quality")
    title_identity_required = _require_bool(
        quality["title_identity_required"], "quality.title_identity_required"
    )
    snippet_min_chars = _require_int(
        quality["s2_body_snippet_min_chars"],
        "quality.s2_body_snippet_min_chars",
        minimum=100,
        maximum=1000000,
    )
    forbid_tldr = _require_bool(
        quality["forbid_tldr_as_abstract"], "quality.forbid_tldr_as_abstract"
    )
    distinguish_recommendation = _require_bool(
        quality["distinguish_recommendation_from_citation"],
        "quality.distinguish_recommendation_from_citation",
    )

    evidence = _require_mapping(config["evidence"], "evidence")
    minimum_factual_papers = _require_int(
        evidence["minimum_factual_papers"],
        "evidence.minimum_factual_papers",
        minimum=1,
        maximum=500,
    )
    minimum_factual_chunks = _require_int(
        evidence["minimum_factual_chunks"],
        "evidence.minimum_factual_chunks",
        minimum=1,
        maximum=100000,
    )

    return S2Policy(
        version=version,
        enabled=enabled,
        existing_backends_mode=mode,
        feature_flags=feature_flags,
        requested_roles=requested_roles,
        results_per_query=results_per_query,
        snippet_results_per_query=snippet_results_per_query,
        precise_snippet_results_per_paper=precise_snippet_results_per_paper,
        max_precise_snippet_papers=max_precise_snippet_papers,
        max_abstract_claim_papers=max_abstract_claim_papers,
        accepted_s2_text_papers_per_facet=accepted_range,
        oa_fulltext_downloads_per_facet=oa_range,
        target_reference_candidate_range=reference_range,
        graph_depth=graph_depth,
        graph_seed_count=graph_seed_count,
        graph_reference_limit_per_seed=graph_reference_limit,
        graph_citation_limit_per_seed=graph_citation_limit,
        graph_recommendation_limit=graph_recommendation_limit,
        max_search_queries=max_search_queries,
        max_snippet_queries=max_snippet_queries,
        max_batch_papers=max_batch_papers,
        title_identity_required=title_identity_required,
        s2_body_snippet_min_chars=snippet_min_chars,
        forbid_tldr_as_abstract=forbid_tldr,
        distinguish_recommendation_from_citation=distinguish_recommendation,
        minimum_factual_papers=minimum_factual_papers,
        minimum_factual_chunks=minimum_factual_chunks,
        config_path=str(policy_path),
        config_sha256=digest,
        _config=config,
    )


# Short aliases keep the future integration surface discoverable without
# duplicating policy-loading behavior.
load_policy = load_s2_policy


__all__ = [
    "DEFAULT_POLICY_CONFIG",
    "DEFAULT_S2_POLICY_PATH",
    "S2Policy",
    "S2PolicyError",
    "load_policy",
    "load_s2_policy",
]
