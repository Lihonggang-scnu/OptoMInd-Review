"""Production adapter for the supplementary retrieval foundation.

This module wires the accepted offline foundation to the real chain behind
injected callbacks:

  task context -> bounded Qwen query generation -> stage-1/stage-2 dedup inside
  SupplementaryRetrievalService -> S2/OA/abstract bootstrap on a task-local
  EMPTY_TASK_SEED -> task-local claim-centered packets -> material proposition
  cards -> material units -> task-local vectors/annotations.

Everything written by this adapter lives under the deterministic task work
directory derived from the service ``idempotency_key``.  The canonical
long-term 287-card library, blueprint, writer, and formal finalization paths
are never touched.  Visual tasks are first-class in the queue but fail closed
unless visual callbacks are injected.

Qwen prompts are loaded from external files under ``prompts/``.  No fallback
model is used.  Raw ``_llm_usage`` and embedding usage are recorded without
inventing prices.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .supplementary_query_dedup import AmbiguousGroup
from .supplementary_retrieval_contract import (
    ContextRegistry,
    SupplementaryExpansionPolicy,
    SupplementaryRetrievalTask,
    project_context_for_task,
    resolve_expansion_policy,
    task_fingerprint,
)
from .supplementary_retrieval_service import (
    MaterializationOutcome,
    RetrievalOutcome,
    ServiceCallbacks,
    SubmissionResult,
    SupplementaryRetrievalService,
)


PIPELINE_SCHEMA_VERSION = "supplementary_retrieval.pipeline.v1"
QUERY_PLAN_SCHEMA_VERSION = "supplementary_retrieval.query_plan.v1"
RELEVANCE_FILTER_SCHEMA_VERSION = "supplementary_retrieval.relevance_filter.v1"

DEFAULT_GENERATOR_PROMPT = (
    Path(__file__).resolve().parents[2]
    / "prompts"
    / "Supplementary Query Generator.txt"
)
DEFAULT_ADJUDICATOR_PROMPT = (
    Path(__file__).resolve().parents[2]
    / "prompts"
    / "Supplementary Query Deduplicator.txt"
)

DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_REPRESENTATION_VERSION = "material-unit-surrogate.v1"

# Generic stopwords/workflow vocabulary that cannot prove a query carries the
# broad research background.  A query sharing only such a token (or only a
# method label such as PINN/simulation) is still a naked gap direction and is
# repaired before durable dedup/submission.
_BACKGROUND_CUE_GENERIC_TERMS = frozenset(
    {
        "a",
        "an",
        "and",
        "approach",
        "approaches",
        "are",
        "as",
        "at",
        "based",
        "be",
        "between",
        "by",
        "can",
        "current",
        "design",
        "for",
        "from",
        "in",
        "including",
        "into",
        "is",
        "it",
        "may",
        "method",
        "methods",
        "of",
        "on",
        "or",
        "paper",
        "papers",
        "performance",
        "research",
        "review",
        "reviews",
        "should",
        "state",
        "study",
        "studies",
        "such",
        "system",
        "systems",
        "that",
        "the",
        "this",
        "to",
        "using",
        "via",
        "was",
        "were",
        "with",
        "within",
    }
)
# Method-family labels are identity anchors but not domain-qualifying by
# themselves: "PINN near-field error" must still be repaired with the broad
# optical/electromagnetic background before durable dedup.
_BACKGROUND_CUE_METHOD_TERMS = frozenset(
    {
        "adjoint",
        "algorithm",
        "ann",
        "automatic",
        "cnn",
        "differentiable",
        "differentiation",
        "dspsa",
        "fdtd",
        "fem",
        "framework",
        "gan",
        "gans",
        "gradient",
        "informed",
        "inverse",
        "learning",
        "lstm",
        "machine",
        "mlp",
        "model",
        "modeling",
        "models",
        "modelling",
        "neural",
        "network",
        "optimization",
        "optimizer",
        "physic",
        "physics",
        "pinn",
        "pinns",
        "rf",
        "rnn",
        "simulation",
        "solver",
        "solvers",
        "surrogate",
        "svm",
        "training",
    }
)
_BACKGROUND_CUE_MIN_DISTINCTIVE_SHARED = 2
# Semantic qualification threshold: a generated query whose embedding is at
# least this similar to the broad background is kept as-is; below it the
# bounded background cue is prefixed (tail preserved).  This is deliberately
# loose; it only guards against naked cross-domain gap directions.
DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD = 0.72
# Search-engine structural guardrails.  Generated queries must be compact
# Boolean-free keyword strings: a bounded number of high-signal terms, with
# only a few function/prose words.  Long natural-language background sentences
# are rejected by this mechanism instead of being sent to S2.
_GENERATED_QUERY_MIN_TERMS = 4
_GENERATED_QUERY_MAX_TERMS = 12
_GENERATED_QUERY_MAX_METHOD_ANCHORS = 2
_GENERATED_QUERY_MAX_PROSE_WORDS = 6
# One bounded cheap-model correction retry is allowed when the first Qwen
# output violates only the compact-search structure; malformed JSON and all
# other validation errors are never retried.
_GENERATED_QUERY_CORRECTION_MODEL_TIER = "cheap_model"
_GENERATED_QUERY_MAX_COUNT = 8
_GENERATED_QUERY_PROSE_WORDS = frozenset(
    {
        "a",
        "about",
        "across",
        "after",
        "also",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "before",
        "between",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "including",
        "into",
        "is",
        "may",
        "more",
        "most",
        "not",
        "of",
        "on",
        "or",
        "should",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "using",
        "via",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "why",
        "with",
        "within",
        "would",
    }
)
# Compact keyword prefix used by the lexical/semantic repair fallback: the
# broad background is reduced to at most one non-method, non-generic domain
# anchor (for example ``electromagnetic`` or ``optical``).  Injecting every
# context term into S2 overconstrains the search and returns zero results.
_GENERATED_QUERY_MAX_COMPACT_PREFIX_ANCHORS = 1


class QueryGenerationError(ValueError):
    """Raised when the query generator returns malformed/empty output."""


class VisualPipelineUnsupportedError(RuntimeError):
    """Raised when a visual task reaches the text-only pipeline adapter."""


@dataclass(slots=True)
class QueryGenerationResult:
    """Validated output of the supplementary query generator."""

    queries: list[str]
    records: list[dict[str, Any]]
    usage: dict[str, Any] = field(default_factory=dict)
    assignment: list[dict[str, Any]] = field(default_factory=list)


class PipelineUsage:
    """Raw usage accumulator; no cost is ever invented."""

    def __init__(self) -> None:
        self.query_generation: list[dict[str, Any]] = []
        self.adjudication: list[dict[str, Any]] = []
        self.material_cards: list[dict[str, Any]] = []
        self.embedding: dict[str, int] = {"input_tokens": 0, "request_count": 0}
        self.s2_telemetry: list[dict[str, Any]] = []

    def record_query_generation(self, usage: Mapping[str, Any] | None) -> None:
        if isinstance(usage, Mapping) and usage:
            self.query_generation.append(dict(usage))

    def record_adjudication(self, usage: Mapping[str, Any] | None) -> None:
        if isinstance(usage, Mapping) and usage:
            self.adjudication.append(dict(usage))

    def record_material_cards(self, usage: Mapping[str, Any] | None) -> None:
        if isinstance(usage, Mapping) and usage:
            self.material_cards.append(dict(usage))

    def record_embedding(
        self, usage: Mapping[str, Any] | None
    ) -> None:
        if not isinstance(usage, Mapping):
            return
        self.embedding["input_tokens"] += max(
            0, int(usage.get("input_tokens") or 0)
        )
        self.embedding["request_count"] += max(
            0, int(usage.get("request_count") or 0)
        )

    def record_s2_telemetry(self, telemetry: Mapping[str, Any] | None) -> None:
        if isinstance(telemetry, Mapping) and telemetry:
            self.s2_telemetry.append(dict(telemetry))

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_generation": list(self.query_generation),
            "adjudication": list(self.adjudication),
            "material_cards": list(self.material_cards),
            "embedding": dict(self.embedding),
            "s2_telemetry": list(self.s2_telemetry),
        }


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _unique(values: Sequence[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        )
    )


class SemanticRelevanceError(RuntimeError):
    """Raised when embedding is unavailable; callers fall back lexically."""


class SemanticRelevanceUsage:
    """Truthful, separately inspectable usage for supplementary semantic matching."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.request_count = 0
        self.embed_calls = 0
        self.vector_count = 0
        self.failure_count = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": int(self.input_tokens),
            "request_count": int(self.request_count),
            "embed_calls": int(self.embed_calls),
            "vector_count": int(self.vector_count),
            "failure_count": int(self.failure_count),
        }


def _cosine_similarity(
    left: Sequence[float] | None,
    right: Sequence[float] | None,
) -> float:
    """Safe cosine similarity; mismatched or empty vectors score 0.0."""

    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right):
        left_value = float(left_value or 0.0)
        right_value = float(right_value or 0.0)
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if not left_norm or not right_norm:
        return 0.0
    return round(dot / (math.sqrt(left_norm) * math.sqrt(right_norm)), 6)


class SupplementarySemanticEngine:
    """Batched cosine-similarity engine for supplementary retrieval.

    Production default uses the existing ``text-embedding-v4`` /
    ``dashscope_embedder`` path.  An injected fake embedder is used by
    offline tests.  Vectors are cached by normalized text so repeat
    comparisons within one pipeline instance never re-embed cached texts.
    Embedding failures are surfaced as :class:`SemanticRelevanceError` so the
    decision layer can audit a lexical fallback instead of failing the task.
    """

    def __init__(
        self,
        embedder: Callable[..., Any] | None = None,
        *,
        usage: SemanticRelevanceUsage | None = None,
        max_batch_size: int = 64,
    ) -> None:
        self._embedder = embedder or _default_embedder
        self._usage = usage or SemanticRelevanceUsage()
        self._cache: dict[str, list[float]] = {}
        self._max_batch_size = max(1, int(max_batch_size))

    @property
    def usage(self) -> SemanticRelevanceUsage:
        return self._usage

    def embed_texts(
        self, texts: Sequence[str]
    ) -> dict[str, list[float]]:
        """Embed unseen texts in one or more bounded batches; cache by norm."""

        normalized: list[str] = []
        unique_texts: list[str] = []
        unique_norms: list[str] = []
        seen: set[str] = set()
        for text in texts:
            norm = _norm_text(text)
            normalized.append(norm)
            if norm and norm not in seen and norm not in self._cache:
                seen.add(norm)
                unique_norms.append(norm)
                unique_texts.append(str(text))
        for start in range(0, len(unique_texts), self._max_batch_size):
            batch = unique_texts[start : start + self._max_batch_size]
            batch_norms = unique_norms[start : start + self._max_batch_size]
            local_usage: dict[str, int] = {
                "input_tokens": 0,
                "request_count": 0,
            }
            try:
                vectors = self._embedder(
                    batch, usage_accumulator=local_usage
                )
            except Exception as exc:
                self._usage.failure_count += 1
                raise SemanticRelevanceError(
                    f"embedding_failed:{type(exc).__name__}"
                ) from exc
            if not isinstance(vectors, (list, tuple)) or len(vectors) != len(
                batch
            ):
                raise SemanticRelevanceError(
                    "embedding_response_count_mismatch"
                )
            self._usage.input_tokens += max(
                0, int(local_usage.get("input_tokens") or 0)
            )
            self._usage.request_count += max(
                0, int(local_usage.get("request_count") or 0)
            )
            self._usage.embed_calls += 1
            self._usage.vector_count += len(vectors)
            for norm, vector in zip(batch_norms, vectors):
                if not isinstance(vector, (list, tuple)) or not vector:
                    raise SemanticRelevanceError("empty_embedding_vector")
                self._cache[norm] = [float(value) for value in vector]
        return {
            norm: self._cache[norm] for norm in normalized if norm
        }

    def cosine(self, left: str, right: str) -> float:
        """Cosine similarity between two texts (cached embedding lookup)."""

        left_norm = _norm_text(left)
        right_norm = _norm_text(right)
        if not left_norm or not right_norm:
            return 0.0
        vectors = self.embed_texts([left, right])
        return _cosine_similarity(
            vectors.get(left_norm), vectors.get(right_norm)
        )


def _background_cue_tokens(cue: str) -> set[str]:
    """Deterministic distinctive tokens for the compact background cue.

    Generic stopwords/workflow words and method-family labels are excluded:
    sharing only ``research``, ``simulation``, or ``PINN`` is not evidence
    that a query carries the broad optical/electromagnetic background.
    """

    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", _norm_text(cue))
        if token not in _BACKGROUND_CUE_GENERIC_TERMS
        and token not in _BACKGROUND_CUE_METHOD_TERMS
    }


def _compact_search_keywords(
    cue: str,
    *,
    max_anchors: int = _GENERATED_QUERY_MAX_COMPACT_PREFIX_ANCHORS,
) -> str:
    """Reduce a background cue to at most one compact domain search anchor.

    The full cue remains the semantic context for similarity and audit; this
    single high-signal anchor is what actually gets prefixed when a naked gap
    query needs repair, so the final S2 query string stays search-engine-ready
    instead of becoming a copied prose background sentence or an
    overconstrained multi-term search.  Method labels (PINN, solver,
    simulation, ...) and generic workflow words are deliberately excluded.
    """

    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"\s+", _text(cue)):
        token = str(raw or "").strip(".,;:!?()[]\"'")
        if not token or not any(char.isalnum() for char in token):
            continue
        norm = token.casefold()
        if norm in _BACKGROUND_CUE_GENERIC_TERMS:
            continue
        if norm in _BACKGROUND_CUE_METHOD_TERMS:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        terms.append(token)
        if len(terms) >= max(1, int(max_anchors)):
            break
    return " ".join(terms)


def _query_contains_background_anchor(query: str, prefix: str) -> bool:
    """Return whether a query already contains every anchor token.

    Repair is idempotent: a query that already carries the selected domain
    anchor (for example ``electromagnetic``) is never prefixed with it again,
    even if semantic similarity is still below the qualification threshold on
    a re-entrant call (pre-submit qualification and plan-build qualification
    share this function).
    """

    if not prefix:
        return False
    query_tokens = set(re.findall(r"[a-z0-9]+", _norm_text(query)))
    prefix_tokens = set(re.findall(r"[a-z0-9]+", _norm_text(prefix)))
    return bool(prefix_tokens) and prefix_tokens <= query_tokens


def _generated_query_prose_reason(query: str) -> str:
    """Return a structural reason if a query is prose-like, else empty."""

    terms = [
        token
        for token in re.split(r"\s+", _text(query))
        if any(char.isalnum() for char in token)
    ]
    if not terms:
        return "query_has_no_search_terms"
    if len(terms) > _GENERATED_QUERY_MAX_TERMS:
        return (
            f"query_has_{len(terms)}_terms_above_compact_limit_"
            f"{_GENERATED_QUERY_MAX_TERMS}"
        )
    prose_words = [
        token
        for token in terms
        if token.casefold().strip(".,;:!?()[]\"'")
        in _GENERATED_QUERY_PROSE_WORDS
    ]
    if len(prose_words) >= _GENERATED_QUERY_MAX_PROSE_WORDS:
        return (
            f"query_has_{len(prose_words)}_prose_words_above_compact_limit_"
            f"{_GENERATED_QUERY_MAX_PROSE_WORDS}"
        )
    if (
        len(terms) >= 8
        and len(prose_words) / len(terms) > 0.4
    ):
        return "query_prose_word_ratio_too_high"
    return ""


def _generated_query_compact_structure_reasons(query: str) -> list[str]:
    """Return compact-search structural violations for one query.

    A valid search query is a compact set of 4-12 high-signal terms with at
    most two broad background/method anchors; the remaining terms describe a
    concrete missing fact.  These are the only retryable failures: the first
    Qwen output may be corrected once with the structural reason.
    """

    terms = [
        token
        for token in re.split(r"\s+", _text(query))
        if any(char.isalnum() for char in token)
    ]
    reasons: list[str] = []
    if not terms:
        return ["query_has_no_search_terms"]
    if len(terms) < _GENERATED_QUERY_MIN_TERMS:
        reasons.append(
            f"query_has_{len(terms)}_terms_below_compact_min_"
            f"{_GENERATED_QUERY_MIN_TERMS}"
        )
    if len(terms) > _GENERATED_QUERY_MAX_TERMS:
        reasons.append(
            f"query_has_{len(terms)}_terms_above_compact_limit_"
            f"{_GENERATED_QUERY_MAX_TERMS}"
        )
    prose_reason = _generated_query_prose_reason(query)
    if prose_reason and prose_reason not in reasons:
        reasons.append(prose_reason)
    return reasons


def _generated_query_soft_structure_warnings(query: str) -> list[str]:
    """Non-blocking format preferences; auditable but not retry-worthy.

    Exceeding the preferred method-anchor count is a soft warning: the query
    is still searchable and must not deadlock a chapter over a formatting
    preference.  Hard failures are reserved for content that cannot sensibly
    be searched (empty, malformed, overlong/prose-heavy, or unusable forms).
    """
    terms = [
        token
        for token in re.split(r"\s+", _text(query))
        if any(char.isalnum() for char in token)
    ]
    if not terms:
        return []
    method_anchor_count = sum(
        1
        for token in terms
        if token.casefold().strip(".,;:!?()[]\"'")
        in _BACKGROUND_CUE_METHOD_TERMS
    )
    if method_anchor_count > _GENERATED_QUERY_MAX_METHOD_ANCHORS:
        return [
            f"query_has_{method_anchor_count}_method_anchors_above_limit_"
            f"{_GENERATED_QUERY_MAX_METHOD_ANCHORS}"
        ]
    return []


def _soft_structure_warnings_for_query_strings(
    queries: Sequence[str],
) -> list[str]:
    """Aggregate unique non-blocking structure warnings across queries."""
    warnings: list[str] = []
    for query in queries:
        warnings.extend(
            _generated_query_soft_structure_warnings(str(query or ""))
        )
    return list(dict.fromkeys(warnings))


def _build_search_background_cue(
    resolved_context: Mapping[str, Any], *, max_chars: int = 240
) -> str:
    """Derive a compact broad research background cue.

    The cue is primarily ``topic_scope.main_scope``; a bounded small number of
    inclusion/lens/axis/section-task items is used only when main scope is
    absent.  It never contains the full user question, and it is not expanded
    into separate search queries; it is the shared background context for
    query generation and candidate relevance auditing.
    """

    topic_scope = resolved_context.get("topic_scope")
    topic_scope = topic_scope if isinstance(topic_scope, dict) else {}
    main_scope = _text(
        topic_scope.get("main_scope") or topic_scope.get("topic")
    )
    if main_scope:
        return main_scope[:max_chars].strip()
    segments: list[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text:
            segments.append(text)

    add(topic_scope.get("main_scope") or topic_scope.get("topic"))
    for key in ("lenses", "research_lenses"):
        for item in topic_scope.get(key) or []:
            add(item)
    for item in (resolved_context.get("dynamic_axes") or [])[:3]:
        if isinstance(item, dict):
            add(item.get("description") or item.get("title"))
    section_task = resolved_context.get("section_task")
    if isinstance(section_task, dict):
        add(section_task.get("title"))
    cue = ""
    seen_segments: set[str] = set()
    for segment in segments:
        key = segment.casefold()
        if key in seen_segments:
            continue
        seen_segments.add(key)
        candidate = (cue + " " + segment).strip()
        if len(candidate) > max_chars:
            break
        cue = candidate
    return cue[:max_chars].strip()


def _apply_search_background_cue(
    records: Sequence[Mapping[str, Any]],
    cue: str,
    *,
    semantic_engine: SupplementarySemanticEngine | None = None,
    semantic_threshold: float = DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD,
) -> list[dict[str, Any]]:
    """Safely repair a naked gap query with the compact background cue.

    Semantic similarity is the primary qualification signal: a query whose
    embedding is at least ``semantic_threshold`` similar to the broad research
    background is kept; otherwise the bounded background cue is prefixed
    (never the full user question, and never split into separate searches).
    When semantic vectors are unavailable the deterministic
    ``>= 2`` distinctive-domain-token rule is the lexical fallback; a single
    ordinary or method token is never sufficient.  Every final query is
    audited with its qualification mode, similarity, threshold, and fallback
    error code.
    """

    cue = _text(cue)
    if not cue:
        return [dict(record) for record in records]
    if not any(
        _text(record.get("query") or record.get("text"))
        for record in records
        if isinstance(record, Mapping)
    ):
        return [dict(record) for record in records]
    cue_tokens = _background_cue_tokens(cue)
    background_prefix = _compact_search_keywords(cue)
    qualification_mode = "lexical_fallback"
    fallback_error_code = ""
    semantic_similarity_by_norm: dict[str, float] = {}
    if semantic_engine is not None:
        query_texts = [
            _text(record.get("query") or record.get("text"))
            for record in records
            if isinstance(record, Mapping)
        ]
        query_texts = [
            text for text in query_texts if text
        ]
        try:
            vectors = semantic_engine.embed_texts([cue, *query_texts])
            cue_norm = _norm_text(cue)
            cue_vector = vectors.get(cue_norm)
            semantic_similarity_by_norm = {
                norm: _cosine_similarity(cue_vector, vector)
                for norm, vector in vectors.items()
            }
            qualification_mode = "semantic"
        except Exception as exc:
            qualification_mode = "lexical_fallback"
            fallback_error_code = (
                f"{type(exc).__name__}:{exc}"[:160]
            )
    repaired: list[dict[str, Any]] = []
    for record in records:
        record = dict(record)
        query = _text(record.get("query") or record.get("text"))
        applied = False
        anchor_already_present = _query_contains_background_anchor(
            query, background_prefix
        )
        lexical_qualified = (
            len(_background_cue_tokens(query) & cue_tokens)
            >= _BACKGROUND_CUE_MIN_DISTINCTIVE_SHARED
        )
        semantic_similarity: float | None = None
        if query and qualification_mode == "semantic":
            semantic_similarity = semantic_similarity_by_norm.get(
                _norm_text(query), 0.0
            )
            qualified = semantic_similarity >= float(semantic_threshold)
        else:
            qualified = lexical_qualified
        if query and not qualified and not anchor_already_present:
            # Preserve the precise gap direction: when the qualified string is
            # over the bound, shorten the background prefix, never the query
            # tail.
            merged = _text(f"{background_prefix} {query}")
            if len(merged) > 240:
                keep = max(0, 240 - len(query) - 1)
                cue_prefix = _text(background_prefix[:keep])
                merged = (
                    _text(f"{cue_prefix} {query}")
                    if cue_prefix
                    else query
                )
            if merged and merged != query:
                query = merged
                applied = True
        repaired_record = dict(record)
        if "query" in record or "text" in record:
            repaired_record["query"] = query
            repaired_record["text"] = query
            repaired_record["background_cue_applied"] = applied
            repaired_record["qualification_mode"] = qualification_mode
            repaired_record["semantic_similarity"] = (
                round(semantic_similarity, 6)
                if semantic_similarity is not None
                else None
            )
            repaired_record["semantic_threshold"] = (
                float(semantic_threshold)
                if qualification_mode == "semantic"
                else None
            )
            repaired_record["fallback_error_code"] = fallback_error_code
            repaired_record["background_prefix_used"] = (
                background_prefix
                if (applied or anchor_already_present)
                else None
            )
            repaired_record["anchor_already_present"] = (
                anchor_already_present
            )
        repaired.append(repaired_record)
    return repaired


def load_prompt_text(path: str | Path) -> str:
    """Load an external prompt file; missing prompts are a hard error."""

    prompt_path = Path(path)
    if not prompt_path.is_file():
        raise QueryGenerationError(f"prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _parse_json_object(content: Any, *, error_type: type[Exception]) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise error_type(f"model output is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise error_type("model output must be a JSON object")
    return parsed


def _parse_query_strings(payload: Mapping[str, Any]) -> list[str]:
    """Validate the minimal Qwen string-only contract.

    Legacy/test-only fallback: production uses the named fill-form contract.
    Qwen emits exactly ``{"queries": ["query one", "query two"]}``.  Schema
    errors (wrong count, non-string entries, empty or over-long strings) are
    hard failures and are never retried.  Compact-search structure is checked
    separately so it can trigger the one bounded correction retry.
    """

    rows = payload.get("queries")
    if not isinstance(rows, list) or not (
        2 <= len(rows) <= _GENERATED_QUERY_MAX_COUNT
    ):
        raise QueryGenerationError(
            "generator must return between 2 and 8 queries"
        )
    queries: list[str] = []
    for row in rows:
        if not isinstance(row, str):
            raise QueryGenerationError(
                "each generated query must be a plain string"
            )
        query = _text(row)
        if not query or len(query) > 240:
            raise QueryGenerationError(
                "each generated query must be a non-empty string <= 240 chars"
            )
        queries.append(query)
    return queries


def _structural_errors_for_query_strings(
    queries: Sequence[str],
) -> list[str]:
    """Return retryable compact-search structure errors across queries."""

    errors: list[str] = []
    for query in queries:
        errors.extend(
            _generated_query_compact_structure_reasons(str(query or ""))
        )
    return list(dict.fromkeys(errors))


def _mapping_tokens(text: str) -> set[str]:
    """Deterministic non-generic token set for lexical coverage matching."""

    return {
        token
        for token in re.findall(r"[a-z0-9]+", _norm_text(text))
        if token not in _BACKGROUND_CUE_GENERIC_TERMS
    }


def _eligible_coverage_targets(
    catalog: Sequence[Mapping[str, Any]],
    gap_type: str,
) -> list[dict[str, Any]]:
    """Return task-specific eligible targets ordered by priority.

    This is a local routing preference, not a relevance filter: the eligible
    set is chosen per gap type, and within it the best semantic match always
    wins without any rejection threshold.
    """

    preferred_types: set[str] = set()
    if gap_type in {"claim_evidence_gap", "section_argument_gap"}:
        preferred_types = {"missing_fact"}
    elif gap_type == "review_structure_gap":
        preferred_types = {"structure", "excerpt", "reviewer", "revision"}
    elif gap_type == "whole_review_gap":
        preferred_types = {"whole_review", "axis"}
    elif gap_type == "visual_material_gap":
        preferred_types = {"visual"}
    eligible = [
        dict(entry)
        for entry in catalog
        if isinstance(entry, Mapping)
        and (
            not preferred_types
            or str(entry.get("target_type") or "") in preferred_types
        )
    ]
    if not eligible:
        eligible = [
            dict(entry)
            for entry in catalog
            if isinstance(entry, Mapping)
        ]
    eligible.sort(
        key=lambda entry: (
            -int(entry.get("priority") or 0),
            str(entry.get("coverage_id") or ""),
        )
    )
    return eligible


def _assign_query_coverage_batch(
    queries: Sequence[str],
    *,
    background_cue: str,
    catalog: Sequence[Mapping[str, Any]],
    gap_type: str,
    semantic_engine: SupplementarySemanticEngine | None,
) -> list[dict[str, Any]]:
    """Assign each query to the best eligible coverage target.

    All queries, the background cue, and eligible target descriptions are
    embedded in one batched cached call so later background qualification
    reuses the vectors where practical.  No fixed relevance threshold is
    applied: the best eligible target always wins.  On embedding failure a
    deterministic weighted lexical-overlap fallback is used and audited.
    """

    eligible = _eligible_coverage_targets(catalog, gap_type)
    assignments: list[dict[str, Any]] = []
    if not eligible:
        return [
            {
                "coverage_id": "",
                "description": "",
                "similarity": None,
                "mode": "no_targets",
                "fallback_error_code": "",
            }
            for _query in queries
        ]
    mode = "semantic"
    fallback_error_code = ""
    vectors: dict[str, list[float]] = {}
    if semantic_engine is not None:
        try:
            vectors = semantic_engine.embed_texts(
                [
                    background_cue,
                    *queries,
                    *(str(entry.get("description") or "") for entry in eligible),
                ]
            )
        except Exception as exc:
            mode = "lexical_fallback"
            fallback_error_code = (
                f"{type(exc).__name__}:{exc}"[:160]
            )
    else:
        mode = "lexical_fallback"
        fallback_error_code = "semantic_engine_unavailable"
    for query in queries:
        if mode == "semantic":
            query_vector = vectors.get(_norm_text(query))

            def semantic_key(entry: Mapping[str, Any]) -> float:
                return _cosine_similarity(
                    query_vector,
                    vectors.get(
                        _norm_text(str(entry.get("description") or ""))
                    ),
                )

            best = max(eligible, key=semantic_key)
            similarity = semantic_key(best)
        else:
            query_tokens = _mapping_tokens(query)

            def lexical_key(entry: Mapping[str, Any]) -> tuple[float, int]:
                overlap = len(
                    query_tokens
                    & _mapping_tokens(
                        str(entry.get("description") or "")
                    )
                )
                return (
                    float(overlap) + int(entry.get("priority") or 0) / 1000.0,
                    -int(entry.get("priority") or 0),
                )

            best = max(eligible, key=lexical_key)
            similarity = None
        assignments.append(
            {
                "coverage_id": str(best.get("coverage_id") or ""),
                "description": str(best.get("description") or ""),
                "similarity": (
                    round(float(similarity), 6)
                    if similarity is not None
                    else None
                ),
                "mode": mode,
                "fallback_error_code": fallback_error_code,
            }
        )
    return assignments


def _records_from_query_strings(
    queries: Sequence[str],
    *,
    background_cue: str,
    catalog: Sequence[Mapping[str, Any]],
    gap_type: str,
    semantic_engine: SupplementarySemanticEngine | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Construct existing full internal records plus auditable assignments.

    Legacy/test-only fallback with semantic assignment: production routing
    uses the explicit named fill-form and never re-routes a valid field.
    """

    assignments = _assign_query_coverage_batch(
        queries,
        background_cue=background_cue,
        catalog=catalog,
        gap_type=gap_type,
        semantic_engine=semantic_engine,
    )
    records: list[dict[str, Any]] = []
    for query, assignment in zip(queries, assignments):
        coverage_id = str(assignment.get("coverage_id") or "")
        description = str(assignment.get("description") or "")
        records.append(
            {
                "query": str(query),
                "coverage_ids": [coverage_id] if coverage_id else [],
                "reason": (
                    "coverage: " + description
                    if description
                    else "targeted gap"
                ),
            }
        )
    return records, assignments


def _normalize_named_query_form(
    payload: Any,
    catalog: Sequence[Mapping[str, Any]],
    *,
    max_queries: int = _GENERATED_QUERY_MAX_COUNT,
) -> tuple[Any, dict[str, Any]]:
    """Collapse duplicate/overflow model fields before contract validation.

    Query count is a retrieval budget, not a reason to discard an otherwise
    useful task.  Keep the first occurrence in deterministic model/catalog
    order, record what was removed, and let the normal validator still reject
    malformed fields or fewer than two distinct queries.
    """

    if not isinstance(payload, Mapping):
        return payload, {"duplicate_count": 0, "overflow_count": 0}
    normalized: dict[str, Any] = {}
    seen_queries: set[str] = set()
    duplicate_count = 0
    overflow_count = 0
    for coverage_id, raw_queries in payload.items():
        if not isinstance(raw_queries, list):
            normalized[str(coverage_id)] = raw_queries
            continue
        kept: list[Any] = []
        for raw_query in raw_queries:
            if not isinstance(raw_query, str):
                kept.append(raw_query)
                continue
            query = _text(raw_query)
            if query in seen_queries:
                duplicate_count += 1
                continue
            if len(seen_queries) >= max_queries:
                overflow_count += 1
                continue
            seen_queries.add(query)
            kept.append(query)
        normalized[str(coverage_id)] = kept
    return normalized, {
        "duplicate_count": duplicate_count,
        "overflow_count": overflow_count,
        "max_queries": max_queries,
        "unique_query_count": len(seen_queries),
    }


def _named_form_errors(
    payload: Any,
    catalog: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Validate the named fill-form response.

    Qwen returns a tiny mapping from supplied stable coverage IDs to query
    string arrays, e.g. ``{"F1": ["query one"], "F2": ["query two"]}``.
    Unknown/missing IDs, malformed values, and zero/too-many *unique* query
    totals are compact retryable validation errors.  Duplicate query text is
    expected at an LLM boundary and is collapsed locally before counting;
    it must not make an otherwise useful supplementary task fail.
    """

    allowed_ids = {
        str(entry.get("coverage_id") or "")
        for entry in catalog
        if isinstance(entry, Mapping)
    }
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return ["named_form_must_be_object"]
    if not payload:
        return ["named_form_empty"]
    query_total = 0
    seen_queries: set[str] = set()
    for coverage_id, raw_queries in payload.items():
        coverage_id = str(coverage_id)
        if coverage_id not in allowed_ids:
            errors.append(f"unknown_coverage_field:{coverage_id}")
        if not isinstance(raw_queries, list):
            errors.append(f"coverage_field_not_list:{coverage_id}")
            continue
        for raw_query in raw_queries:
            if not isinstance(raw_query, str):
                errors.append(f"query_not_string:{coverage_id}")
                continue
            query = _text(raw_query)
            if not query or len(query) > 240:
                errors.append(f"query_invalid_length:{coverage_id}")
                continue
            if query in seen_queries:
                # Local deduplication is the first step of the supplementary
                # query contract.  Repeated model output is harmless as long
                # as enough distinct queries remain after collapsing it.
                continue
            seen_queries.add(query)
            query_total += 1
    if not (2 <= query_total <= _GENERATED_QUERY_MAX_COUNT):
        errors.append(f"query_total_out_of_range:{query_total}")
    return list(dict.fromkeys(errors))


def _parse_named_query_form(
    payload: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Flatten a valid named form into deterministic (coverage_id, query) cells."""

    catalog_order = {
        str(entry.get("coverage_id") or ""): index
        for index, entry in enumerate(catalog)
        if isinstance(entry, Mapping)
    }
    cells: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for coverage_id in sorted(
        payload,
        key=lambda key: (
            catalog_order.get(str(key), 1 << 30),
            str(key),
        ),
    ):
        for raw_query in payload[coverage_id]:
            query = _text(raw_query)
            if query and query not in seen_queries:
                seen_queries.add(query)
                cells.append((str(coverage_id), query))
    return cells


def _records_from_named_cells(
    cells: Sequence[tuple[str, str]],
    *,
    background_cue: str,
    catalog: Sequence[Mapping[str, Any]],
    semantic_engine: SupplementarySemanticEngine | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build full internal records from explicit named-field assignments.

    Routing trusts the explicit coverage_id; semantic similarity is retained
    only as audit metadata and never overrides a valid field assignment.
    """

    description_by_id = {
        str(entry.get("coverage_id") or ""): str(
            entry.get("description") or ""
        )
        for entry in catalog
        if isinstance(entry, Mapping)
    }
    records: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    vectors: dict[str, list[float]] = {}
    audit_texts: list[str] = []
    if semantic_engine is not None:
        audit_texts = [
            text
            for coverage_id, query in cells
            for text in (query, description_by_id.get(coverage_id, ""))
            if text
        ]
        try:
            vectors = semantic_engine.embed_texts(audit_texts)
        except Exception as exc:
            audit_error = f"{type(exc).__name__}:{exc}"[:160]
            vectors = {}
    else:
        audit_error = ""
    for coverage_id, query in cells:
        description = description_by_id.get(coverage_id, "")
        similarity: float | None = None
        fallback_error_code = ""
        if vectors:
            similarity = _cosine_similarity(
                vectors.get(_norm_text(query)),
                vectors.get(_norm_text(description)),
            )
        elif semantic_engine is not None:
            fallback_error_code = audit_error
        records.append(
            {
                "query": query,
                "coverage_ids": [coverage_id] if coverage_id else [],
                "reason": (
                    "coverage: " + description
                    if description
                    else "targeted gap"
                ),
            }
        )
        assignments.append(
            {
                "mode": "explicit_field",
                "coverage_id": coverage_id,
                "description": description,
                "similarity": (
                    round(float(similarity), 6)
                    if similarity is not None
                    else None
                ),
                "fallback_error_code": fallback_error_code,
            }
        )
    return records, assignments


def _validate_adjudication_decisions(
    payload: Mapping[str, Any],
    groups: Sequence[AmbiguousGroup],
) -> list[dict[str, Any]]:
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("adjudicator output must contain a decisions list")
    known_queries = {
        candidate.query_id
        for group in groups
        for candidate in group.queries
    }
    known_targets = known_queries | {
        ref.query_id for group in groups for ref in group.refs
    }
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("each adjudicator decision must be an object")
        query_id = str(decision.get("query_id") or "").strip()
        action = str(decision.get("action") or "").strip()
        reason = str(decision.get("reason") or "").strip()
        if query_id not in known_queries:
            raise ValueError(f"unknown adjudicator query_id: {query_id}")
        if query_id in seen:
            raise ValueError(f"duplicate adjudicator query_id: {query_id}")
        if action not in {"keep", "reject", "merge"}:
            raise ValueError(f"invalid adjudicator action: {action}")
        if not reason:
            raise ValueError(f"missing adjudicator reason: {query_id}")
        merged_into = str(decision.get("merged_into_query_id") or "").strip()
        if action == "merge" and (
            not merged_into or merged_into not in known_targets
        ):
            raise ValueError(
                f"merge target invalid for query_id: {query_id}"
            )
        seen.add(query_id)
        validated.append(
            {
                "query_id": query_id,
                "action": action,
                "reason": reason,
                "merged_into_query_id": merged_into,
            }
        )
    missing = sorted(known_queries - seen)
    if missing:
        raise ValueError(
            "adjudicator omitted decisions for: " + ",".join(missing)
        )
    return validated


def _bounded_generation_value(field_id: str, value: Any) -> Any:
    """Bound list cells without discarding an entire task-specific field."""

    if not isinstance(value, list):
        return value
    if field_id == "dynamic_axes":
        return [
            {
                "axis_id": str(item.get("axis_id") or item.get("id") or ""),
                "description": _text(
                    item.get("description") or item.get("title") or ""
                ),
            }
            for item in value
            if isinstance(item, dict)
        ][:12]
    if field_id in {
        "existing_paper_identities",
        "historical_queries",
        "concurrent_queries",
    }:
        return [str(item) for item in value][:20]
    return list(value)[:12]


def _build_coverage_catalog(
    resolved_context: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build the deterministic compact coverage catalog for one task.

    Each entry carries a stable ``coverage_id``, a concise ``description``,
    a ``target_type``, and a ``priority`` so Python can semantically route
    each generated query string to the best task-specific target.  The
    catalog is bounded and deterministic; Qwen reads descriptions as context
    but never emits indices.
    """

    catalog: list[dict[str, Any]] = []

    def add(
        coverage_id: str,
        description: str,
        *,
        target_type: str,
        priority: int,
    ) -> None:
        text = _text(description)
        if text and len(catalog) < 24:
            catalog.append(
                {
                    "coverage_id": _text(coverage_id),
                    "description": text,
                    "target_type": str(target_type),
                    "priority": int(priority),
                }
            )

    for index, unit in enumerate(
        (resolved_context.get("missing_fact_units") or [])[:12]
    ):
        add(
            f"F{index + 1}",
            f"missing fact unit: {unit}",
            target_type="missing_fact",
            priority=90,
        )
    for index, axis in enumerate(
        (resolved_context.get("dynamic_axes") or [])[:12]
    ):
        if isinstance(axis, dict):
            axis_id = str(axis.get("axis_id") or axis.get("id") or "")
            description = _text(
                axis.get("description") or axis.get("title") or ""
            )
            add(
                axis_id or f"A{index + 1}",
                f"dynamic axis: {description}",
                target_type="axis",
                priority=70,
            )
    section_task = resolved_context.get("section_task")
    if isinstance(section_task, dict):
        section_id = str(section_task.get("section_id") or "").strip()
        title = _text(
            section_task.get("title") or section_task.get("task") or ""
        )
        add(
            section_id or "S1",
            f"section task: {title}",
            target_type="section",
            priority=60,
        )
    argument_role = _text(resolved_context.get("argument_role"))
    if argument_role:
        add(
            "R1",
            f"argument role: {argument_role}",
            target_type="argument",
            priority=60,
        )
    target_claim = resolved_context.get("target_claim_or_sentence")
    if isinstance(target_claim, Mapping):
        claim_id = str(target_claim.get("claim_id") or "").strip()
        statement = _text(target_claim.get("statement") or "")
        add(
            claim_id or "C1",
            f"target claim: {statement}",
            target_type="claim",
            priority=80,
        )
    review_structure = resolved_context.get("current_review_structure")
    if isinstance(review_structure, Mapping):
        for index, section in enumerate(
            (review_structure.get("existing_sections") or [])[:8]
        ):
            if isinstance(section, Mapping):
                add(
                    str(section.get("section_id") or f"X{index + 1}"),
                    f"existing review section: "
                    f"{str(section.get('section_id') or index + 1)}",
                    target_type="structure",
                    priority=70,
                )
        for index, section in enumerate(
            (review_structure.get("new_sections") or [])[:4]
        ):
            if isinstance(section, Mapping):
                add(
                    str(section.get("section_id") or f"N{index + 1}"),
                    f"planned new section: "
                    f"{str(section.get('section_id') or index + 1)}",
                    target_type="structure",
                    priority=65,
                )
    excerpts = resolved_context.get("paper_introduction_conclusion_excerpts")
    if isinstance(excerpts, Mapping):
        add(
            "P1",
            "current paper introduction excerpt: "
            + _text(excerpts.get("current_paper_introduction_excerpt") or ""),
            target_type="excerpt",
            priority=60,
        )
        add(
            "P2",
            "current paper conclusion excerpt: "
            + _text(excerpts.get("current_paper_conclusion_excerpt") or ""),
            target_type="excerpt",
            priority=60,
        )
    reviewer_feedback = resolved_context.get("reviewer_feedback")
    if isinstance(reviewer_feedback, Mapping):
        for index, (key, value) in enumerate(
            list(reviewer_feedback.items())[:6]
        ):
            add(
                f"RF{index + 1}",
                f"reviewer note {key}: {_text(value)}",
                target_type="reviewer",
                priority=70,
            )
    for index, revision in enumerate(
        (resolved_context.get("author_revision_history") or [])[:6]
    ):
        if isinstance(revision, Mapping):
            add(
                f"AH{index + 1}",
                "author revision: "
                + _text(
                    revision.get("outcome") or revision.get("revision") or ""
                ),
                target_type="revision",
                priority=60,
            )
    whole_feedback = resolved_context.get("whole_review_feedback")
    if isinstance(whole_feedback, Mapping):
        add(
            "WR1",
            "whole review feedback: " + _text(whole_feedback),
            target_type="whole_review",
            priority=80,
        )
    for index, slot in enumerate(
        (resolved_context.get("visual_slots") or [])[:6]
    ):
        if isinstance(slot, Mapping):
            add(
                str(slot.get("slot_id") or f"V{index + 1}"),
                "visual slot: "
                + _text(
                    f"{slot.get('slot_id') or index + 1} "
                    f"{slot.get('role') or ''}"
                ),
                target_type="visual",
                priority=80,
            )
    for index, gap in enumerate(
        (resolved_context.get("visual_gaps") or [])[:6]
    ):
        add(
            f"VG{index + 1}",
            f"visual gap: {gap}",
            target_type="visual",
            priority=75,
        )
    return catalog


def _compact_generation_context(
    task: SupplementaryRetrievalTask,
    resolved_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded, high-information context for the generator prompt.

    Every task-specific projected cell that is present is forwarded under its
    stable field ID (lists are structurally capped, never dropped wholesale),
    plus an inspectable ``context_fields`` record and the resolved expansion
    policy.  The whole registry is never dumped.
    """

    task_metadata = resolved_context.get("task_metadata")
    task_metadata = task_metadata if isinstance(task_metadata, dict) else {}
    payload: dict[str, Any] = {
        "gap_type": task.gap_type,
        "task_id": task.task_id,
        "expansion_policy": task_metadata.get("expansion_policy") or {},
        "search_background_cue": _build_search_background_cue(
            resolved_context
        ),
        "coverage_catalog": _build_coverage_catalog(resolved_context),
    }
    included: list[str] = []
    topic_scope = resolved_context.get("topic_scope")
    topic_scope = topic_scope if isinstance(topic_scope, dict) else {}
    for field_id, value in resolved_context.items():
        if field_id == "task_metadata":
            continue
        payload[field_id] = _bounded_generation_value(field_id, value)
        included.append(field_id)
    # Convenience projection keys used by the prompt contract; they are
    # derived from projected cells, never a separate whole-registry dump.
    payload["exclusion_boundaries"] = [
        str(item)
        for item in (
            topic_scope.get("exclusion_boundaries")
            or resolved_context.get("exclusion_boundaries")
            or []
        )
    ][:12]
    payload["context_fields"] = sorted(set(included))
    return payload


def make_qwen_query_generator(
    *,
    prompt_path: str | Path | None = None,
    call_qwen: Callable[..., Any] | None = None,
    model_tier: str = "b_plus_model",
    usage_sink: Callable[[Mapping[str, Any] | None], None] | None = None,
    semantic_engine: SupplementarySemanticEngine | None = None,
) -> Callable[[SupplementaryRetrievalTask, Mapping[str, Any]], QueryGenerationResult]:
    """Factory for the bounded Qwen query generator (temperature 0, JSON, no fallback)."""

    prompt = load_prompt_text(prompt_path or DEFAULT_GENERATOR_PROMPT)
    qwen_call = call_qwen or _default_qwen_call

    def generate(
        task: SupplementaryRetrievalTask,
        resolved_context: Mapping[str, Any],
    ) -> QueryGenerationResult:
        payload = _compact_generation_context(task, resolved_context)
        catalog = list(payload.get("coverage_catalog") or [])
        background_cue = str(payload.get("search_background_cue") or "")
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]
        usage_attempts: list[dict[str, Any]] = []
        normalization_attempts: list[dict[str, Any]] = []

        def call_qwen_once(
            agent_name: str,
            call_messages: list[dict[str, str]],
            tier: str,
        ) -> dict[str, Any]:
            response = qwen_call(
                agent_name,
                call_messages,
                model_tier=tier,
                temperature=0.0,
                response_format={"type": "json_object"},
                force_mock=False,
                allow_model_fallback=False,
                enable_thinking=False,
            )
            attempt_usage = dict(response.get("_llm_usage") or {})
            usage_attempts.append(attempt_usage)
            if usage_sink is not None:
                usage_sink(attempt_usage)
            return response

        response = call_qwen_once(
            "SupplementaryQueryGenerator", messages, model_tier
        )
        parsed = _parse_json_object(
            response.get("content") or "", error_type=QueryGenerationError
        )
        parsed, normalization = _normalize_named_query_form(parsed, catalog)
        normalization_attempts.append(normalization)
        named_errors = _named_form_errors(parsed, catalog)
        cells = (
            _parse_named_query_form(parsed, catalog)
            if not named_errors
            else []
        )
        queries = [query for _coverage_id, query in cells]
        structural_errors = (
            _structural_errors_for_query_strings(queries)
            if cells
            else []
        )
        soft_warnings = (
            _soft_structure_warnings_for_query_strings(queries)
            if cells
            else []
        )
        validation_errors = named_errors + structural_errors
        if validation_errors:
            # One bounded cheap-model correction retry for named-form and
            # compact-search validation errors.  The retry sends only the
            # allowed named fields, the background cue, the validation errors,
            # and the invalid query cells -- never the full task context or
            # the prior full conversation.  Malformed JSON is never retried.
            correction_payload = {
                "instruction": "correct_named_query_form",
                "background_cue": background_cue,
                "allowed_coverage_fields": [
                    {
                        "coverage_id": str(entry.get("coverage_id") or ""),
                        "description": str(entry.get("description") or ""),
                    }
                    for entry in catalog
                ],
                "schema": {
                    str(entry.get("coverage_id") or ""): ["query one"]
                    for entry in catalog[:2]
                    if str(entry.get("coverage_id") or "")
                },
                "validation_errors": list(validation_errors),
                "invalid_queries": [
                    {
                        "coverage_id": coverage_id,
                        "query": query,
                        "structural_errors": _generated_query_compact_structure_reasons(
                            query
                        ),
                    }
                    for coverage_id, query in cells
                ],
            }
            correction_messages = [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        correction_payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ]
            response = call_qwen_once(
                "SupplementaryQueryGeneratorCorrection",
                correction_messages,
                _GENERATED_QUERY_CORRECTION_MODEL_TIER,
            )
            parsed = _parse_json_object(
                response.get("content") or "",
                error_type=QueryGenerationError,
            )
            parsed, normalization = _normalize_named_query_form(parsed, catalog)
            normalization_attempts.append(normalization)
            named_errors = _named_form_errors(parsed, catalog)
            cells = (
                _parse_named_query_form(parsed, catalog)
                if not named_errors
                else []
            )
            queries = [query for _coverage_id, query in cells]
            structural_errors = (
                _structural_errors_for_query_strings(queries)
                if cells
                else []
            )
            soft_warnings = (
                _soft_structure_warnings_for_query_strings(queries)
                if cells
                else []
            )
            still_errors = named_errors + structural_errors
            if still_errors:
                raise QueryGenerationError(
                    "generated query form invalid after correction: "
                    + "; ".join(still_errors)
                )
        records, assignments = _records_from_named_cells(
            cells,
            background_cue=background_cue,
            catalog=catalog,
            semantic_engine=semantic_engine,
        )
        final_usage = dict(usage_attempts[-1]) if usage_attempts else {}
        final_usage["attempt_count"] = len(usage_attempts)
        final_usage["attempts"] = [dict(item) for item in usage_attempts]
        final_usage["query_form_normalization"] = normalization_attempts
        final_usage["soft_structure_warnings"] = soft_warnings
        final_usage["query_soft_structure_warnings"] = {
            query: _generated_query_soft_structure_warnings(query)
            for query in queries
        }
        return QueryGenerationResult(
            queries=[record["query"] for record in records],
            records=records,
            usage=final_usage,
            assignment=assignments,
        )

    return generate


def make_qwen_adjudicator(
    *,
    prompt_path: str | Path | None = None,
    call_qwen: Callable[..., Any] | None = None,
    model_tier: str = "b_plus_model",
    usage_sink: Callable[[Mapping[str, Any] | None], None] | None = None,
) -> Callable[[Sequence[AmbiguousGroup]], dict[str, Any]]:
    """Factory for the batch Qwen dedup adjudicator (one call for all groups)."""

    prompt = load_prompt_text(prompt_path or DEFAULT_ADJUDICATOR_PROMPT)
    qwen_call = call_qwen or _default_qwen_call

    def adjudicate(groups: Sequence[AmbiguousGroup]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    [group.to_dict() for group in groups],
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]
        response = qwen_call(
            "SupplementaryQueryDeduplicator",
            messages,
            model_tier=model_tier,
            temperature=0.0,
            response_format={"type": "json_object"},
            force_mock=False,
            allow_model_fallback=False,
            enable_thinking=False,
        )
        usage = dict(response.get("_llm_usage") or {})
        if usage_sink is not None:
            usage_sink(usage)
        parsed = _parse_json_object(
            response.get("content") or "", error_type=ValueError
        )
        decisions = _validate_adjudication_decisions(parsed, groups)
        return {"decisions": decisions}

    return adjudicate


def _default_qwen_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from llm.qwen_chat_client import call_qwen_chat

    return call_qwen_chat(*args, **kwargs)


def build_supplementary_query_plan(
    task: SupplementaryRetrievalTask,
    resolved_context: Mapping[str, Any],
    query_records: Sequence[Mapping[str, Any]],
    *,
    extra_metadata: Mapping[str, Any] | None = None,
    policy: SupplementaryExpansionPolicy | None = None,
    semantic_engine: SupplementarySemanticEngine | None = None,
    semantic_threshold: float = DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD,
) -> dict[str, Any]:
    """Build a temporary query plan accepted by derive_topic_scope_contract.

    The original user question and topic scope remain the canonical identity.
    Generated supplementary queries are the discovery target list: they are
    placed into keyword decomposition (and only that field is expanded into
    discovery).  Topic lenses and scope items are retained in audit metadata,
    not expanded into independent discovery queries.  Exclusion boundaries are
    preserved untouched.
    """

    user_question = _text(
        resolved_context.get("user_question")
        or task.metadata.get("user_question")
    )
    if not user_question:
        raise ValueError("user_question context cell is required")
    topic_scope = resolved_context.get("topic_scope")
    topic_scope = topic_scope if isinstance(topic_scope, dict) else {}
    background_cue = _build_search_background_cue(resolved_context)
    repaired_records = _apply_search_background_cue(
        query_records,
        background_cue,
        semantic_engine=semantic_engine,
        semantic_threshold=semantic_threshold,
    )
    queries = _unique(
        [
            record.get("text") or record.get("query")
            for record in repaired_records
            if isinstance(record, dict)
        ]
    )
    if not queries:
        queries = _unique(task.retrieval_queries)
    if not queries:
        raise ValueError("no supplementary queries available for the query plan")

    topic_lenses = _unique(
        topic_scope.get("lenses")
        or topic_scope.get("research_lenses")
        or []
    )
    topic_inclusions = _unique(
        topic_scope.get("inclusion_boundaries")
        or topic_scope.get("inclusions")
        or []
    )
    if topic_scope.get("main_scope"):
        topic_inclusions.insert(0, _text(topic_scope["main_scope"]))
    topic_scope_items = _unique(topic_scope.get("scope_items") or [])
    topic_exclusions = _unique(
        topic_scope.get("exclusion_boundaries")
        or topic_scope.get("exclusions")
        or []
    )
    expansion_policy = policy or resolve_expansion_policy(task)
    policy_dict = expansion_policy.to_dict()
    relevance_context = _compact_generation_context(task, resolved_context)
    # The service context snapshot omits task_metadata; inject the resolved
    # policy so the marker carries exactly the compact context the generator
    # received (all cells bounded, never a full-registry dump).
    relevance_context["expansion_policy"] = policy_dict
    dynamic_axis_segments: list[str] = []
    for item in (resolved_context.get("dynamic_axes") or [])[:3]:
        if isinstance(item, dict):
            axis_text = _text(
                item.get("description") or item.get("title")
            )
            if axis_text:
                dynamic_axis_segments.append(axis_text)
    section_task = resolved_context.get("section_task")
    section_task_title = (
        _text(section_task.get("title"))
        if isinstance(section_task, dict)
        else ""
    )
    execution_meta = dict(extra_metadata or {})
    effective_result_cap = (
        int(execution_meta["result_cap"])
        if execution_meta.get("result_cap") is not None
        else int(policy_dict["result_cap"])
    )
    effective_extra_request_cap = (
        int(execution_meta["extra_request_cap"])
        if execution_meta.get("extra_request_cap") is not None
        else int(policy_dict["extra_request_cap"])
    )
    audit = {
        "schema_version": QUERY_PLAN_SCHEMA_VERSION,
        "discovery_mode": "generated_only",
        "discovery_queries": list(queries),
        "search_background_cue": background_cue,
        "relevance_context": relevance_context,
        "semantic_qualification_threshold": float(semantic_threshold),
        "semantic_usage": (
            semantic_engine.usage.to_dict()
            if semantic_engine is not None
            else None
        ),
        "search_background_context": {
            "main_scope": _text(topic_scope.get("main_scope")),
            "lenses": topic_lenses[:8],
            "dynamic_axes": dynamic_axis_segments[:3],
            "section_task_title": section_task_title,
        },
        "topic_scope": {
            "main_scope": _text(topic_scope.get("main_scope")),
            "lenses": topic_lenses,
            "inclusion_boundaries": topic_inclusions,
            "scope_items": topic_scope_items,
        },
        "task_id": task.task_id,
        "gap_type": task.gap_type,
        "priority": task.priority,
        "history_refs": sorted(set(task.history_refs)),
        "source_provenance": dict(task.source_provenance),
        "query_records": [dict(record) for record in repaired_records],
        "expansion_policy": policy_dict,
        "allow_graph_expansion": bool(policy_dict["allow_graph_expansion"]),
        "allow_role_expansion": bool(policy_dict["allow_role_expansion"]),
        "allow_exact_paper_followup": bool(
            policy_dict["allow_exact_paper_followup"]
        ),
        "allow_batch_enrichment": bool(
            policy_dict["allow_batch_enrichment"]
        ),
        "allow_oa_fulltext_fallback": bool(
            policy_dict["allow_oa_fulltext_fallback"]
        ),
        "allow_reference_expansion": bool(
            policy_dict["allow_reference_expansion"]
        ),
        "allow_citation_expansion": bool(
            policy_dict["allow_citation_expansion"]
        ),
        "allow_recommendation_expansion": bool(
            policy_dict["allow_recommendation_expansion"]
        ),
        "allow_multi_seed_graph": bool(
            policy_dict["allow_multi_seed_graph"]
        ),
        "allow_visual_processing": bool(policy_dict["allow_visual_processing"]),
        "graph_modes": list(policy_dict["graph_modes"]),
        "result_cap": effective_result_cap,
        "extra_request_cap": effective_extra_request_cap,
        "s2_snippet_results_per_query_cap": int(
            policy_dict["s2_snippet_results_per_query_cap"]
        ),
        "s2_precise_paper_cap": int(policy_dict["s2_precise_paper_cap"]),
        "batch_enrichment_paper_cap": int(
            policy_dict["batch_enrichment_paper_cap"]
        ),
        "oa_fulltext_paper_cap": int(policy_dict["oa_fulltext_paper_cap"]),
        "abstract_claim_paper_cap": int(
            policy_dict["abstract_claim_paper_cap"]
        ),
        "graph_seed_cap": int(policy_dict["graph_seed_cap"]),
    }
    if isinstance(extra_metadata, Mapping):
        audit["execution_metadata"] = dict(extra_metadata)

    return {
        "schema_version": QUERY_PLAN_SCHEMA_VERSION,
        "input": {"user_query": user_question},
        "output": {
            "canonical_question": user_question,
            "problem_understanding": user_question,
            "scope_definition": {
                "main_scope": _text(topic_scope.get("main_scope")),
                "scope_items": topic_scope_items,
                "inclusion_boundaries": topic_inclusions,
                "exclusion_boundaries": topic_exclusions,
            },
            "lenses": topic_lenses,
            "inclusion_boundaries": topic_inclusions,
            "exclusion_boundaries": topic_exclusions,
            "keyword_decomposition": {"keywords": queries},
        },
        "supplementary_retrieval": audit,
    }


def discovery_queries_from_plan(plan: Mapping[str, Any]) -> list[str]:
    """Return the bounded discovery query list carried by a supplementary plan.

    A bootstrap adapter can use this marker to select only the generated
    queries for S2 discovery instead of expanding topic lenses/scope items.
    """

    audit = plan.get("supplementary_retrieval")
    if not isinstance(audit, Mapping):
        return []
    queries = audit.get("discovery_queries")
    return [str(item) for item in queries] if isinstance(queries, list) else []


def _task_work_dir(work_root: Path, idempotency_key: str) -> Path:
    digest = hashlib.sha256(str(idempotency_key or "").encode("utf-8")).hexdigest()[:40]
    return work_root / "supplementary_tasks" / f"task_{digest}"


def _default_create_empty_kb(path: Path) -> Path:
    from optomind_research.runtime.topic_scoped_kb_stage import (
        create_empty_review_kb,
    )

    return create_empty_review_kb(path)


def _default_atomic_write_json(path: Path, payload: Any) -> None:
    from optomind_research.runtime.artifact_store import atomic_write_json

    atomic_write_json(path, payload)


def _default_prepare_s2_harness_kb(**kwargs: Any) -> dict[str, Any]:
    from optomind_research.s2_harness_bootstrap import prepare_s2_harness_kb

    return prepare_s2_harness_kb(**kwargs)


def make_literature_retrieve_callback(
    *,
    work_root: str | Path,
    policy_path: str | Path | None = None,
    results_limit: int | None = None,
    extra_request_cap: int | None = None,
    snippet_limit: int | None = None,
    semantic_engine: SupplementarySemanticEngine | None = None,
    semantic_threshold: float = DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD,
    prepare_fn: Callable[..., Any] | None = None,
    create_empty_kb_fn: Callable[..., Any] | None = None,
    atomic_write_json_fn: Callable[..., Any] | None = None,
    usage: PipelineUsage | None = None,
) -> Callable[..., RetrievalOutcome]:
    """Create the literature retrieval callback (S2/OA/abstract bootstrap).

    The supplementary query plan is written in the existing supported
    query-plan shape with generated queries as the bounded discovery keyword
    list, then passed directly to ``prepare_s2_harness_kb`` (whose public
    signature is unchanged).  The plan also carries the
    ``supplementary_retrieval.discovery_queries`` marker for adapters that
    want to select the generated list explicitly.
    """

    work_root_path = Path(work_root)
    prepare = prepare_fn or _default_prepare_s2_harness_kb
    create_empty_kb = create_empty_kb_fn or _default_create_empty_kb
    atomic_write = atomic_write_json_fn or _default_atomic_write_json

    def retrieve(
        task: SupplementaryRetrievalTask,
        query_records: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> RetrievalOutcome:
        expansion_policy = resolve_expansion_policy(task)
        effective_results_limit = (
            max(1, int(results_limit))
            if results_limit is not None
            else max(1, int(expansion_policy.result_cap))
        )
        effective_extra_request_cap = (
            max(0, int(extra_request_cap))
            if extra_request_cap is not None
            else max(0, int(expansion_policy.extra_request_cap))
        )
        effective_snippet_limit = (
            max(0, int(snippet_limit))
            if snippet_limit is not None
            else max(0, int(expansion_policy.s2_snippet_results_per_query_cap))
        )
        idempotency_key = str(execution_meta.get("idempotency_key") or "")
        work_dir = _task_work_dir(work_root_path, idempotency_key)
        work_dir.mkdir(parents=True, exist_ok=True)
        empty_seed_path = work_dir / "EMPTY_TASK_SEED.sqlite"
        query_plan_path = work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"
        report_path = work_dir / "S2_BOOTSTRAP_REPORT.json"

        report: dict[str, Any] | None = None
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                report = None
            if report is not None and str(report.get("status") or "") != "failed":
                return _retrieval_outcome(
                    task=task,
                    work_dir=work_dir,
                    report=report,
                    execution_meta=execution_meta,
                    reused=True,
                    external_call_count=0,
                    atomic_write=atomic_write,
                    usage=usage,
                )

        create_empty_kb(empty_seed_path)
        resolved_context = dict(context)
        plan = build_supplementary_query_plan(
            task,
            resolved_context,
            query_records,
            extra_metadata={
                **dict(execution_meta),
                "result_cap": effective_results_limit,
                "extra_request_cap": effective_extra_request_cap,
                "s2_snippet_results_per_query_cap": effective_snippet_limit,
            },
            policy=expansion_policy,
            semantic_engine=semantic_engine,
            semantic_threshold=semantic_threshold,
        )
        atomic_write(query_plan_path, plan)
        report = prepare(
            query_plan_path=query_plan_path,
            base_kb_sqlite=empty_seed_path,
            work_dir=work_dir,
            results_limit=effective_results_limit,
            snippet_limit=effective_snippet_limit,
            policy_path=policy_path,
            semantic_relevance=semantic_engine,
        )
        if str(report.get("status") or "") == "failed":
            raise RuntimeError(
                "S2 bootstrap failed: "
                + str(report.get("error") or report.get("error_code") or "unknown")
            )
        return _retrieval_outcome(
            task=task,
            work_dir=work_dir,
            report=report,
            execution_meta=execution_meta,
            reused=False,
            external_call_count=len(report.get("external_query_runs") or []),
            atomic_write=atomic_write,
            usage=usage,
        )

    return retrieve


def _retrieval_outcome(
    *,
    task: SupplementaryRetrievalTask,
    work_dir: Path,
    report: Mapping[str, Any],
    execution_meta: Mapping[str, Any],
    reused: bool,
    external_call_count: int,
    atomic_write: Callable[..., Any],
    usage: PipelineUsage | None,
) -> RetrievalOutcome:
    external_query_runs = list(report.get("external_query_runs") or [])
    if reused:
        external_query_runs = []
    admitted_candidates = _admitted_candidate_records(
        report.get("material_flow_ledger_path") or ""
    )
    telemetry = report.get("s2_query_telemetry") or {}
    if usage is not None:
        usage.record_s2_telemetry(telemetry if isinstance(telemetry, dict) else {})
    metadata = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "execution_meta": dict(execution_meta),
        "work_dir": str(work_dir),
        "empty_seed_path": str(work_dir / "EMPTY_TASK_SEED.sqlite"),
        "query_plan_path": str(work_dir / "SUPPLEMENTARY_QUERY_PLAN.json"),
        "runtime_kb_sqlite": str(report.get("runtime_kb_sqlite") or ""),
        "graph_path": str(report.get("graph_path") or ""),
        "material_flow_ledger_path": str(
            report.get("material_flow_ledger_path") or ""
        ),
        "telemetry_path": str(report.get("telemetry_path") or ""),
        "status": str(report.get("status") or ""),
        "reused": reused,
        "external_call_count": external_call_count,
        "material_flow_summary": dict(report.get("material_flow_summary") or {}),
        "search_queries": list(report.get("search_queries") or []),
        "report_sha256": str(report.get("report_sha256") or ""),
        "task_id": task.task_id,
        "gap_type": task.gap_type,
    }
    atomic_write(
        work_dir / "SUPPLEMENTARY_RETRIEVAL_REPORT.json",
        {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "execution_meta": dict(execution_meta),
            "reused": reused,
            "external_call_count": external_call_count,
            "status": metadata["status"],
            "paths": {
                "work_dir": metadata["work_dir"],
                "empty_seed_path": metadata["empty_seed_path"],
                "query_plan_path": metadata["query_plan_path"],
                "runtime_kb_sqlite": metadata["runtime_kb_sqlite"],
                "material_flow_ledger_path": metadata["material_flow_ledger_path"],
            },
        },
    )
    return RetrievalOutcome(
        candidates=admitted_candidates,
        adequate=True,
        query_runs=external_query_runs,
        metadata=metadata,
        route=str(execution_meta.get("route") or "literature"),
    )


_ADMITTED_CANDIDATE_FIELDS = (
    "paper_id",
    "doi",
    "title",
    "year",
    "venue",
    "material_status",
    "admitted_to_downstream",
)


def _admitted_candidate_records(ledger_path: Any) -> list[dict[str, Any]]:
    """Return lightweight identity/status records for admitted ledger papers.

    The material flow ledger is authoritative for the admitted paper count.
    Only fields already present in the ledger row are copied -- never full
    text.  A missing or malformed ledger fails safely to an empty list.
    """

    if not ledger_path:
        return []
    path = Path(str(ledger_path))
    if not path.is_file():
        return []
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(ledger, dict):
        return []
    records: list[dict[str, Any]] = []
    for row in ledger.get("papers") or []:
        if not isinstance(row, dict) or not bool(row.get("admitted_to_downstream")):
            continue
        records.append(
            {
                field: row.get(field)
                for field in _ADMITTED_CANDIDATE_FIELDS
                if field in row
            }
        )
    return records


def _default_build_packets(**kwargs: Any) -> dict[str, Any]:
    from optomind_research.runtime.claim_centered_material_cards import (
        build_claim_centered_material_packets,
    )

    return build_claim_centered_material_packets(**kwargs)


def _default_extract_cards(**kwargs: Any) -> dict[str, Any]:
    from optomind_research.runtime.material_proposition_extractor import (
        run_material_proposition_extraction,
    )

    return run_material_proposition_extraction(**kwargs)


def _default_build_units(**kwargs: Any) -> dict[str, Any]:
    from optomind_research.runtime.material_unit_store import (
        build_material_unit_store,
    )

    return build_material_unit_store(**kwargs)


def _default_embedder(
    texts: Sequence[str], *, usage_accumulator: dict[str, int] | None = None, **_: Any
) -> list[list[float]]:
    from optomind_research.runtime.material_semantic_cache import (
        dashscope_embedder,
    )

    return dashscope_embedder(texts, usage_accumulator=usage_accumulator)


def finalize_task_material_cache(
    *,
    units: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
    question: str,
    output_dir: str | Path,
    embedder: Callable[..., Any],
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    batch_size: int = 10,
    workers: int = 4,
    atomic_write: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Finalize task-local vectors and query annotations with injected embedder."""

    from optomind_research.runtime.material_semantic_cache import (
        MaterialSemanticCache,
    )
    from optomind_research.runtime.material_unit_store import (
        attach_query_annotations,
        question_identity,
    )

    atomic_write_fn = atomic_write or _default_atomic_write_json
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    vector_path = output_path / "material_vectors.sqlite"
    ledger_path = output_path / "EMBEDDING_USAGE_LEDGER.json"
    usage: dict[str, int] = {"input_tokens": 0, "request_count": 0}
    with MaterialSemanticCache(vector_path) as cache:
        def embed(texts: Sequence[str]) -> list[list[float]]:
            local_usage: dict[str, int] = {"input_tokens": 0, "request_count": 0}
            vectors = embedder(texts, usage_accumulator=local_usage)
            usage["input_tokens"] += max(0, int(local_usage.get("input_tokens") or 0))
            usage["request_count"] += max(
                0, int(local_usage.get("request_count") or 0)
            )
            return vectors

        vector_result = cache.ensure_units_parallel(
            units,
            embed,
            embedding_model=embedding_model,
            batch_size=batch_size,
            workers=workers,
        )
    identity = question_identity(None, question)
    annotated = attach_query_annotations(
        {"units": list(units)},
        cards,
        query_id=identity["query_id"],
        question=question,
    )
    final_units_path = output_path / "MATERIAL_UNITS_FINAL.json"
    atomic_write_fn(final_units_path, annotated)
    atomic_write_fn(
        ledger_path,
        {
            "schema_version": "optomind.embedding_usage_ledger.v1",
            "cumulative": dict(usage),
            "runs": [{"vector_result": vector_result, "delta": dict(usage)}],
        },
    )
    report = {
        "schema_version": "optomind.material_card_cache_run.v1",
        "final_units_path": str(final_units_path),
        "vector_cache_path": str(vector_path),
        "embedding_model": embedding_model,
        "unit_count": len(units),
        "card_count": len(cards),
        "vector_result": vector_result,
        "embedding_usage": dict(usage),
        "query_identity": identity,
    }
    atomic_write_fn(output_path / "FINALIZATION_REPORT.json", report)
    return report


def _filter_supplementary_out_of_scope(
    units: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Filter exactly-out-of-scope works before embedding.

    Only sanitized cards classified exactly ``out_of_scope`` are excluded.
    Central, substantial, contextual, and missing-card works are retained
    fail-safe.  Raw cards and raw units remain on disk for audit; only the
    retained sets reach vectorization/finalization.
    """

    card_relevance: dict[str, str] = {}
    for card in cards:
        work_id = str(card.get("canonical_work_id") or "").strip()
        relevance = str(card.get("question_relevance") or "").strip()
        if work_id:
            card_relevance[work_id] = relevance
    excluded_work_ids = sorted(
        work_id
        for work_id, relevance in card_relevance.items()
        if relevance == "out_of_scope"
    )
    excluded = set(excluded_work_ids)
    retained_units = [
        dict(unit)
        for unit in units
        if str(unit.get("work_id") or "").strip() not in excluded
    ]
    retained_cards = [
        dict(card)
        for card in cards
        if str(card.get("canonical_work_id") or "").strip() not in excluded
    ]
    unit_work_ids = {
        str(unit.get("work_id") or "").strip()
        for unit in units
        if str(unit.get("work_id") or "").strip()
    }
    retained_work_ids = {
        str(unit.get("work_id") or "").strip()
        for unit in retained_units
        if str(unit.get("work_id") or "").strip()
    }
    unclassified_work_ids = sorted(unit_work_ids - set(card_relevance))
    audit = {
        "schema_version": RELEVANCE_FILTER_SCHEMA_VERSION,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "pre_filter_card_work_count": len(card_relevance),
            "post_filter_card_work_count": len(
                {
                    str(card.get("canonical_work_id") or "").strip()
                    for card in retained_cards
                    if str(card.get("canonical_work_id") or "").strip()
                }
            ),
            "pre_filter_unit_work_count": len(unit_work_ids),
            "post_filter_unit_work_count": len(retained_work_ids),
            "pre_filter_unit_count": len(units),
            "post_filter_unit_count": len(retained_units),
        },
        "excluded_work_ids": excluded_work_ids,
        "excluded_works": [
            {
                "work_id": work_id,
                "question_relevance": "out_of_scope",
                "reason": "question_relevance_out_of_scope",
            }
            for work_id in excluded_work_ids
        ],
        "unclassified_work_ids": unclassified_work_ids,
        "all_out_of_scope": bool(units and not retained_units),
    }
    return retained_units, retained_cards, audit


def make_literature_materialize_callback(
    *,
    embedder: Callable[..., Any] | None = None,
    build_packets_fn: Callable[..., Any] | None = None,
    extract_cards_fn: Callable[..., Any] | None = None,
    build_units_fn: Callable[..., Any] | None = None,
    atomic_write_json_fn: Callable[..., Any] | None = None,
    cards_model_tier: str = "b_plus_model",
    cards_workers: int = 1,
    embedding_batch_size: int = 10,
    embedding_workers: int = 4,
    usage: PipelineUsage | None = None,
) -> Callable[..., MaterializationOutcome]:
    """Create the literature materialization callback (task-local increment)."""

    build_packets = build_packets_fn or _default_build_packets
    extract_cards = extract_cards_fn or _default_extract_cards
    build_units = build_units_fn or _default_build_units
    embed = embedder or _default_embedder
    atomic_write = atomic_write_json_fn or _default_atomic_write_json

    def materialize(
        task: SupplementaryRetrievalTask,
        retrieval: RetrievalOutcome,
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> MaterializationOutcome:
        meta = retrieval.metadata
        work_dir = Path(str(meta.get("work_dir") or ""))
        ledger_path = Path(str(meta.get("material_flow_ledger_path") or ""))
        runtime_kb = Path(str(meta.get("runtime_kb_sqlite") or ""))
        query_plan_path = Path(str(meta.get("query_plan_path") or ""))
        materialization_report_path = (
            work_dir / "SUPPLEMENTARY_MATERIALIZATION_REPORT.json"
        )
        packets_dir = work_dir / "claim_centered_classification"
        packets_path = packets_dir / "MATERIAL_CARD_PACKETS.json"
        cards_dir = work_dir / "material_cards"
        units_dir = work_dir / "material_units"
        units_path = units_dir / "MATERIAL_UNITS.json"
        vector_dir = work_dir / "material_vectors"
        final_units_path = vector_dir / "MATERIAL_UNITS_FINAL.json"

        def outcome_and_report(
            *,
            adequate: bool,
            reason: str,
            sources: list[dict[str, Any]] | None = None,
            total_references: int = 0,
            background_only_references: int = 0,
            reused: bool = False,
            qwen_usage: list[dict[str, Any]] | None = None,
            embedding_usage: dict[str, int] | None = None,
            extra: Mapping[str, Any] | None = None,
        ) -> MaterializationOutcome:
            payload: dict[str, Any] = {
                "schema_version": PIPELINE_SCHEMA_VERSION,
                "execution_meta": dict(execution_meta),
                "reason": reason,
                "reused": reused,
                "work_dir": str(work_dir),
                "packets_path": str(packets_path),
                "cards_dir": str(cards_dir),
                "units_path": str(units_path),
                "vector_dir": str(vector_dir),
                "final_units_path": str(final_units_path),
                "qwen_usage": list(qwen_usage or []),
                "embedding_usage": dict(embedding_usage or {}),
                "total_references": int(total_references),
                "background_only_references": int(
                    background_only_references
                ),
                "material_classes": [],
                "identities": [],
            }
            if isinstance(extra, Mapping):
                payload.update({k: v for k, v in extra.items() if v is not None})
            atomic_write(
                materialization_report_path,
                {
                    "schema_version": PIPELINE_SCHEMA_VERSION,
                    "execution_meta": dict(execution_meta),
                    "adequate": adequate,
                    "reason": reason,
                    "reused": reused,
                    "payload": payload,
                },
            )
            return MaterializationOutcome(
                sources=list(sources or []),
                adequate=adequate,
                total_references=int(total_references),
                background_only_references=int(background_only_references),
                materialized_route="task_local_increment",
                metadata=payload,
            )

        if materialization_report_path.is_file() and final_units_path.is_file():
            try:
                stored = json.loads(
                    materialization_report_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                stored = {}
            stored_meta = stored.get("execution_meta") or {}
            if (
                str(stored_meta.get("idempotency_key") or "")
                == str(execution_meta.get("idempotency_key") or "")
                and str(stored_meta.get("task_fingerprint") or "")
                == str(execution_meta.get("task_fingerprint") or "")
            ):
                stored_payload = stored.get("payload") or {}
                return outcome_and_report(
                    adequate=bool(stored.get("adequate", True)),
                    reason=str(stored.get("reason") or "reused"),
                    sources=[],
                    total_references=int(stored_payload.get("total_references") or 0),
                    background_only_references=int(
                        stored_payload.get("background_only_references") or 0
                    ),
                    reused=True,
                    qwen_usage=[],
                    embedding_usage={"input_tokens": 0, "request_count": 0},
                    extra={
                        "material_classes": stored_payload.get("material_classes") or [],
                        "identities": stored_payload.get("identities") or [],
                        "admitted_paper_count": stored_payload.get(
                            "admitted_paper_count"
                        ),
                        "packet_count": stored_payload.get("packet_count"),
                        "card_summary": stored_payload.get("card_summary") or {},
                        "relevance_filter_audit": stored_payload.get(
                            "relevance_filter_audit"
                        ) or {},
                        "retained_work_count": stored_payload.get(
                            "retained_work_count"
                        ),
                        "retained_unit_count": stored_payload.get(
                            "retained_unit_count"
                        ),
                        "stored_qwen_usage": stored_payload.get("qwen_usage") or [],
                        "stored_embedding_usage": (
                            stored_payload.get("embedding_usage") or {}
                        ),
                        "vector_result": {"requested": 0, "reused": 1, "embedded": 0},
                    },
                )

        if not ledger_path.is_file():
            return outcome_and_report(
                adequate=False,
                reason="material_flow_ledger_missing",
            )
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return outcome_and_report(
                adequate=False,
                reason="material_flow_ledger_invalid",
            )
        admitted = [
            row
            for row in (ledger.get("papers") or [])
            if isinstance(row, dict) and row.get("admitted_to_downstream")
        ]
        if not admitted:
            return outcome_and_report(
                adequate=False,
                reason="no_admitted_material",
                extra={"admitted_paper_count": 0},
            )

        packets = build_packets(
            kb_sqlite=runtime_kb,
            material_flow_ledger_path=ledger_path,
            query_plan_path=query_plan_path,
            output_path=packets_path,
        )
        cards_summary = extract_cards(
            packet_path=packets_path,
            output_dir=cards_dir,
            model_tier=cards_model_tier,
            workers=cards_workers,
            skip_existing=True,
        )
        units_result = build_units(
            kb_sqlite=runtime_kb,
            material_flow_ledger_path=ledger_path,
            output_path=units_path,
        )
        units = list(units_result.get("units") or [])
        cards: list[dict[str, Any]] = []
        cards_root = cards_dir / "cards"
        if cards_root.is_dir():
            for path in sorted(cards_root.glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                card = value.get("card") if isinstance(value, dict) else None
                if isinstance(card, dict):
                    cards.append(card)
        units, cards, relevance_filter_audit = (
            _filter_supplementary_out_of_scope(units, cards)
        )
        atomic_write(
            work_dir / "SUPPLEMENTARY_RELEVANCE_FILTER_AUDIT.json",
            relevance_filter_audit,
        )
        excluded_work_set = set(
            relevance_filter_audit.get("excluded_work_ids") or []
        )
        retained_packets = [
            dict(packet)
            for packet in (packets.get("packets") or [])
            if isinstance(packet, dict)
            and str(packet.get("canonical_work_id") or "").strip()
            not in excluded_work_set
        ]
        qwen_usage: list[dict[str, Any]] = []
        for row in cards_summary.get("rows") or []:
            llm_usage = row.get("llm_usage") if isinstance(row, dict) else None
            if isinstance(llm_usage, dict) and llm_usage:
                qwen_usage.append(dict(llm_usage))
                if usage is not None:
                    usage.record_material_cards(llm_usage)
        final_report = finalize_task_material_cache(
            units=units,
            cards=cards,
            question=str(packets.get("question") or ""),
            output_dir=vector_dir,
            embedder=embed,
            batch_size=embedding_batch_size,
            workers=embedding_workers,
            atomic_write=atomic_write,
        )
        embedding_usage = dict(final_report["embedding_usage"])
        if usage is not None:
            usage.record_embedding(embedding_usage)
        material_classes = sorted(
            {
                str(item)
                for packet in retained_packets
                if isinstance(packet, dict)
                for item in (packet.get("material_classes") or [])
            }
        )
        identities = sorted(
            {
                str(packet.get("canonical_work_id") or "")
                for packet in retained_packets
                if isinstance(packet, dict)
            }
        )
        background_only = sum(
            1
            for unit in units
            if str((unit.get("durable_content") or {}).get("content_depth"))
            == "abstract_claim"
        )
        return outcome_and_report(
            adequate=True,
            reason="committed",
            sources=list(retained_packets),
            total_references=len(units),
            background_only_references=background_only,
            reused=False,
            qwen_usage=qwen_usage,
            embedding_usage=embedding_usage,
            extra={
                "material_classes": material_classes,
                "identities": identities,
                "admitted_paper_count": len(admitted),
                "packet_count": len(retained_packets),
                "relevance_filter_audit": relevance_filter_audit,
                "retained_work_count": relevance_filter_audit.get(
                    "counts", {}
                ).get("post_filter_unit_work_count"),
                "retained_unit_count": relevance_filter_audit.get(
                    "counts", {}
                ).get("post_filter_unit_count"),
                "card_summary": {
                    "selected_work_count": cards_summary.get("selected_work_count"),
                    "new_attempt_count": cards_summary.get("new_attempt_count"),
                    "reused_count": cards_summary.get("reused_count"),
                    "successful_card_count": cards_summary.get("successful_card_count"),
                    "failed_count": cards_summary.get("failed_count"),
                },
                "vector_result": final_report.get("vector_result"),
            },
        )

    return materialize


def make_unsupported_visual_callbacks() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Return callbacks that fail closed for visual tasks."""

    def unsupported_retrieve(
        task: SupplementaryRetrievalTask,
        queries: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> RetrievalOutcome:
        raise VisualPipelineUnsupportedError(
            "visual supplementary retrieval is not wired in the text pipeline; "
            "inject visual callbacks before submitting visual tasks"
        )

    def unsupported_materialize(
        task: SupplementaryRetrievalTask,
        retrieval: RetrievalOutcome,
        context: Mapping[str, Any],
        execution_meta: Mapping[str, Any],
    ) -> MaterializationOutcome:
        raise VisualPipelineUnsupportedError(
            "visual supplementary materialization is not wired in the text "
            "pipeline; inject visual callbacks before submitting visual tasks"
        )

    return unsupported_retrieve, unsupported_materialize


class SupplementaryRetrievalPipeline:
    """High-level coordinator wiring generator, adjudicator, and callbacks."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        work_root: str | Path,
        policy_path: str | Path | None = None,
        results_limit: int | None = None,
        extra_request_cap: int | None = None,
        snippet_limit: int | None = None,
        generator: Callable[..., Any] | None = None,
        adjudicator: Callable[..., Any] | None = None,
        qwen_call: Callable[..., Any] | None = None,
        generator_prompt_path: str | Path | None = None,
        adjudicator_prompt_path: str | Path | None = None,
        retrieve_callback: Callable[..., Any] | None = None,
        materialize_callback: Callable[..., Any] | None = None,
        visual_retrieve: Callable[..., Any] | None = None,
        visual_materialize: Callable[..., Any] | None = None,
        visual_cache_root: str | Path | None = None,
        enable_visual_procurement: bool = False,
        visual_procurement_config: Any | None = None,
        visual_reviewer: Callable[..., Any] | None = None,
        enable_visual_review: bool = True,
        embedder: Callable[..., Any] | None = None,
        semantic_embedder: Callable[..., Any] | None = None,
        semantic_engine: SupplementarySemanticEngine | None = None,
        semantic_threshold: float = DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD,
        build_packets_fn: Callable[..., Any] | None = None,
        extract_cards_fn: Callable[..., Any] | None = None,
        build_units_fn: Callable[..., Any] | None = None,
        prepare_fn: Callable[..., Any] | None = None,
        create_empty_kb_fn: Callable[..., Any] | None = None,
        atomic_write_json_fn: Callable[..., Any] | None = None,
        cards_model_tier: str = "b_plus_model",
        cards_workers: int = 1,
        embedding_batch_size: int = 10,
        embedding_workers: int = 4,
        usage: PipelineUsage | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.work_root = Path(work_root)
        self.usage = usage or PipelineUsage()
        self._semantic_engine = (
            semantic_engine
            or SupplementarySemanticEngine(
                embedder=semantic_embedder or embedder
            )
        )
        self._semantic_threshold = float(semantic_threshold)
        self.generator = generator or make_qwen_query_generator(
            prompt_path=generator_prompt_path,
            call_qwen=qwen_call,
            usage_sink=self.usage.record_query_generation,
            semantic_engine=self._semantic_engine,
        )
        self.adjudicator = adjudicator or make_qwen_adjudicator(
            prompt_path=adjudicator_prompt_path,
            call_qwen=qwen_call,
            usage_sink=self.usage.record_adjudication,
        )
        unsupported_visual_retrieve, unsupported_visual_materialize = (
            make_unsupported_visual_callbacks()
        )
        self.retrieve_callback = retrieve_callback or make_literature_retrieve_callback(
            work_root=self.work_root,
            policy_path=policy_path,
            results_limit=results_limit,
            extra_request_cap=extra_request_cap,
            snippet_limit=snippet_limit,
            semantic_engine=self._semantic_engine,
            semantic_threshold=self._semantic_threshold,
            prepare_fn=prepare_fn,
            create_empty_kb_fn=create_empty_kb_fn,
            atomic_write_json_fn=atomic_write_json_fn,
            usage=self.usage,
        )
        self.materialize_callback = (
            materialize_callback
            or make_literature_materialize_callback(
                embedder=embedder,
                build_packets_fn=build_packets_fn,
                extract_cards_fn=extract_cards_fn,
                build_units_fn=build_units_fn,
                atomic_write_json_fn=atomic_write_json_fn,
                cards_model_tier=cards_model_tier,
                cards_workers=cards_workers,
                embedding_batch_size=embedding_batch_size,
                embedding_workers=embedding_workers,
                usage=self.usage,
            )
        )
        if visual_retrieve is not None or visual_materialize is not None:
            self.visual_retrieve = (
                visual_retrieve or unsupported_visual_retrieve
            )
            self.visual_materialize = (
                visual_materialize or unsupported_visual_materialize
            )
            self.visual_procurement_config = None
            self.visual_reviewer = None
        elif visual_cache_root is not None or enable_visual_procurement:
            from functools import partial

            from .visual_procurement_pipeline import (
                VisualProcurementConfig,
                make_visual_materialize_callback,
                make_visual_retrieve_callback,
                review_with_visual_argument_classifier,
            )

            cache_root = (
                Path(visual_cache_root)
                if visual_cache_root is not None
                else Path(work_root) / "long_term_visual_cache"
            )
            cfg = visual_procurement_config or VisualProcurementConfig()
            if visual_reviewer is not None:
                reviewer = visual_reviewer
            elif enable_visual_review:
                reviewer = partial(
                    review_with_visual_argument_classifier,
                    review_cap=cfg.review_cap,
                )
            else:
                reviewer = None
            self.visual_procurement_config = cfg
            self.visual_reviewer = reviewer
            self.visual_retrieve = make_visual_retrieve_callback(
                self.retrieve_callback,
            )
            self.visual_materialize = make_visual_materialize_callback(
                cache_root=cache_root,
                reviewer=reviewer,
                literature_materialize=self.materialize_callback,
                config=cfg,
            )
        else:
            self.visual_retrieve = unsupported_visual_retrieve
            self.visual_materialize = unsupported_visual_materialize
            self.visual_procurement_config = None
            self.visual_reviewer = None

    @property
    def semantic_relevance_usage(self) -> SemanticRelevanceUsage:
        return self._semantic_engine.usage

    @property
    def semantic_threshold(self) -> float:
        return self._semantic_threshold

    def make_service_callbacks(self) -> ServiceCallbacks:
        """Return callbacks wired to adjudication, retrieval, materialization."""

        return ServiceCallbacks(
            retrieve=self.retrieve_callback,
            materialize=self.materialize_callback,
            visual_retrieve=self.visual_retrieve,
            visual_materialize=self.visual_materialize,
            adjudicator=self.adjudicator,
        )

    def generate_and_submit(
        self,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        *,
        allow_retry: bool = False,
    ):
        """Generate queries and submit, after a durable replay preflight.

        The SQLite service is the source of truth for task idempotency.  When
        the task has explicit retrieval queries or a recoverable prior
        submission, the durable identity is checked before the Qwen generator
        is invoked so committed/no_progress replay is zero-Qwen and
        zero-embedding.  Failed tasks never spend Qwen without an explicit
        retry; an explicit retry generates fresh queries.  Background
        qualification (which may spend the single batched semantic embedding
        call) happens only after the zero-network replay preflight.
        """

        task_errors = list(task.validate())
        if task_errors:
            raise ValueError(
                "cannot preflight supplementary retrieval identity: "
                + "; ".join(task_errors)
            )
        projected_context = project_context_for_task(task, registry)
        background_cue = _build_search_background_cue(projected_context)
        raw_explicit_queries = [
            str(query).strip()
            for query in task.retrieval_queries
            if str(query).strip()
        ]
        preflight = self._preflight_replay(
            task,
            registry,
            raw_explicit_queries,
            allow_retry=allow_retry,
        )
        if preflight is not None:
            return preflight
        generated = self.generator(task, projected_context)
        # Qualify before SupplementaryRetrievalService.submit so durable dedup
        # sees the final background-qualified string, not the naked gap.
        repaired_records = _apply_search_background_cue(
            generated.records,
            background_cue,
            semantic_engine=self._semantic_engine,
            semantic_threshold=self._semantic_threshold,
        )
        submit_queries = [
            record["query"] for record in repaired_records if record["query"]
        ]
        submit_records = [
            record for record in repaired_records if record["query"]
        ]
        if not submit_queries:
            explicit_records = _apply_search_background_cue(
                [{"query": query} for query in raw_explicit_queries],
                background_cue,
                semantic_engine=self._semantic_engine,
                semantic_threshold=self._semantic_threshold,
            )
            submit_records = [
                record for record in explicit_records if record.get("query")
            ]
            submit_queries = [
                record["query"] for record in submit_records
            ]
        service = SupplementaryRetrievalService(
            self.db_path,
            callbacks=self.make_service_callbacks(),
        )
        try:
            return service.submit(
                task,
                registry,
                query_records=submit_records,
                allow_retry=allow_retry,
            )
        finally:
            service.close()

    def _preflight_replay(
        self,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        explicit_queries: Sequence[str],
        *,
        allow_retry: bool,
    ) -> SubmissionResult | None:
        """Return durable reuse/block evidence without calling the generator.

        Returns ``None`` when no prior submission is recoverable, meaning the
        caller may generate fresh queries.  A recoverable prior submission is
        found either by the exact idempotency key (when explicit queries are
        available) or by a task_id + task-fingerprint match.
        """

        service = SupplementaryRetrievalService(
            self.db_path,
            callbacks=ServiceCallbacks(),
        )
        try:
            row = None
            key = ""
            if explicit_queries:
                key = service._idempotency_key(task, registry, explicit_queries)
                row = service.get_task_by_idempotency(key)
            if row is None:
                latest = service.get_task(task.task_id)
                if latest is not None and latest["fingerprint"] == task_fingerprint(
                    task, registry
                ):
                    row = latest
                    key = str(latest["idempotency_key"])
            if row is None:
                return None
            return self._submission_from_row(
                row,
                idempotency_key=key,
                allow_retry=allow_retry,
            )
        finally:
            service.close()

    @staticmethod
    def _submission_from_row(
        row: Mapping[str, Any],
        *,
        idempotency_key: str,
        allow_retry: bool,
    ) -> SubmissionResult | None:
        """Convert a durable row into reuse/block evidence for preflight."""

        status = str(row.get("status") or "")
        attempts = tuple(row.get("attempts") or [])
        result = row.get("result")
        if status in {"committed", "no_progress"}:
            return SubmissionResult(
                reused=True,
                idempotency_key=idempotency_key,
                task_id=str(row.get("task_id") or ""),
                status=status,
                reuse_reason=f"{status}_replay",
                attempt_history=attempts,
                result=result,
            )
        if status in {"queued", "running", "materializing"}:
            return SubmissionResult(
                reused=True,
                idempotency_key=idempotency_key,
                task_id=str(row.get("task_id") or ""),
                status=status,
                reuse_reason="already_active",
                attempt_history=attempts,
            )
        if status == "failed" and not allow_retry:
            return SubmissionResult(
                reused=False,
                idempotency_key=idempotency_key,
                task_id=str(row.get("task_id") or ""),
                status="failed",
                reuse_reason="failed_requires_explicit_retry",
                errors=("explicit_retry_required",),
                attempt_history=attempts,
                result=result,
            )
        return None

    def run_pending(self, *, max_tasks: int = 100) -> list[Any]:
        """Run queued tasks serially through the same wired callbacks."""

        service = SupplementaryRetrievalService(
            self.db_path,
            callbacks=self.make_service_callbacks(),
        )
        try:
            return service.process_pending(max_tasks=max_tasks)
        finally:
            service.close()


__all__ = [
    "DEFAULT_ADJUDICATOR_PROMPT",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_GENERATOR_PROMPT",
    "DEFAULT_REPRESENTATION_VERSION",
    "DEFAULT_SEMANTIC_QUALIFICATION_THRESHOLD",
    "PIPELINE_SCHEMA_VERSION",
    "PipelineUsage",
    "QueryGenerationError",
    "QueryGenerationResult",
    "SemanticRelevanceError",
    "SemanticRelevanceUsage",
    "SupplementarySemanticEngine",
    "SupplementaryRetrievalPipeline",
    "VisualPipelineUnsupportedError",
    "build_supplementary_query_plan",
    "finalize_task_material_cache",
    "load_prompt_text",
    "make_literature_materialize_callback",
    "make_literature_retrieve_callback",
    "make_qwen_adjudicator",
    "make_qwen_query_generator",
    "make_unsupported_visual_callbacks",
]
