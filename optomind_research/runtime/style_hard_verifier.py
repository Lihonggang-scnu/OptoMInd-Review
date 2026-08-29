"""Deterministic hard verification for LLM style rewrites.

Style judgement belongs to an LLM; this module never judges style.  It only
answers one question: did a rewrite preserve every fact-bearing token of the
original paragraph?  Any failure causes the caller to discard the rewrite.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence

# Inline evidence markers produced upstream, e.g. [REF:s2:abc123].
_REF = re.compile(r"\[REF:([^\]]+)\]")
# Numbers including decimals, exponents, ranges and percentages.
_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# Units and physical quantities common in this corpus.
_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:nm|um|µm|mm|cm|m|THz|GHz|MHz|Hz|dB|deg|°|%|NA|"
    r"eV|meV|fs|ps|ns|ms|s|W|mW|K|C)\b",
    re.IGNORECASE,
)
# Inline math and LaTeX fragments must survive byte-identical.
_MATH = re.compile(r"\$[^$]+\$|\\\([^)]*\\\)|\\\[[^\]]*\\\]")
# Hedges carry scientific caution; silently deleting one changes the claim.
_HEDGE = re.compile(
    r"\b(?:may|might|could|can|appears?|suggests?|indicates?|likely|"
    r"potentially|possibly|approximately|roughly|about|nearly|typically|"
    r"generally|often|sometimes|partially|preliminary|reportedly|"
    r"under (?:certain|specific) conditions|in principle|to date|so far)\b",
    re.IGNORECASE,
)


def _multiset(
    pattern: re.Pattern,
    text: str,
    *,
    fold_case: bool = False,
) -> Dict[str, int]:
    """Count pattern occurrences, case-folding only where case is not a fact.

    ``fold_case`` must stay False for citations, numbers, units and math: in
    this corpus case *is* the fact.  ``$\\Lambda$`` (grating period) and
    ``$\\lambda$`` (wavelength) are different quantities; ``100 mW`` and
    ``100 MW`` differ by 10**6; ``5 ms`` and ``5 Ms`` by 10**9.  Folding those
    to one key silently turned ``exact=True`` into "exact up to case", so a
    rewrite could rescale a measurement and still be accepted.  Only hedges
    fold, because ``May`` at a sentence start and ``may`` mid-sentence carry
    the same epistemic caution.
    """

    counts: Dict[str, int] = {}
    for match in pattern.findall(text):
        key = match if isinstance(match, str) else str(match)
        key = key.strip()
        if fold_case:
            key = key.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _protected_term_count(text: str, term: str) -> int:
    """Count a protected term as a token, not as an arbitrary substring.

    The old ``str.count`` check treated the acronym ``HIL`` as present in
    ``While`` (the letters ``hil`` occur contiguously), so every safe opener
    rewrite was rejected when the terminology ledger contained HIL.  ASCII
    scientific terms are matched on token boundaries; an optional lowercase
    plural suffix keeps canonical acronyms such as ``ODNN`` protected inside
    ``ODNNs``.  Phrases or non-ASCII notation retain a conservative exact
    case-folded substring fallback.
    """

    needle = str(term or "").strip()
    if not needle:
        return 0
    if re.fullmatch(r"[A-Za-z0-9]+(?:[ _-][A-Za-z0-9]+)*", needle):
        plural_suffix = "s?" if needle.isupper() and len(needle) >= 2 else ""
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(needle)}{plural_suffix}"
            rf"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        return len(pattern.findall(str(text or "")))
    return str(text or "").casefold().count(needle.casefold())


def verify_rewrite(
    *,
    original: str,
    rewritten: str,
    protected_terms: List[str] | None = None,
    protected_term_aliases: Mapping[str, str | Sequence[str]] | None = None,
    max_length_ratio: float = 1.35,
    min_length_ratio: float = 0.70,
) -> Dict[str, Any]:
    """Return a verdict describing whether a rewrite is safe to accept.

    ok is True only when every fact-bearing multiset is preserved.  The
    caller must discard the rewrite whenever ok is False.
    """

    violations: List[Dict[str, Any]] = []

    def _compare(name: str, pattern: re.Pattern, *, exact: bool) -> None:
        # ``exact`` also selects the key policy: fact-bearing tokens compare
        # case-sensitively, hedges fold.  The two always move together, so
        # they are derived from one flag rather than passed separately.
        before = _multiset(pattern, original, fold_case=not exact)
        after = _multiset(pattern, rewritten, fold_case=not exact)
        if exact:
            if before != after:
                violations.append(
                    {
                        "check": name,
                        "missing": {
                            k: before[k] - after.get(k, 0)
                            for k in before
                            if before[k] > after.get(k, 0)
                        },
                        "added": {
                            k: after[k] - before.get(k, 0)
                            for k in after
                            if after[k] > before.get(k, 0)
                        },
                    }
                )
            return
        # Non-exact checks forbid loss but tolerate rephrasing that adds.
        missing = {
            k: before[k] - after.get(k, 0)
            for k in before
            if before[k] > after.get(k, 0)
        }
        if missing:
            violations.append({"check": name, "missing": missing, "added": {}})

    _compare("citations", _REF, exact=True)
    _compare("numbers", _NUMBER, exact=True)
    _compare("units", _UNIT, exact=True)
    _compare("math", _MATH, exact=True)
    _compare("hedges", _HEDGE, exact=False)

    aliases = dict(protected_term_aliases or {})
    aliases_folded = {
        str(key).strip().casefold(): value
        for key, value in aliases.items()
        if str(key).strip()
    }
    for term in protected_terms or []:
        needle = term.strip()
        if not needle:
            continue
        before_count = _protected_term_count(original, needle)
        after_count = _protected_term_count(rewritten, needle)
        alias_values = aliases.get(needle)
        if alias_values is None:
            alias_values = aliases_folded.get(needle.casefold(), ())
        if isinstance(alias_values, str):
            alias_values = (alias_values,)
        # A terminology ledger may protect a component phrase such as
        # ``Optical Neural Networks`` while the abbreviation inventory maps
        # the complete occurrence ``Diffractive optical neural networks`` to
        # ``DONNs``.  Treat that nested component as covered only when the
        # caller supplied this exact, already-validated full-form alias.  This
        # keeps the exception narrow and prevents arbitrary term deletion.
        nested_alias_values = list(alias_values)
        for full_form, full_alias in aliases.items():
            if (
                _protected_term_count(str(full_form), needle) > 0
                and _protected_term_count(original, str(full_form)) > 0
            ):
                nested_alias_values.append(str(full_alias))
        alias_count = sum(
            _protected_term_count(rewritten, str(alias))
            for alias in nested_alias_values
            if str(alias).strip()
        )
        # Abbreviation normalization is the one intentional exception to the
        # ordinary protected-term conservation rule: a reviewed occurrence of
        # ``full name (ACRONYM)`` may become ``ACRONYM``.  The caller must
        # supply this narrow alias map only after validating that exact edit.
        if before_count > after_count + alias_count:
            violations.append({"check": "protected_term", "term": needle})

    original_length = max(1, len(original.split()))
    ratio = len(rewritten.split()) / original_length
    if ratio > max_length_ratio or ratio < min_length_ratio:
        violations.append({"check": "length_ratio", "ratio": round(ratio, 3)})

    if not rewritten.strip():
        violations.append({"check": "empty_rewrite"})

    return {
        "schema_version": "optomind.style_hard_verification.v1",
        "ok": not violations,
        "violations": violations,
        "length_ratio": round(ratio, 3),
    }
