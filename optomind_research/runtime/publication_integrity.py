"""Publication-facing integrity transforms shared by review and plan outputs.

This module deliberately sits *after* scientific reasoning and *before*
Markdown/LaTeX rendering.  It does not invent content.  Its job is to turn a
traceable internal draft into reader-facing prose without leaking workflow
labels, fragmenting mathematical expressions, or repeatedly defining the same
acronym.  The functions are deterministic so the same upstream asset always
produces the same publication source.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_GREEK_TEX = {
    "\u0393": r"\Gamma", "\u0394": r"\Delta", "\u0398": r"\Theta",
    "\u039b": r"\Lambda", "\u039e": r"\Xi", "\u03a0": r"\Pi",
    "\u03a3": r"\Sigma", "\u03a5": r"\Upsilon", "\u03a6": r"\Phi",
    "\u03a8": r"\Psi", "\u03a9": r"\Omega", "\u03b1": r"\alpha",
    "\u03b2": r"\beta", "\u03b3": r"\gamma", "\u03b4": r"\delta",
    "\u03b5": r"\epsilon", "\u03f5": r"\varepsilon", "\u03b6": r"\zeta",
    "\u03b7": r"\eta", "\u03b8": r"\theta", "\u03d1": r"\vartheta",
    "\u03b9": r"\iota", "\u03ba": r"\kappa", "\u03f0": r"\varkappa",
    "\u03bb": r"\lambda", "\u03bc": r"\mu", "\u03bd": r"\nu",
    "\u03be": r"\xi", "\u03c0": r"\pi", "\u03d6": r"\varpi",
    "\u03c1": r"\rho", "\u03f1": r"\varrho", "\u03c3": r"\sigma",
    "\u03c2": r"\varsigma", "\u03c4": r"\tau", "\u03c5": r"\upsilon",
    "\u03c6": r"\phi", "\u03d5": r"\varphi", "\u03c7": r"\chi",
    "\u03c8": r"\psi", "\u03c9": r"\omega",
}

_SYMBOL_TEX = {
    "\u00b1": r"\pm", "\u2213": r"\mp", "\u00d7": r"\times",
    "\u00f7": r"\div", "\u00b7": r"\cdot", "\u2219": r"\cdot",
    "\u2264": r"\leq", "\u2265": r"\geq", "\u226a": r"\ll",
    "\u226b": r"\gg", "\u2248": r"\approx", "\u2243": r"\simeq",
    "\u2245": r"\cong", "\u2260": r"\neq", "\u2261": r"\equiv",
    "\u221d": r"\propto", "\u221e": r"\infty", "\u2202": r"\partial",
    "\u2207": r"\nabla", "\u2208": r"\in", "\u2209": r"\notin",
    "\u220b": r"\ni", "\u2282": r"\subset", "\u2286": r"\subseteq",
    "\u2283": r"\supset", "\u2287": r"\supseteq", "\u222a": r"\cup",
    "\u2229": r"\cap", "\u2211": r"\sum", "\u220f": r"\prod",
    "\u222b": r"\int", "\u222e": r"\oint", "\u2227": r"\wedge",
    "\u2228": r"\vee", "\u00ac": r"\neg", "\u2295": r"\oplus",
    "\u2297": r"\otimes", "\u22a5": r"\perp", "\u2225": r"\parallel",
    "\u2220": r"\angle", "\u2192": r"\rightarrow", "\u2190": r"\leftarrow",
    "\u2194": r"\leftrightarrow", "\u21d2": r"\Rightarrow",
    "\u21d0": r"\Leftarrow", "\u21d4": r"\Leftrightarrow",
    "\u21a6": r"\mapsto", "\u2191": r"\uparrow", "\u2193": r"\downarrow",
    "\u223c": r"\sim", "\u210f": r"\hbar", "\u2113": r"\ell",
    "\u211c": r"\Re", "\u2111": r"\Im", "\u27e8": r"\langle",
    "\u27e9": r"\rangle", "\u2329": r"\langle", "\u232a": r"\rangle",
    "\u00b0": r"^\circ",
}

_SUPERSCRIPT_TRANSLATION = str.maketrans(
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e\u207f",
    "0123456789+-=()n",
)
_SUPERSCRIPT_EXTRA = {"\u1d40": "T"}
_SUPERSCRIPT_CHARS = (
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"
    "\u207a\u207b\u207c\u207d\u207e\u207f\u1d40"
)
_SUBSCRIPT_MAP = {
    **dict(zip("\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e", "0123456789+-=()")),
    "\u2090": "a", "\u2091": "e", "\u2095": "h", "\u1d62": "i",
    "\u2c7c": "j", "\u2096": "k", "\u2097": "l", "\u2098": "m",
    "\u2099": "n", "\u2092": "o", "\u209a": "p", "\u1d63": "r",
    "\u209b": "s", "\u209c": "t", "\u2093": "x",
}
_PROTECTED_PATTERN = re.compile(
    r"```.*?```|`[^`\n]*`|\[REF:[^\]]+\]|\[@[^\]]+\]|"
    r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$",
    re.DOTALL,
)
_EXISTING_MATH_PATTERN = re.compile(
    r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$|"
    r"\\\(.*?\\\)|\\\[.*?\\\]",
    re.DOTALL,
)
# A Python string containing ``\tau`` becomes a literal tab followed by
# ``au`` when the backslash is not escaped before JSON serialization.  LLM
# generated manuscript fragments have exhibited exactly that corruption.  A
# small, math-only repair is safe; ordinary prose tabs are still normalized to
# spaces below and are never interpreted as TeX commands.
_CORRUPTED_TEX_TAB_COMMANDS = {
    "au": r"\tau",
    "heta": r"\theta",
    "imes": r"\times",
    "ext": r"\text",
}
_FORMULA_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<lhs>[A-Za-z\u0370-\u03ff](?:[A-Za-z0-9\u0370-\u03ff\u2070-\u209f"
    r"\u00b2\u00b3\u00b9\u1d40_\u00b1]){0,14})"
    r"\s*(?P<operator>=|\u221d|\u223c|\u2248|\u2264|\u2265|\u2243|\u2245|\u2260)\s*"
    r"(?P<rhs>[A-Za-z0-9\u0370-\u03ff\u2070-\u209f\u00b2\u00b3\u00b9\u1d40"
    r"_\u00b1\u2213\u221a\u2211\u03a3\u220f\u222b\u221d\u223c\u2248\u2264"
    r"\u2265\u00d7\u00b7+*/^()\-\u2212\s]+?)"
    r"(?=(?:[.,;:\uff0c\u3002\uff1b\uff1a](?:\s|(?=[\u3400-\u9fff])|$)|"
    r"\s+(?=[\u3400-\u9fff])|$))"
)
_ASCII_GREEK_ALIASES = {
    "Alpha": r"\Alpha", "Beta": r"\Beta", "Gamma": r"\Gamma",
    "Delta": r"\Delta", "Theta": r"\Theta", "Lambda": r"\Lambda",
    "Xi": r"\Xi", "Pi": r"\Pi", "Sigma": r"\Sigma",
    "Phi": r"\Phi", "Psi": r"\Psi", "Omega": r"\Omega",
    "alpha": r"\alpha", "beta": r"\beta", "gamma": r"\gamma",
    "delta": r"\delta", "epsilon": r"\epsilon", "varepsilon": r"\varepsilon",
    "zeta": r"\zeta", "eta": r"\eta", "theta": r"\theta",
    "iota": r"\iota", "kappa": r"\kappa", "lambda": r"\lambda",
    "mu": r"\mu", "nu": r"\nu", "xi": r"\xi", "pi": r"\pi",
    "rho": r"\rho", "sigma": r"\sigma", "tau": r"\tau",
    "upsilon": r"\upsilon", "phi": r"\phi", "chi": r"\chi",
    "psi": r"\psi", "omega": r"\omega",
}
_ASCII_GREEK_NAMES = "|".join(
    sorted((re.escape(value) for value in _ASCII_GREEK_ALIASES), key=len, reverse=True)
)
_ASCII_POWER_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?P<base>(?:{_ASCII_GREEK_NAMES})|"
    r"[\u0370-\u03ff]|[A-Za-z][A-Za-z0-9]*)\s*\^\s*"
    r"\((?P<exponent>[A-Za-z0-9+*/.\-]+)\)"
)
_ASCII_NUMERIC_POWER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<base>\d+(?:\.\d+)?)\s*\^\s*(?P<exponent>\d+)(?![A-Za-z0-9])"
)
# Big-O complexity notation is scientific math even when it appears inline in
# otherwise ordinary prose.  Keep this deliberately narrow: recognizing
# ``O(N^2)``/``O(N^3)``-style expressions avoids treating arbitrary prose with a
# caret as mathematics while preventing the publication hazard audit from
# flagging a legitimate scaling law as an unresolved formula.
_ASCII_COMPLEXITY_POWER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<order>O)\s*\(\s*"
    r"(?P<base>[A-Za-z])\s*\^\s*(?P<exponent>\d+)\s*\)"
)
_ASCII_SUBSCRIPT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?P<base>[A-Za-z\u0370-\u03ff])_\s*"
    rf"(?P<sub>(?:(?:{_ASCII_GREEK_NAMES})|[A-Za-z0-9]|[\u0370-\u03ff])+)\b"
)
_ASCII_VARIATION_PATTERN = re.compile(
    rf"\b(?P<symbol>Delta|delta|epsilon|alpha|beta|chi|sigma|kappa)\s+(?P<variable>[A-Za-z])\b"
)
_ASCII_NORM_PRODUCT_PATTERN = re.compile(
    r"\|\|(?P<left>[A-Za-z][A-Za-z0-9]*)\|\|\s*\^\s*(?P<left_power>\d+)"
    r"\s*\|\|(?P<right>[A-Za-z][A-Za-z0-9]*)\|\|\s*\^\s*(?P<right_power>\d+)"
)
_ASCII_NORM_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?P<lhs>[A-Za-z][A-Za-z0-9]*)\s*=\s*"
    r"\|\|(?P<left>[A-Za-z][A-Za-z0-9]*)\|\|\s*\^\s*(?P<left_power>\d+)"
    r"\s*\|\|(?P<right>[A-Za-z][A-Za-z0-9]*)\|\|\s*\^\s*(?P<right_power>\d+)"
)
_ASCII_MATRIX_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?P<lhs>[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)?)\s*=\s*"
    r"(?P<matrix>\[\s*\[[^\]\n]{1,60}\]\s*,\s*\[[^\]\n]{1,60}\]\s*\])"
)
_ASCII_RATIO_ASSIGNMENT_PATTERN = re.compile(
    rf"\b(?P<lhs>(?:{_ASCII_GREEK_NAMES})|[A-Za-z])\s*=\s*"
    r"(?P<rhs>[A-Za-z0-9]+\s*/\s*[A-Za-z0-9]+)\b"
)
_DERIVATIVE_ASSIGNMENT_PATTERN = re.compile(
    rf"\b(?P<lhs>(?:{_ASCII_GREEK_NAMES})|[\u03b1-\u03c9\u0391-\u03a9]|[A-Za-z])\s*=\s*"
    r"(?:partial|\u2202)\s+(?P<numerator>[A-Za-z][A-Za-z0-9_-]*)\s*/\s*"
    rf"(?:partial|\u2202)\s*(?P<denominator>(?:{_ASCII_GREEK_NAMES})|[\u03b1-\u03c9\u0391-\u03a9]|[A-Za-z][A-Za-z0-9_-]*)"
)
_FORMULA_TRAILING_PROSE = re.compile(
    r"\s+(?=(?:applies|becomes|holds|is|are|remains|which|that|when|"
    r"on|where|with|for|from|at|in|under|near|after|before)\b)",
    re.IGNORECASE,
)
_UNICODE_SUBSCRIPT_CHARS = "".join(_SUBSCRIPT_MAP)
_UNICODE_SUBSCRIPT_SEQUENCE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?P<base>[A-Za-z\u0370-\u03ff])"
    rf"(?P<subs>[{re.escape(_UNICODE_SUBSCRIPT_CHARS)}]+"
    rf"(?:,[{re.escape(_UNICODE_SUBSCRIPT_CHARS)}]+)*)"
)
_UNICODE_VARIATION_PATTERN = re.compile(
    r"(?P<symbol>[\u0394\u03b4\u03b5\u03b1\u03b2\u03c7\u03c3\u03ba])\s+(?P<variable>[A-Za-z])\b"
)
_UNRESOLVED_FORMULA_HAZARDS = (
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*\s*\^\s*(?:\(|\d)"),
    re.compile(r"\b[A-Za-z]\s*_\s*(?:[A-Za-z]|\d)"),
    re.compile(r"\b(?:Delta|delta|epsilon|alpha|beta|chi|sigma|kappa)\s+[A-Za-z]\b"),
)
_ACRONYM_PATTERN = re.compile(
    r"\b(?P<long>[A-Z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+){1,11})\s*"
    r"\((?P<short>[A-Z][A-Z0-9-]{1,14})\)"
)
_ACRONYM_NONTERM_WORDS = {
    "am", "are", "be", "been", "can", "could", "did", "do", "does",
    "had", "has", "have", "is", "may", "might", "must", "provide",
    "provides", "require", "requires", "should", "will", "would",
}
_INTERNAL_PATTERNS = (
    re.compile(r"\b(?:coverage_sufficient|needs_more_literature|all_gates_passed|"
               r"downstream_research_plan_ready|review_harness|task\.json|agent_state|"
               r"validation_passed)\b", re.I),
    re.compile(r"\b(?:we|this review)\s+(?:promise|commit to|will deliver)\s+"
               r"(?:\w+\s+){0,3}(?:deliverables|artifacts|outputs)\b", re.I),
)


def _protect(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def repl(match: re.Match[str]) -> str:
        token = f"OMINTEGRITYPROTECTED{len(protected):06d}TOKEN"
        protected.append(match.group(0))
        return token

    return _PROTECTED_PATTERN.sub(repl, text), protected


def _restore(text: str, protected: list[str]) -> str:
    for index, value in enumerate(protected):
        text = text.replace(f"OMINTEGRITYPROTECTED{index:06d}TOKEN", value)
    return text


def _normalize_existing_math_spans(text: str) -> str:
    """Repair Unicode math and JSON-damaged TeX inside existing math spans.

    ``normalize_formula_spans`` deliberately protects already-delimited math
    so it does not double-wrap valid expressions.  That protection used to
    leave Unicode ``κ``/``≈`` and a literal-tab form of ``\tau`` untouched,
    allowing a document to compile while displaying a damaged formula.
    Normalize only the contents of recognized math delimiters and leave code
    and prose semantics unchanged.
    """

    def replace(match: re.Match[str]) -> str:
        span = match.group(0)
        if span.startswith("$$") and span.endswith("$$"):
            open_len = close_len = 2
        elif span.startswith((r"\(", r"\[")) and span.endswith(
            (r"\)", r"\]"),
        ):
            open_len = close_len = 2
        else:
            open_len = close_len = 1
        prefix = span[:open_len]
        suffix = span[len(span) - close_len :]
        body = span[open_len : len(span) - close_len]
        for suffix_text, command in _CORRUPTED_TEX_TAB_COMMANDS.items():
            body = re.sub(
                r"\t" + re.escape(suffix_text),
                lambda _match, value=command: value,
                body,
            )
        body = body.replace("\t", " ").replace("\r", " ")
        for char, command in {**_GREEK_TEX, **_SYMBOL_TEX}.items():
            body = body.replace(char, command)
        return prefix + body + suffix

    return _EXISTING_MATH_PATTERN.sub(replace, str(text or ""))


def _tex_atom(char: str) -> str:
    return _GREEK_TEX.get(char, _SYMBOL_TEX.get(char, char))


def _translate_superscript(value: str) -> str:
    return "".join(
        _SUPERSCRIPT_EXTRA.get(char, char.translate(_SUPERSCRIPT_TRANSLATION))
        for char in value
    )


def _formula_to_tex(value: str) -> str:
    """Convert one bounded scientific expression as one math span.

    Character-level conversion was the historical failure mode: it emitted
    adjacent `$\\epsilon$` and `$^{2/3}$` spans that Pandoc/XeLaTeX could
    legally compile but rendered as broken prose.  This parser is intentionally
    modest; unsupported material remains text and is audited later.
    """

    # NFKC turns ``²`` into the plain digit ``2`` and destroys exponent
    # semantics.  NFC preserves scientific superscripts while still giving us
    # stable composed Unicode.
    source = unicodedata.normalize("NFC", value).replace("\u2212", "-")
    source = source.replace("\u03a3_", "\u2211_")
    for name, latex in _ASCII_GREEK_ALIASES.items():
        source = re.sub(
            rf"\b{re.escape(name)}\b",
            lambda _: latex,
            source,
        )
    output: list[str] = []
    index = 0
    supers = _SUPERSCRIPT_CHARS
    subs = "".join(_SUBSCRIPT_MAP)
    while index < len(source):
        char = source[index]
        if char == "\u221a":
            if index + 1 < len(source) and source[index + 1] == "(":
                depth = 1
                end = index + 2
                while end < len(source) and depth:
                    if source[end] == "(":
                        depth += 1
                    elif source[end] == ")":
                        depth -= 1
                    end += 1
                if depth == 0:
                    operand = _formula_to_tex(source[index + 2 : end - 1])
                    output.append(r"\sqrt{" + operand + "}")
                    index = end
                    continue
            if index + 1 < len(source):
                operand = _tex_atom(source[index + 1])
                output.append(r"\sqrt{" + operand + "}")
                index += 2
                continue
            output.append(r"\sqrt{\cdot}")
        elif char in supers:
            end = index
            while end < len(source) and source[end] in supers:
                end += 1
            output.append("^{" + _translate_superscript(source[index:end]) + "}")
            index = end
            continue
        elif char in subs:
            end = index
            while end < len(source) and source[end] in subs:
                end += 1
            output.append("_{" + "".join(_SUBSCRIPT_MAP[item] for item in source[index:end]) + "}")
            index = end
            continue
        elif char == "^" and index + 1 < len(source) and source[index + 1] == "(":
            end = source.find(")", index + 2)
            if end > index:
                inside = _formula_to_tex(source[index + 2:end])
                output.append("^{(" + inside + ")}")
                index = end + 1
                continue
            output.append("^")
        elif char == "\u00b1" and output and (index + 1 == len(source) or source[index + 1].isspace() or source[index + 1] == "="):
            output.append(r"_{\pm}")
        else:
            atom = _tex_atom(char)
            # TeX treats ``\\DeltaR`` as an unknown control sequence.  Insert
            # a harmless math-space whenever a named Greek command is followed
            # by another letter or command-producing glyph.
            if (
                char in _GREEK_TEX
                and index + 1 < len(source)
                and (
                    source[index + 1].isalpha()
                    or source[index + 1] in _GREEK_TEX
                )
            ):
                atom += " "
            output.append(atom)
        index += 1
    return "".join(output)


def _replace_outside_protected(
    text: str,
    pattern: re.Pattern[str],
    replacement: Any,
) -> str:
    """Apply a safe normalizer without entering existing code/math spans."""

    protected_text, protected = _protect(text)
    normalized = pattern.sub(replacement, protected_text)
    return _restore(normalized, protected)


def _tex_ascii_atom(value: str) -> str:
    """Convert ASCII Greek aliases only while a span is known to be math."""

    output = str(value or "")
    for name, latex in _ASCII_GREEK_ALIASES.items():
        output = re.sub(
            rf"\b{re.escape(name)}\b",
            lambda _: latex,
            output,
        )
    return output


def _derivative_match_to_tex(match: re.Match[str]) -> str:
    """Render an already-recognized derivative expression as a single span."""

    numerator = re.sub(r"[^A-Za-z0-9]", "", match.group("numerator"))
    lhs = _formula_to_tex(match.group("lhs"))
    denominator = _formula_to_tex(match.group("denominator"))
    return (
        lhs + r" = \frac{\partial \mathrm{" + numerator
        + r"}}{\partial " + denominator + "}"
    )


def normalize_ascii_math_spans(text: str) -> tuple[str, list[dict[str, str]]]:
    """Normalize common LLM-emitted ASCII scientific notation as whole spans.

    Scientific models often produce readable plain text such as ``delta^(1/N)``
    or ``10^6``.  Pandoc escapes those characters literally, so a document can
    compile while the mathematical meaning is visually broken.  These bounded
    patterns cover high-frequency scientific notation without guessing at prose.
    """

    assets: list[dict[str, str]] = []

    def capture(raw: str, latex: str) -> str:
        assets.append({"raw": raw, "latex": latex})
        return "$" + latex + "$"

    def matrix_repl(match: re.Match[str]) -> str:
        lhs = match.group("lhs")
        lhs_tex = re.sub(
            r"_([A-Za-z0-9]+)$",
            lambda item: "_{\\mathrm{" + item.group(1) + "}}",
            lhs,
        )
        matrix = match.group("matrix")
        raw = match.group(0)
        return capture(raw, lhs_tex + " = " + matrix)

    def norm_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        latex = (
            r"\lVert " + _tex_ascii_atom(match.group("left"))
            + r"\rVert^{" + match.group("left_power") + r"}"
            + r"\lVert " + _tex_ascii_atom(match.group("right"))
            + r"\rVert^{" + match.group("right_power") + r"}"
        )
        return capture(raw, latex)

    def norm_assignment_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        latex = (
            _tex_ascii_atom(match.group("lhs"))
            + r" = \lVert " + _tex_ascii_atom(match.group("left"))
            + r"\rVert^{" + match.group("left_power") + r"}"
            + r"\lVert " + _tex_ascii_atom(match.group("right"))
            + r"\rVert^{" + match.group("right_power") + r"}"
        )
        return capture(raw, latex)

    def power_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        base = match.group("base")
        base_tex = (
            _formula_to_tex(base)
            if re.search(r"[\u0370-\u03ff]", base)
            else _tex_ascii_atom(base)
        )
        return capture(
            raw,
            base_tex + "^{(" + _tex_ascii_atom(match.group("exponent")) + ")}",
        )

    def numeric_power_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(raw, match.group("base") + "^{" + match.group("exponent") + "}")

    def complexity_power_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(
            raw,
            match.group("order")
            + "("
            + match.group("base")
            + "^{"
            + match.group("exponent")
            + "})",
        )

    def ratio_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(
            raw,
            _tex_ascii_atom(match.group("lhs")) + " = " + _tex_ascii_atom(match.group("rhs")),
        )

    def derivative_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(raw, _derivative_match_to_tex(match))

    def subscript_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(
            raw,
            _formula_to_tex(match.group("base"))
            + "_{" + _formula_to_tex(match.group("sub")) + "}",
        )

    def unicode_subscript_sequence_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        subs = "".join(
            "," if char == "," else _SUBSCRIPT_MAP[char]
            for char in match.group("subs")
        )
        return capture(
            raw,
            _formula_to_tex(match.group("base")) + "_{" + subs + "}",
        )

    def variation_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(raw, _tex_ascii_atom(match.group("symbol")) + " " + match.group("variable"))

    def unicode_variation_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return capture(raw, _tex_atom(match.group("symbol")) + " " + match.group("variable"))

    normalized = _replace_outside_protected(text, _ASCII_MATRIX_ASSIGNMENT_PATTERN, matrix_repl)
    normalized = _replace_outside_protected(normalized, _ASCII_NORM_ASSIGNMENT_PATTERN, norm_assignment_repl)
    normalized = _replace_outside_protected(normalized, _ASCII_NORM_PRODUCT_PATTERN, norm_repl)
    normalized = _replace_outside_protected(normalized, _ASCII_POWER_PATTERN, power_repl)
    normalized = _replace_outside_protected(normalized, _ASCII_NUMERIC_POWER_PATTERN, numeric_power_repl)
    normalized = _replace_outside_protected(
        normalized,
        _ASCII_COMPLEXITY_POWER_PATTERN,
        complexity_power_repl,
    )
    normalized = _replace_outside_protected(normalized, _ASCII_RATIO_ASSIGNMENT_PATTERN, ratio_repl)
    normalized = _replace_outside_protected(normalized, _DERIVATIVE_ASSIGNMENT_PATTERN, derivative_repl)
    normalized = _replace_outside_protected(
        normalized,
        _UNICODE_SUBSCRIPT_SEQUENCE_PATTERN,
        unicode_subscript_sequence_repl,
    )
    normalized = _replace_outside_protected(normalized, _ASCII_SUBSCRIPT_PATTERN, subscript_repl)
    normalized = _replace_outside_protected(normalized, _ASCII_VARIATION_PATTERN, variation_repl)
    normalized = _replace_outside_protected(normalized, _UNICODE_VARIATION_PATTERN, unicode_variation_repl)
    return normalized, assets


def find_unresolved_formula_hazards(text: str) -> list[str]:
    """Return unprotected formula-like fragments that would render as prose."""

    protected_text, _ = _protect(text)
    hazards: list[str] = []
    for pattern in _UNRESOLVED_FORMULA_HAZARDS:
        hazards.extend(match.group(0) for match in pattern.finditer(protected_text))
    return sorted(set(hazards))


def normalize_formula_spans(text: str) -> tuple[str, list[dict[str, str]]]:
    """Wrap recognized scientific expressions as a single TeX math span."""

    protected_text, protected = _protect(text.replace("\u2212", "-"))
    assets: list[dict[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0).strip()
        # A sentence fragment containing prose after an equality sign is not a
        # safe candidate.  Requiring a scientific glyph or compact symbolic LHS
        # prevents the common prose case from being swallowed.
        if not any(
            ord(char) > 127 or char in "=^_+/" for char in raw
        ):
            return raw
        split = _FORMULA_TRAILING_PROSE.search(raw)
        formula = raw[:split.start()].rstrip() if split else raw
        trailing = raw[split.start():] if split else ""
        derivative = _DERIVATIVE_ASSIGNMENT_PATTERN.fullmatch(formula)
        latex = (
            _derivative_match_to_tex(derivative)
            if derivative is not None
            else _formula_to_tex(formula)
        )
        assets.append({"raw": formula, "latex": latex})
        return "$" + latex + "$" + trailing

    normalized = _FORMULA_CANDIDATE.sub(repl, protected_text)
    return _restore(normalized, protected), assets


def _normalize_remaining_symbols(text: str) -> str:
    """Normalize isolated notation without touching already protected math."""

    protected_text, protected = _protect(text)
    protected_text = "".join(
        unicodedata.normalize("NFKC", char)
        if 0x1D400 <= ord(char) <= 0x1D7FF else char
        for char in protected_text
    )
    # Preserve bra-ket notation as one expression before isolated conversion.
    protected_text = re.sub(
        r"\|([A-Za-z\u0391-\u03a9\u03b1-\u03c9\u03d1\u03d5\u03f5\u03d6\u03f1\u03f0]+)[\u27e9\u232a]",
        lambda match: "$\\lvert " + "".join(_tex_atom(char) for char in match.group(1)) + r"\rangle$",
        protected_text,
    )
    protected_text = re.sub(
        r"\u221a\s*([A-Za-z0-9\u0391-\u03a9\u03b1-\u03c9\u03d1\u03d5\u03f5\u03d6\u03f1\u03f0])",
        lambda match: "$\\sqrt{" + _tex_atom(match.group(1)) + "}$",
        protected_text,
    )
    supers = _SUPERSCRIPT_CHARS
    subs = "".join(_SUBSCRIPT_MAP)
    protected_text = re.sub(
        f"[{re.escape(supers)}]+",
        lambda match: "$^{" + _translate_superscript(match.group(0)) + "}$",
        protected_text,
    )
    protected_text = re.sub(
        f"[{re.escape(subs)}]+",
        lambda match: "$_{" + "".join(_SUBSCRIPT_MAP[char] for char in match.group(0)) + "}$",
        protected_text,
    )
    for char, latex in {**_GREEK_TEX, **_SYMBOL_TEX}.items():
        protected_text = protected_text.replace(char, f" ${latex}$ ")
    protected_text = protected_text.replace("\u221a", r" $\sqrt{\cdot}$ ")
    protected_text = re.sub(r"[ \t]{2,}", " ", protected_text)
    return _restore(protected_text, protected)


def canonicalize_acronyms(text: str) -> tuple[str, list[dict[str, str]]]:
    """Keep a full definition once, then use the same acronym thereafter."""

    seen: dict[tuple[str, str], int] = {}
    ledger: list[dict[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        long = " ".join(match.group("long").split())
        short = match.group("short")
        long_words = re.findall(r"[A-Za-z0-9]+", long.lower())
        # A regex cannot know a term boundary on its own.  Refuse a candidate
        # that has swallowed sentence grammar ("platforms must satisfy ...").
        # That preserves the next genuine term definition for the ledger.
        if len(long_words) > 6 or any(
            word in _ACRONYM_NONTERM_WORDS for word in long_words
        ):
            return match.group(0)
        key = (long.lower(), short)
        occurrence = seen.get(key, 0) + 1
        seen[key] = occurrence
        if occurrence == 1:
            ledger.append({"long_form": long, "short_form": short, "first_occurrence": "retained"})
            return f"{long} ({short})"
        ledger.append({"long_form": long, "short_form": short, "first_occurrence": "collapsed"})
        return short

    return _ACRONYM_PATTERN.sub(repl, text), ledger


def sanitize_public_prose(text: str, *, preserve_verification_labels: bool = False) -> tuple[str, dict[str, Any]]:
    """Remove internal-workflow language from publication-facing text."""

    removed: list[str] = []
    output = text
    # Remove entire internally addressed sentences rather than leaving a
    # grammatically broken fragment in a scientific paragraph.
    sentence_pattern = re.compile(
        r"[^.!?\n]*(?:we|this review)\s+(?:promise|commit to|will deliver)\s+"
        r"[^.!?\n]*(?:deliverables|artifacts|outputs)[^.!?\n]*[.!?]?",
        re.I,
    )
    for match in list(sentence_pattern.finditer(output)):
        removed.append(match.group(0).strip())
    output = sentence_pattern.sub("", output)
    for pattern in _INTERNAL_PATTERNS:
        if preserve_verification_labels and "verification_deferred" in pattern.pattern:
            continue
        for match in list(pattern.finditer(output)):
            removed.append(match.group(0))
        output = pattern.sub("", output)
    output = re.sub(r"[ \t]{2,}", " ", output)
    output = re.sub(r"\n{3,}", "\n\n", output).strip() + "\n"
    remaining = []
    for pattern in _INTERNAL_PATTERNS:
        remaining.extend(match.group(0) for match in pattern.finditer(output))
    return output, {
        "removed_internal_fragments": removed,
        "remaining_internal_fragments": sorted(set(remaining)),
        "status": "passed" if not remaining else "needs_attention",
    }


def prepare_publication_markdown(
    text: str,
    *,
    preserve_verification_labels: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Apply deterministic reader-facing publication integrity transforms."""

    existing_math_normalized = _normalize_existing_math_spans(str(text or ""))
    formula_normalized, formula_assets = normalize_formula_spans(
        existing_math_normalized
    )
    ascii_normalized, ascii_formula_assets = normalize_ascii_math_spans(formula_normalized)
    normalized = _normalize_remaining_symbols(ascii_normalized)
    normalized, terminology_ledger = canonicalize_acronyms(normalized)
    normalized, public_audit = sanitize_public_prose(
        normalized,
        preserve_verification_labels=preserve_verification_labels,
    )
    return normalized, {
        "schema_version": "optomind.publication_integrity.v1",
        "equation_store": formula_assets + ascii_formula_assets,
        "unresolved_formula_hazards": find_unresolved_formula_hazards(normalized),
        "terminology_ledger": terminology_ledger,
        "public_prose_audit": public_audit,
    }
