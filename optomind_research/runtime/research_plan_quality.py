"""Deterministic quality guards for R5 research-plan prose."""

from __future__ import annotations

import re
from typing import Any, Mapping

_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9-]{1,9}\b")
_PAREN_ACRONYM = re.compile(r"\(\s*(?P<acronym>[A-Z][A-Z0-9-]{1,9})\s*\)")
_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
_SOURCE_MARKER = re.compile(
    r"(?:\[REF:[^\]]+\]|\b(?:doi|source|cited|reported|measured|literature|"
    r"paper|published|according\s+to|from\s+the\s+study|validated\s+against)\b)",
    re.I,
)
_PROPOSED_MARKER = re.compile(
    r"\b(?:proposed|proposal|program\s+(?:scope|parameter|target)|"
    r"calibration\s+(?:target|parameter|distribution)|design\s+target|"
    r"to\s+be\s+calibrated|verification_deferred|planned|will\s+test|"
    r"aim(?:s)?|posits?|hypothesis|falsif(?:y|ication))\b",
    re.I,
)
_NORMALIZED_NUMERIC_MARKER = re.compile(
    r"\b(?:verification_deferred|proposed\s+(?:program|calibration)|"
    r"design\s+target|calibration\s+target)\b",
    re.I,
)
_PROGRAM_NUMBER = re.compile(
    r"(?:"
    r"(?<!\w)[<>~]?\s*\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?)?\s*"
    r"(?:%|ppm|ppb|nm|um|μm|mm|cm|mrad|rad(?:ians?)?|deg(?:rees?)?|"
    r"hz|khz|mhz|ghz|thz|db|w|mw|kw|gpu[- ]?hours?|hours?|days?|"
    r"weeks?|months?|years?|samples?|realizations?|runs?|replicates?)"
    r"(?!\w)"
    r"|(?<!\w)[$€£]\s*\d[\d,]*(?:\.\d+)?"
    r"|\b(?:sigma|σ)\s*=\s*\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?)?\s*%?"
    r")",
    re.I,
)
_SKIP_NUMERIC_KEYS = {
    "source_context", "normalization_audit", "readiness_summary",
    "reference_paper_ids", "supporting_paper_ids", "supporting_chunk_ids",
    "main_hypothesis_ids", "future_hypothesis_ids", "opportunity_ids",
    "hypothesis_ids", "metric_ids", "baseline_ids", "dependencies", "year",
}
_COMMON_ACRONYMS = {"API", "CJK", "DOI", "JSON", "LLM", "OA", "PDF", "RAG", "URL"}
_CONNECTORS = {
    "a", "an", "and", "as", "for", "from", "in", "of", "on", "or", "the", "to", "with"
}
_CANONICAL_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9-]{1,19}(?![A-Za-z0-9-])"
)
_METRIC_BEARING_KEYS = {
    "comparison_protocol",
    "evaluation_metrics",
    "expected_outputs",
    "expected_results",
    "experiments",
    "falsification_conditions",
    "methods",
    "methods_summary",
    "narrative_markdown",
    "objectives",
    "paper_abstract",
    "problem_statement",
    "proposed_tests",
    "rationale",
    "risks",
    "statement",
    "stop_or_pivot_criteria",
    "strategy",
    "technical_details",
    "verification_deferred",
}
_STRUCTURAL_ID_KEYS = {
    "baseline_id",
    "baseline_ids",
    "hypothesis_id",
    "hypothesis_ids",
    "metric_id",
    "metric_ids",
    "opportunity_id",
    "opportunity_ids",
    "platform_id",
    "problem_id",
    "program_focus_gate_id",
    "work_package_id",
}
_SIMULATED_FABRICATED_METRIC = re.compile(
    r"\bsimulated\s*-\s*vs\.?\s*-\s*fabricated\b|"
    r"\bsimulated\s+vs\.?\s+fabricated\b",
    re.I,
)
_FABRICATION_EXCLUSION = re.compile(
    r"\b(?:no|without|not|exclude(?:s|d)?|simulation[- ]only|all\s+simulation[- ]based)\b"
    r"[^.!?\n]{0,100}\b(?:actual\s+)?(?:experiment\w*|fabricat\w*|"
    r"empirical\s+measurement\w*|physical\s+validation)\b",
    re.I,
)
_PUBLISHED_REFERENCE_FABRICATION_DATA = re.compile(
    r"\b(?:published|reference|literature|reported|archived|prior)\b"
    r"[^.!?\n]{0,100}\b(?:fabricat\w*|coating\w*|device\w*|sample\w*)\b"
    r"[^.!?\n]{0,70}\b(?:data|measurement\w*|spectr\w*|performance|result\w*)\b|"
    r"\b(?:fabricat\w*|coating\w*|device\w*|sample\w*)\b"
    r"[^.!?\n]{0,70}\b(?:data|measurement\w*|spectr\w*|performance|result\w*)\b"
    r"[^.!?\n]{0,100}\b(?:published|reference|literature|reported|archived|prior)\b",
    re.I,
)


def _flatten_source(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_flatten_source(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_source(item) for item in value)
    return str(value or "")


def _letters(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalpha())


def _initials(phrase: str) -> str:
    # Hyphenated scientific names contribute one initial per component:
    # Gires-Tournois interferometer -> GTI.
    initials: list[str] = []
    for token in _WORD.findall(phrase):
        initials.extend(part[0].upper() for part in token.split("-") if part)
    return "".join(initials)


def _singularize(value: str) -> str:
    words = value.split()
    if not words:
        return value
    replacements = {
        "interferometers": "interferometer",
        "thresholds": "threshold",
        "algorithms": "algorithm",
        "systems": "system",
        "mirrors": "mirror",
        "methods": "method",
    }
    words[-1] = replacements.get(words[-1].casefold(), words[-1])
    return " ".join(words)


def _term_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _explicit_candidates(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    pattern = re.compile(
        r"(?P<long>[A-Za-z][A-Za-z0-9/&,'-]*(?:\s+[A-Za-z][A-Za-z0-9/&,'-]*){1,9})"
        r"\s*\(\s*(?P<acronym>[A-Z][A-Z0-9-]{1,9})\s*\)"
    )
    for match in pattern.finditer(text):
        long_form = _singularize(" ".join(match.group("long").split()))
        acronym = match.group("acronym")
        if _initials(long_form) == _letters(acronym):
            found.setdefault(acronym, []).append(long_form)
    return found


def _ngram_candidates(text: str, acronym: str) -> list[str]:
    target = _letters(acronym)
    words = list(_WORD.finditer(text))
    candidates: list[str] = []
    # Do not match an acronym against every phrase in the source.  That
    # produces accidental expansions (for example, "guidance to identify"
    # for GTI).  Inferred phrases must occur near an actual acronym use.
    acronym_positions = [
        match.start()
        for match in re.finditer(rf"\b{re.escape(acronym)}\b", text)
    ]
    if not acronym_positions:
        return []
    for start in range(len(words)):
        for count in range(2, min(10, len(words) - start) + 1):
            phrase_start = words[start].start()
            phrase_end = words[start + count - 1].end()
            if not any(
                abs(position - phrase_start) <= 600
                or abs(position - phrase_end) <= 600
                for position in acronym_positions
            ):
                continue
            phrase = text[phrase_start:phrase_end]
            if _initials(phrase) == target and len(phrase) >= 5:
                candidates.append(_singularize(" ".join(phrase.split())))
    return candidates


def build_source_terminology_ledger(
    blueprint: Any = None,
    review: Any = "",
    context: Any = None,
) -> dict[str, Any]:
    """Build an acronym ledger from supplied source text, without inference."""

    sources = [
        ("blueprint", _flatten_source(blueprint)),
        ("review", _flatten_source(review)),
        ("context", _flatten_source(context)),
    ]
    candidates: dict[str, list[tuple[str, str, int]]] = {}
    for priority, (source_kind, text) in enumerate(sources):
        if not text:
            continue
        explicit = _explicit_candidates(text)
        # Only explicit ``long form (ACRONYM)`` definitions can establish a
        # canonical term.  Searching arbitrary nearby n-grams by initials
        # turns ordinary phrases such as "absence of" into fake expansions
        # for AO and makes a valid plan impossible to submit.  Bare acronym
        # uses remain unclassified instead of being guessed.
        acronyms = set(explicit)
        for acronym in acronyms:
            if acronym in _COMMON_ACRONYMS or len(_letters(acronym)) < 2:
                continue
            values = list(explicit.get(acronym) or [])
            for value in values:
                if _term_key(value):
                    candidates.setdefault(acronym, []).append(
                        (value, source_kind, priority)
                    )

    entries: list[dict[str, Any]] = []
    canonical: dict[str, str] = {}
    for acronym in sorted(candidates):
        grouped: dict[str, dict[str, Any]] = {}
        for value, source_kind, priority in candidates[acronym]:
            item = grouped.setdefault(
                _term_key(value),
                {"expansion": value, "sources": [], "priority": priority},
            )
            item["priority"] = min(item["priority"], priority)
            item["sources"].append(source_kind)
        values = sorted(
            grouped.values(),
            key=lambda item: (
                item["priority"],
                len(item["expansion"]),
                item["expansion"].casefold(),
            ),
        )
        status = "unambiguous" if len(values) == 1 else "ambiguous"
        entry = {
            "acronym": acronym,
            "status": status,
            "canonical_expansion": (
                values[0]["expansion"] if status == "unambiguous" else ""
            ),
            "candidates": [
                {
                    "expansion": item["expansion"],
                    "source_kinds": sorted(set(item["sources"])),
                }
                for item in values
            ],
        }
        entries.append(entry)
        if status == "unambiguous":
            canonical[acronym] = entry["canonical_expansion"]
    return {
        "schema_version": "research_harness.source_terminology_ledger.v1",
        "entries": entries,
        "canonical_expansions": canonical,
        "policy": (
            "Canonical expansions are copied from blueprint, review, or shared "
            "context text. Ambiguous source mappings are fail-closed."
        ),
    }


def _metric_definition_rows(plan: Any) -> list[Mapping[str, Any]]:
    if not isinstance(plan, Mapping):
        return []
    evaluation = plan.get("unified_evaluation")
    if not isinstance(evaluation, Mapping):
        return []
    rows = evaluation.get("metrics")
    if not isinstance(rows, (list, tuple)):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def build_canonical_token_registry(
    plan: Any,
    ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build canonical all-caps tokens from structured metric definitions and sources."""

    tokens: dict[str, dict[str, Any]] = {}

    def add_token(
        token: Any,
        *,
        metric_id: str = "",
        metric_name: str = "",
        source_acronym: bool = False,
    ) -> None:
        value = str(token or "").strip()
        if not value or not _CANONICAL_TOKEN.fullmatch(value):
            return
        item = tokens.setdefault(
            value,
            {
                "metric_ids": set(),
                "metric_names": set(),
                "source_acronym": False,
            },
        )
        if metric_id:
            item["metric_ids"].add(metric_id)
        if metric_name:
            item["metric_names"].add(metric_name)
        item["source_acronym"] = bool(
            item["source_acronym"] or source_acronym
        )

    for row in _metric_definition_rows(plan):
        metric_id = str(row.get("metric_id") or row.get("id") or "").strip()
        metric_name = str(row.get("name") or row.get("label") or "").strip()
        for token in _CANONICAL_TOKEN.findall(metric_name):
            add_token(token, metric_id=metric_id, metric_name=metric_name)

    for entry in (ledger or {}).get("entries", []):
        if not isinstance(entry, Mapping) or entry.get("status") != "unambiguous":
            continue
        add_token(entry.get("acronym"), source_acronym=True)

    serializable_tokens = {
        token: {
            "metric_ids": sorted(item["metric_ids"]),
            "metric_names": sorted(item["metric_names"]),
            "source_acronym": bool(item["source_acronym"]),
        }
        for token, item in sorted(tokens.items())
    }
    return {
        "schema_version": "research_harness.canonical_token_registry.v1",
        "canonical_tokens": sorted(serializable_tokens),
        "tokens": serializable_tokens,
    }


def _edit_distance_one(left: str, right: str) -> bool:
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    if len(left) > len(right):
        left, right = right, left
    index_left = index_right = differences = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
            index_right += 1
            continue
        differences += 1
        index_right += 1
        if differences > 1:
            return False
    return True


def _metric_ids_from_value(value: Any, known_metric_ids: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key in ("metric_id", "metric_ids", "metrics"):
            found.update(_metric_ids_from_value(value.get(key), known_metric_ids))
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found.update(_metric_ids_from_value(item, known_metric_ids))
        return found
    text = str(value or "").strip()
    if text in known_metric_ids:
        found.add(text)
    return found


def _normalize_canonical_tokens(
    value: Any,
    registry: Mapping[str, Any],
    *,
    path: str = "",
    metric_ids: set[str] | None = None,
    key: str = "",
    correct: bool = True,
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Normalize unique edit-distance-one canonical tokens in metric prose."""

    metric_ids = set(metric_ids or set())
    if "normalization_audit" in path:
        return value, [], []
    token_info = registry.get("tokens", {})
    canonical_tokens = set(token_info)
    known_metric_ids = {
        metric_id
        for item in token_info.values()
        if isinstance(item, Mapping)
        for metric_id in item.get("metric_ids", [])
    }

    if isinstance(value, Mapping):
        local_metric_ids = metric_ids | _metric_ids_from_value(
            value, known_metric_ids
        )
        output: dict[str, Any] = {}
        audits: list[dict[str, Any]] = []
        errors: list[str] = []
        for item_key, item in value.items():
            child_path = f"{path}.{item_key}" if path else str(item_key)
            normalized, child_audits, child_errors = _normalize_canonical_tokens(
                item,
                registry,
                path=child_path,
                metric_ids=local_metric_ids,
                key=str(item_key),
                correct=correct,
            )
            output[item_key] = normalized
            audits.extend(child_audits)
            errors.extend(child_errors)
        return output, audits, errors
    if isinstance(value, list):
        output: list[Any] = []
        audits: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, item in enumerate(value):
            normalized, child_audits, child_errors = _normalize_canonical_tokens(
                item,
                registry,
                path=f"{path}[{index}]",
                metric_ids=metric_ids,
                key=key,
                correct=correct,
            )
            output.append(normalized)
            audits.extend(child_audits)
            errors.extend(child_errors)
        return output, audits, errors
    if (
        not isinstance(value, str)
        or not token_info
        or key.casefold() in _STRUCTURAL_ID_KEYS
    ):
        return value, [], []

    metric_bearing = key.casefold() in _METRIC_BEARING_KEYS
    replacements: list[tuple[int, int, str, str, dict[str, Any]]] = []
    audits: list[dict[str, Any]] = []
    errors: list[str] = []
    for match in _CANONICAL_TOKEN.finditer(value):
        candidate = match.group(0)
        if candidate in canonical_tokens:
            continue
        matches = sorted(
            token
            for token in canonical_tokens
            if _edit_distance_one(candidate, token)
        )
        if len(matches) != 1:
            if len(matches) > 1 and metric_bearing:
                errors.append(
                    f"ambiguous_canonical_token:{path or '<root>'}:"
                    f"{candidate}:candidates={','.join(matches)}"
                )
            continue
        canonical = matches[0]
        info = token_info.get(canonical, {})
        owners = {
            str(item)
            for item in info.get("metric_ids", [])
            if str(item)
        }
        tied = bool(info.get("source_acronym")) and metric_bearing
        if metric_ids:
            tied = bool(not owners or owners & metric_ids)
        elif metric_bearing:
            tied = len(owners) == 1 or bool(info.get("source_acronym"))
        if not tied:
            continue
        if not correct:
            errors.append(
                f"noncanonical_canonical_token:{path or '<root>'}:"
                f"{candidate}:expected={canonical}"
            )
            continue
        replacements.append(
            (
                match.start(),
                match.end(),
                candidate,
                canonical,
                {
                    "action": "replace_noncanonical_canonical_token",
                    "path": path or "<root>",
                    "previous_token": candidate,
                    "canonical_token": canonical,
                    "metric_ids": sorted(owners & metric_ids) or sorted(owners),
                    "reason": (
                        "Unique edit-distance-one match to a canonical metric or "
                        "unambiguous source token in metric-bearing prose."
                    ),
                },
            )
        )
    for start, end, previous, canonical, audit in reversed(replacements):
        value = value[:start] + canonical + value[end:]
        audits.append(audit)
    audits.reverse()
    return value, audits, errors


def audit_plan_quality_warnings(plan: Mapping[str, Any]) -> list[str]:
    """Return nonblocking warnings for under-specified simulation comparisons."""

    project_type = str(plan.get("project_type") or "").casefold()
    relevant_text = _flatten_source(
        {
            "project_type": plan.get("project_type"),
            "boundaries": plan.get("boundaries"),
            "methods_summary": plan.get("methods_summary"),
            "experiments": plan.get("experiments"),
            "rationale": plan.get("rationale"),
            "narrative_markdown": plan.get("narrative_markdown"),
        }
    )
    excludes_actual_work = project_type in {"simulation", "theory"} and bool(
        _FABRICATION_EXCLUSION.search(relevant_text)
    )
    if not excludes_actual_work:
        return []
    warnings: list[str] = []
    for row in _metric_definition_rows(plan):
        name = str(row.get("name") or row.get("label") or "").strip()
        if not _SIMULATED_FABRICATED_METRIC.search(name):
            continue
        if _PUBLISHED_REFERENCE_FABRICATION_DATA.search(relevant_text):
            continue
        metric_id = str(row.get("metric_id") or row.get("id") or name).strip()
        warnings.append(
            "simulated_vs_fabricated_metric_lacks_explicit_published_reference_"
            f"fabrication_data:{metric_id}"
        )
    return list(dict.fromkeys(warnings))


def _definition_occurrences(text: str, acronym: str) -> list[tuple[int, int, str]]:
    occurrences: list[tuple[int, int, str]] = []
    for match in _PAREN_ACRONYM.finditer(text):
        if match.group("acronym") != acronym:
            continue
        segment_start = max(
            text.rfind(".", 0, match.start()),
            text.rfind("!", 0, match.start()),
            text.rfind("?", 0, match.start()),
            text.rfind("\n", 0, match.start()),
        ) + 1
        segment = text[segment_start:match.start()]
        words = list(_WORD.finditer(segment))
        candidate: tuple[int, int, str] | None = None
        for count in range(min(12, len(words)), 1, -1):
            start = words[-count].start()
            phrase = segment[start:words[-1].end()]
            if _initials(phrase) == _letters(acronym):
                candidate = (
                    segment_start + start,
                    segment_start + words[-1].end(),
                    phrase,
                )
                break
        if candidate is None:
            candidate = (segment_start, match.start(), segment.strip())
        occurrences.append((candidate[0], match.start(), candidate[2]))
    return occurrences


def _expand_replacement_start(text: str, start: int, segment_start: int) -> int:
    prefix = text[segment_start:start]
    words = list(_WORD.finditer(prefix))
    if not words:
        return start
    index = len(words) - 1
    while index >= 0:
        token = words[index].group().casefold()
        if token in _CONNECTORS or token in {
            "is", "are", "was", "were", "denotes", "called", "using",
            "uses", "via", "known", "termed", "named",
        }:
            break
        index -= 1
    return (
        segment_start + words[index + 1].start()
        if index + 1 < len(words)
        else start
    )


def normalize_source_terminology(
    value: Any,
    ledger: Mapping[str, Any] | None,
    *,
    path: str = "",
    correct: bool = True,
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Correct only unambiguous incompatible LONG FORM (ACRONYM) uses."""

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        audits: list[dict[str, Any]] = []
        errors: list[str] = []
        for key, item in value.items():
            if key in {"source_context", "normalization_audit"}:
                # Immutable provenance is an input to the plan, not generated
                # publication prose.  Preserve it byte-for-byte and audit only
                # the plan fields that the model authored.
                output[key] = item
                continue
            child_path = f"{path}.{key}" if path else str(key)
            output[key], child_audit, child_errors = normalize_source_terminology(
                item, ledger, path=child_path, correct=correct
            )
            audits.extend(child_audit)
            errors.extend(child_errors)
        return output, audits, errors
    if isinstance(value, list):
        output: list[Any] = []
        audits: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, item in enumerate(value):
            normalized, child_audit, child_errors = normalize_source_terminology(
                item, ledger, path=f"{path}[{index}]", correct=correct
            )
            output.append(normalized)
            audits.extend(child_audit)
            errors.extend(child_errors)
        return output, audits, errors
    if not isinstance(value, str) or not ledger:
        return value, [], []

    text = value
    audits: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in ledger.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        acronym = str(entry.get("acronym") or "")
        canonical = str(entry.get("canonical_expansion") or "")
        for start, end, phrase in reversed(_definition_occurrences(text, acronym)):
            local = text[start:end]
            if canonical and _term_key(canonical) in _term_key(local):
                continue
            location = path or "<root>"
            if entry.get("status") != "unambiguous":
                errors.append(f"ambiguous_acronym_expansion:{location}:{acronym}")
                continue
            if not canonical:
                continue
            if not correct:
                errors.append(
                    f"incompatible_acronym_expansion:{location}:{acronym}:"
                    f"expected={canonical}"
                )
                continue
            line_start = max(text.rfind("\n", 0, start) + 1, 0)
            # Include a contiguous modifier prefix before the incompatible
            # expansion, but stop at a sentence connector.  This prevents a
            # phrase such as "... compensated Grating-Tuned Interferometer"
            # from retaining the wrong modifier after replacement.
            replacement_start = _expand_replacement_start(
                text, end, line_start
            )
            previous = text[replacement_start:end]
            separator = " " if end > 0 and text[end - 1].isspace() else ""
            text = text[:replacement_start] + canonical + separator + text[end:]
            audits.append(
                {
                    "action": "replace_incompatible_acronym_expansion",
                    "path": location,
                    "acronym": acronym,
                    "previous_expansion": previous.strip(),
                    "canonical_expansion": canonical,
                    "reason": (
                        "Copied unambiguous source terminology; no scientific "
                        "claim was inferred."
                    ),
                }
            )
    return text, audits, errors


def _label_numeric_sentence(sentence: str) -> str:
    compact = " ".join(sentence.split()).strip()
    if not compact or not _PROGRAM_NUMBER.search(compact):
        return sentence
    if _SOURCE_MARKER.search(compact) or _NORMALIZED_NUMERIC_MARKER.search(compact):
        return sentence
    lower = compact.casefold()
    labels: list[str] = []
    if any(token in lower for token in ("wavelength", "operating", "scope", "band")):
        labels.append("Proposed program scope")
    if any(
        token in lower
        for token in ("sigma", "distribution", "sampling", "realization", "replicate")
    ):
        labels.append("Proposed calibration distribution")
    if any(token in lower for token in ("month", "timeline", "budget", "gpu", "cost")):
        labels.append("Proposed program schedule or budget")
    if any(token in lower for token in ("threshold", "accuracy", "falsif", "sample")):
        labels.append("Proposed calibration target")
    if not labels:
        labels.append("Proposed program parameter")
    return "; ".join(labels) + " (verification_deferred): " + sentence


def normalize_quantitative_program_text(value: Any) -> str:
    """Label unreferenced numeric planning prose while preserving cited facts."""

    text = str(value or "")
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(_label_numeric_sentence(part) for part in parts)


def normalize_plan_quality(
    plan: Mapping[str, Any],
    ledger: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    registry = build_canonical_token_registry(plan, ledger)
    normalized, audits, errors = normalize_source_terminology(
        dict(plan), ledger, correct=True
    )
    normalized, token_audits, token_errors = _normalize_canonical_tokens(
        normalized,
        registry,
        correct=True,
    )
    audits.extend(token_audits)
    errors.extend(token_errors)

    def walk(value: Any, path: str = "", key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                item_key: walk(
                    item,
                    f"{path}.{item_key}" if path else item_key,
                    str(item_key),
                )
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [
                walk(item, f"{path}[{index}]", key)
                for index, item in enumerate(value)
            ]
        if isinstance(value, str) and key.casefold() not in _SKIP_NUMERIC_KEYS:
            return normalize_quantitative_program_text(value)
        return value

    normalized = walk(normalized)
    return normalized, audits, errors


def audit_plan_quality(
    plan: Mapping[str, Any],
    ledger: Mapping[str, Any] | None = None,
) -> list[str]:
    registry = build_canonical_token_registry(plan, ledger)
    _, _, errors = normalize_source_terminology(
        dict(plan), ledger, correct=False
    )
    _, _, token_errors = _normalize_canonical_tokens(
        dict(plan),
        registry,
        correct=False,
    )
    errors.extend(token_errors)
    return list(dict.fromkeys(errors))
